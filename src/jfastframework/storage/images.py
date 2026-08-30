"""Re-encoding images on the way in, for the disks that ask for it.

    [plugin.storage.disks.uploads]
    pipeline = ["validate", "optimise-image"]

    [plugin.storage.disks.uploads.optimise-image]
    format = "webp"
    quality = 82
    max_dimension = 2400
    strip_metadata = true

Optional, off unless a disk names it, and deliberately incurious about
anything it does not recognise. A signed PDF that goes through an image codec
comes out an invalid PDF. A Canon CR2 raw re-encoded is a destroyed negative,
and CR2 is a TIFF container -- same magic bytes, same sniffed type -- so this
step will not touch `image/tiff` at all. Refusing to act on the unknown is the
only default that cannot lose data.

What it does act on is `image/jpeg`, `image/png`, `image/gif` and `image/webp`,
and even there it stops at the first thing it cannot do without losing
something: an animated image, an alpha channel the target format cannot carry,
a file Pillow cannot decode.

The whole encode runs in `asyncio.to_thread`. Image codecs are CPU, not I/O,
which is exactly why they read as harmless: nothing about `Image.open(...)`
looks like a network call, and a 24-megapixel JPEG is still hundreds of
milliseconds of decode plus seconds of re-encode with every other request on
the worker waiting. `contracts/blocking.py` knows about Pillow because of this
step.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any, NamedTuple

from jfastframework.storage.pipeline import (
    PipelineConfigError,
    Upload,
    UploadRejected,
    UploadStep,
    sniff_content_type,
)

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

STEP_NAME = "optimise-image"

# 50 megapixels: larger than any camera a user is likely to be holding and
# smaller than Pillow's own 89.5MP default, so configuring nothing tightens
# the guard rather than loosening it.
DEFAULT_MAX_PIXELS = 50_000_000


class Target(NamedTuple):
    """One output format: what Pillow calls it, what it is served as, what it is named."""

    pil: str
    content_type: str
    extension: str


TARGETS: Mapping[str, Target] = {
    "webp": Target("WEBP", "image/webp", ".webp"),
    "jpeg": Target("JPEG", "image/jpeg", ".jpg"),
    "png": Target("PNG", "image/png", ".png"),
    "gif": Target("GIF", "image/gif", ".gif"),
}

# The sniffed types this step will re-encode, and what `format = "keep"` means
# for each. `image/tiff` is absent on purpose -- see the module docstring --
# and so are avif and heic, which Pillow cannot decode without plugins that
# may or may not be in the image.
HANDLED: Mapping[str, Target] = {
    "image/jpeg": TARGETS["jpeg"],
    "image/png": TARGETS["png"],
    "image/gif": TARGETS["gif"],
    "image/webp": TARGETS["webp"],
}

FORMATS: tuple[str, ...] = ("keep", *sorted(TARGETS))

_SAVE_OPTIONS: Mapping[str, Mapping[str, Any]] = {
    # method=6 is Pillow's slowest, smallest WebP encode. It runs once, in a
    # thread; the bytes it produces are served for the life of the object.
    "WEBP": {"method": 6},
    "JPEG": {"optimize": True, "progressive": True},
    # `quality` means nothing to a lossless format. Compression level does.
    "PNG": {"optimize": True, "compress_level": 9},
    "GIF": {"optimize": True},
}
_LOSSY = frozenset({"WEBP", "JPEG"})

SETTINGS: tuple[str, ...] = (
    "format",
    "quality",
    "max_dimension",
    "strip_metadata",
    "max_pixels",
)


def _has_alpha(image: PILImage) -> bool:
    return image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info


def _prepare(image: PILImage, target: Target) -> PILImage:
    """The mode the target format can actually hold."""
    from PIL import Image

    if target.pil == "JPEG" and image.mode not in ("RGB", "L"):
        return image.convert("RGB")
    if target.pil == "WEBP" and image.mode not in ("RGB", "RGBA"):
        return image.convert("RGBA" if _has_alpha(image) else "RGB")
    if target.pil == "GIF" and image.mode != "P":
        return image.convert("P", palette=Image.Palette.ADAPTIVE)
    return image


def _with_extension(key: str, extension: str) -> str:
    """Re-point the key at the bytes actually written.

    A local disk reads an object's content type back out of its key, so
    leaving `holiday.jpg` on WebP bytes serves them labelled `image/jpeg`.
    `put()` returns the key it wrote, and that is the one to keep.
    """
    head, separator, name = key.rpartition("/")
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{head}{separator}{stem}{extension}"


def _whole(value: Any, setting: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{setting} must be a whole number, not {value!r}")
    if value < minimum:
        raise ValueError(f"{setting} must be {minimum} or more, got {value}")
    return value


class OptimiseImageStep:
    """Re-encode what it recognises; hand everything else on untouched."""

    name = STEP_NAME

    def __init__(
        self,
        disk: str,
        *,
        format: str = "webp",
        quality: int = 82,
        max_dimension: int = 0,
        strip_metadata: bool = True,
        max_pixels: int = DEFAULT_MAX_PIXELS,
    ) -> None:
        if format not in FORMATS:
            raise ValueError(f"format must be one of {', '.join(FORMATS)}, not {format!r}")
        if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
            raise ValueError(f"quality must be between 1 and 100, got {quality!r}")
        if not isinstance(strip_metadata, bool):
            raise ValueError(f"strip_metadata must be true or false, not {strip_metadata!r}")

        self.disk = disk
        self.format = format
        self.quality = quality
        # 0 disables the resize. A default that shrank pictures would be a
        # surprise dressed up as an optimisation.
        self.max_dimension = _whole(max_dimension, "max_dimension", minimum=0)
        self.strip_metadata = strip_metadata
        self.max_pixels = _whole(max_pixels, "max_pixels", minimum=1)

        try:
            from PIL import Image
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on the extra
            raise PipelineConfigError(
                f"storage disk {disk!r}: the {STEP_NAME!r} step needs Pillow. "
                f"Install jfastframework[images]."
            ) from exc

        # Pillow's own bomb guard is a module global, so with two disks
        # configured the last one built wins it. The header check in
        # `_reencode` is what enforces *this* disk's ceiling; this assignment
        # only covers a decode that never reaches that check.
        Image.MAX_IMAGE_PIXELS = self.max_pixels

    async def process(self, upload: Upload) -> Upload:
        # Sniffed here rather than read off `upload.content_type`: that value
        # is whatever the browser sent, and `validate` is not guaranteed to be
        # in front of this step to have replaced it.
        sniffed = sniff_content_type(upload.data)
        source = HANDLED.get(sniffed) if sniffed else None
        if source is None:
            return upload

        encoded = await asyncio.to_thread(self._reencode, upload.key, upload.data, source)
        if encoded is None:
            return upload

        data, target, resized = encoded
        # Re-encoding does not always shrink a file -- a hand-optimised PNG of
        # flat colour routinely grows. Keeping the original is the honest
        # outcome, unless the point of the run was the pixel dimensions.
        if not resized and len(data) >= len(upload.data):
            return upload
        return replace(
            upload,
            data=data,
            content_type=target.content_type,
            key=_with_extension(upload.key, target.extension),
        )

    def _reencode(self, key: str, data: bytes, source: Target) -> tuple[bytes, Target, bool] | None:
        """The whole codec run. Synchronous, and only ever called in a thread."""
        from PIL import Image, ImageOps

        try:
            with Image.open(io.BytesIO(data)) as opened:
                pixels = opened.width * opened.height
                if pixels > self.max_pixels:
                    raise UploadRejected(
                        f"disk {self.disk!r}: {key!r} declares {pixels} pixels, over this "
                        f"disk's max_pixels of {self.max_pixels}"
                    )
                if getattr(opened, "n_frames", 1) > 1:
                    # A still frame of an animation is not the animation, and
                    # nothing downstream would report the loss.
                    return None

                # Orientation is applied before it is dropped: stripping the
                # flag without rotating the pixels turns a portrait sideways,
                # and WebP viewers ignore the flag even when it survives.
                transposed = ImageOps.exif_transpose(opened)
                image = transposed if transposed is not None else opened

                target = source if self.format == "keep" else TARGETS[self.format]
                if target.pil == "JPEG" and _has_alpha(image):
                    # JPEG has no alpha channel. Compositing onto a background
                    # colour nobody chose is a silent edit of the picture, so
                    # the step keeps the transparency and gives up the format.
                    target = TARGETS["webp"]

                resized = False
                if self.max_dimension and max(image.size) > self.max_dimension:
                    image.thumbnail(
                        (self.max_dimension, self.max_dimension), Image.Resampling.LANCZOS
                    )
                    resized = True

                icc = image.info.get("icc_profile")
                exif = image.info.get("exif")
                prepared = _prepare(image, target)

                options: dict[str, Any] = dict(_SAVE_OPTIONS[target.pil])
                if target.pil in _LOSSY:
                    options["quality"] = self.quality
                if icc:
                    # A colour profile is colour, not privacy. Dropping it
                    # shifts every pixel in a way nobody asked for.
                    options["icc_profile"] = icc
                if exif and not self.strip_metadata:
                    options["exif"] = exif
                # Pillow's savers read exif, XMP and PNG text chunks straight
                # out of `info`, so emptying it is what actually removes the
                # GPS coordinates rather than merely not adding them.
                prepared.info = {}

                buffer = io.BytesIO()
                prepared.save(buffer, format=target.pil, **options)
        except Image.DecompressionBombError as exc:
            raise UploadRejected(
                f"disk {self.disk!r}: {key!r} declares more pixels than this disk decodes "
                f"(max_pixels is {self.max_pixels}); it is refused rather than allocated"
            ) from exc
        except (OSError, ValueError):
            # Unreadable, truncated, or a format Pillow declines. Deciding what
            # is admissible belongs to `validate`; this step only optimises,
            # so it hands the bytes on exactly as they arrived.
            return None

        return buffer.getvalue(), target, resized


def build_optimise_image(disk: str, config: Mapping[str, Any]) -> UploadStep:
    """Factory registered under `optimise-image`."""
    unknown = set(config) - set(SETTINGS)
    if unknown:
        raise PipelineConfigError(
            f"storage disk {disk!r}: {STEP_NAME} has no setting "
            f"{', '.join(sorted(repr(key) for key in unknown))}; "
            f"it takes {', '.join(SETTINGS)}"
        )
    try:
        return OptimiseImageStep(disk, **{key: config[key] for key in SETTINGS if key in config})
    except ValueError as exc:
        raise PipelineConfigError(f"storage disk {disk!r}: {STEP_NAME} {exc}") from exc
