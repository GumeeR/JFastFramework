"""Image optimisation: what it re-encodes, and everything it refuses to touch.

The refusals carry the weight here. Re-encoding a signed PDF produces an
invalid PDF; re-encoding a raw photograph destroys the negative; converting an
animated GIF to a still loses the animation with no error anywhere. So most of
what follows asserts that bytes came out exactly as they went in.

The measurement at the bottom is the number quoted in `docs/storage.md`. It is
generated here rather than remembered, because a saving nobody re-measures is a
saving that stops being true.
"""

from __future__ import annotations

import io
import random
import zlib
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from jfastframework.errors import PluginError
from jfastframework.plugins.builtin.storage import StoragePlugin
from jfastframework.storage.base import StorageBackend
from jfastframework.storage.images import STEP_NAME
from jfastframework.storage.pipeline import (
    PipelineConfigError,
    Upload,
    UploadRejected,
    UploadStep,
    build_pipeline,
)
from jfastframework.testing import build_test_app

# -- fixtures the tests build rather than carry -------------------------


def _photo(size: tuple[int, int] = (1600, 1200)) -> Image.Image:
    """A stand-in for a photograph: smooth ramps, hard edges, and fine texture.

    The texture matters, twice over. Without it the fixture is a gradient, PNG
    stores it in a few kilobytes and every measurement taken on it flatters the
    step. With *per-pixel* noise instead, JPEG spends a fortune preserving what
    WebP quietly smooths away, and the step measures 84% better than it is. So
    the grain is generated at quarter scale and interpolated up: detail both
    codecs have to actually encode. Seeded, so the saving quoted in the docs is
    the same number on the next run.
    """
    ramp = Image.linear_gradient("L")
    bands = (ramp, ramp.transpose(Image.ROTATE_90), ramp.transpose(Image.ROTATE_180))
    image = Image.merge("RGB", bands).resize(size, Image.BICUBIC)
    draw = ImageDraw.Draw(image)
    for index in range(24):
        left = (index * 61) % size[0]
        top = (index * 47) % size[1]
        radius = 20 + (index * 13) % 90
        draw.ellipse(
            (left, top, left + radius, top + radius),
            fill=((index * 37) % 256, (index * 73) % 256, (index * 151) % 256),
        )
    coarse = (size[0] // 4, size[1] // 4)
    grain = Image.frombytes("RGB", coarse, random.Random(7).randbytes(coarse[0] * coarse[1] * 3))
    return Image.blend(image, grain.resize(size, Image.BICUBIC), 0.35)


def _screenshot(size: tuple[int, int] = (1600, 1200)) -> Image.Image:
    """Flat colour and hard edges: the other half of what a disk actually holds."""
    image = Image.new("RGB", size, (247, 247, 250))
    draw = ImageDraw.Draw(image)
    for index in range(18):
        top = 40 + index * 60
        draw.rectangle((60, top, size[0] - 60, top + 34), fill=(224, 228, 236))
        draw.rectangle((72, top + 8, 72 + 20 * (index % 7 + 3), top + 26), fill=(90, 96, 112))
    return image


def _encode(image: Image.Image, fmt: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def _jpeg(image: Image.Image | None = None, *, quality: int = 90, **options: object) -> bytes:
    return _encode(image or _photo(), "JPEG", quality=quality, **options)


def _png(image: Image.Image | None = None, **options: object) -> bytes:
    return _encode(image or _screenshot(), "PNG", **options)


def _animated_gif(frames: int = 4) -> bytes:
    images = [
        Image.new("RGB", (64, 64), (index * 60, 255 - index * 60, 128)).convert(
            "P", palette=Image.Palette.ADAPTIVE
        )
        for index in range(frames)
    ]
    buffer = io.BytesIO()
    images[0].save(
        buffer, format="GIF", save_all=True, append_images=images[1:], duration=80, loop=0
    )
    return buffer.getvalue()


def _bomb_png(width: int, height: int) -> bytes:
    """A PNG whose IHDR declares a huge canvas but whose body is one pixel.

    The shape of the decompression-bomb CVE: the file is tiny, the allocation
    the decoder is asked to make is not. Rewriting IHDR means recomputing its
    CRC, or Pillow rejects the file as corrupt before the size ever matters.
    """
    raw = _encode(Image.new("RGB", (1, 1)), "PNG")
    signature, body = raw[:8], raw[8:]
    header = bytearray(body[8:21])
    header[0:4] = width.to_bytes(4, "big")
    header[4:8] = height.to_bytes(4, "big")
    crc = zlib.crc32(b"IHDR" + bytes(header)).to_bytes(4, "big")
    return signature + body[:8] + bytes(header) + crc + body[25:]


PDF = b"%PDF-1.7\n" + b"\x00" * 512
# Canon CR2 and Nikon NEF raws are TIFF containers: same magic, same sniffed
# type, and re-encoding one destroys a negative.
TIFF_RAW = b"II*\x00" + b"\x00" * 512


# -- the step ------------------------------------------------------------


def step(**config: object) -> UploadStep:
    """Built through the registry, so registration is under test too."""
    return build_pipeline("uploads", [STEP_NAME], {STEP_NAME: config}).steps[0]


async def run(built: UploadStep, key: str, data: bytes, content_type: str | None = None) -> Upload:
    return await built.process(
        Upload(disk="uploads", key=key, data=data, content_type=content_type)
    )


# -- what it leaves alone ------------------------------------------------


async def test_a_pdf_comes_out_byte_identical() -> None:
    result = await run(step(), "contract.pdf", PDF)
    assert result.data == PDF
    assert result.key == "contract.pdf"


async def test_a_tiff_is_left_alone_because_raw_wears_the_same_magic() -> None:
    result = await run(step(), "negative.cr2", TIFF_RAW)
    assert result.data == TIFF_RAW


async def test_something_it_cannot_identify_is_left_alone() -> None:
    data = b"just some text, honestly"
    assert (await run(step(), "notes.txt", data)).data == data


async def test_an_animated_gif_keeps_its_frames() -> None:
    gif = _animated_gif()
    result = await run(step(), "spinner.gif", gif)
    assert result.data == gif
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.n_frames == 4


async def test_re_encoding_that_would_grow_the_file_is_skipped() -> None:
    """Bytes already encoded harder than this disk asks for. Leave them be."""
    already = _encode(_photo((800, 600)), "WEBP", quality=40, method=6)
    result = await run(step(format="webp", quality=82), "done.webp", already)
    assert result.data == already


async def test_bytes_that_lie_about_their_type_are_read_from_the_bytes() -> None:
    """`validate` may not be in front of this step. It sniffs for itself."""
    result = await run(step(), "avatar.png", PDF, content_type="image/png")
    assert result.data == PDF


async def test_a_corrupt_image_is_passed_through_not_rejected() -> None:
    """This step optimises. Deciding what is admissible is `validate`'s job."""
    broken = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    assert (await run(step(), "broken.png", broken)).data == broken


# -- what it re-encodes --------------------------------------------------


async def test_a_photograph_gets_smaller_and_says_so() -> None:
    original = _jpeg()
    result = await run(step(format="webp", quality=82), "holiday.jpg", original)
    assert len(result.data) < len(original)
    assert result.content_type == "image/webp"
    # The key carries the extension a local disk derives its stored type from,
    # so leaving it as .jpg would serve WebP bytes labelled image/jpeg.
    assert result.key == "holiday.webp"


async def test_max_dimension_downscales_the_long_edge() -> None:
    original = _jpeg(_photo((3000, 1500)))
    result = await run(step(max_dimension=1200), "wide.jpg", original)
    with Image.open(io.BytesIO(result.data)) as image:
        assert max(image.size) == 1200
        assert image.size == (1200, 600)


async def test_a_smaller_image_is_not_upscaled() -> None:
    result = await run(step(max_dimension=4000), "small.jpg", _jpeg(_photo((640, 480))))
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.size == (640, 480)


async def test_keep_re_encodes_in_the_format_it_arrived_in() -> None:
    result = await run(step(format="keep", max_dimension=800), "holiday.jpg", _jpeg())
    assert result.content_type == "image/jpeg"
    assert result.key == "holiday.jpg"
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.format == "JPEG"


# -- EXIF is privacy, not size -------------------------------------------


def _jpeg_with_gps(orientation: int = 1, size: tuple[int, int] = (1600, 1200)) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = orientation
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (51.0, 30.0, 0.0)
    return _jpeg(_photo(size), exif=exif.tobytes())


async def test_gps_coordinates_are_stripped_by_default() -> None:
    result = await run(step(format="keep"), "holiday.jpg", _jpeg_with_gps())
    with Image.open(io.BytesIO(result.data)) as image:
        assert dict(image.getexif()) == {}


async def test_a_photography_app_can_keep_the_exif() -> None:
    result = await run(step(format="keep", strip_metadata=False), "holiday.jpg", _jpeg_with_gps())
    with Image.open(io.BytesIO(result.data)) as image:
        assert 0x8825 in image.getexif()


async def test_orientation_is_applied_before_it_is_dropped() -> None:
    """Stripping a rotation flag without rotating the pixels turns a photo sideways."""
    original = _jpeg_with_gps(orientation=6, size=(400, 200))
    result = await run(step(format="keep"), "portrait.jpg", original)
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.size == (200, 400)


# -- transparency --------------------------------------------------------


def _png_with_alpha(size: tuple[int, int] = (800, 600)) -> bytes:
    image = _photo(size).convert("RGBA")
    ImageDraw.Draw(image).rectangle((0, 0, size[0] // 2, size[1]), fill=(0, 0, 0, 0))
    return _encode(image, "PNG")


async def test_alpha_is_never_flattened_onto_a_colour_nobody_chose() -> None:
    result = await run(step(format="jpeg"), "logo.png", _png_with_alpha())
    assert result.content_type == "image/webp"
    assert result.key == "logo.webp"
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.convert("RGBA").getpixel((10, 10))[3] == 0


async def test_an_opaque_png_honours_the_configured_jpeg() -> None:
    result = await run(step(format="jpeg"), "chart.png", _png(_photo((800, 600))))
    assert result.content_type == "image/jpeg"
    assert result.key == "chart.jpg"


# -- decompression bombs -------------------------------------------------


async def test_a_declared_forty_gigapixel_png_is_refused() -> None:
    with pytest.raises(UploadRejected, match="pixels"):
        await run(step(), "bomb.png", _bomb_png(200_000, 200_000))


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
async def test_the_step_enforces_its_own_pixel_ceiling() -> None:
    """Pillow only *warns* between MAX_IMAGE_PIXELS and twice it. This rejects."""
    with pytest.raises(UploadRejected, match=r"40000 pixels.*30000"):
        await run(step(max_pixels=30_000), "big.jpg", _jpeg(_photo((200, 200))))


async def test_an_image_under_the_ceiling_is_processed() -> None:
    result = await run(step(max_pixels=100_000), "ok.jpg", _jpeg(_photo((200, 200))))
    assert result.content_type == "image/webp"


# -- configuration -------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"format": "bmp"}, "format"),
        ({"quality": 0}, "quality"),
        ({"quality": 101}, "quality"),
        ({"max_dimension": -1}, "max_dimension"),
        ({"max_pixels": 0}, "max_pixels"),
        ({"strip_exif": True}, "no setting"),
    ],
)
def test_a_misconfigured_step_fails_at_startup(config: dict[str, object], message: str) -> None:
    with pytest.raises(PipelineConfigError, match=message):
        step(**config)


# -- through the plugin --------------------------------------------------


def images_app(tmp_path: Path, **optimise: object) -> object:
    return build_test_app(
        plugins=["observability", "storage"],
        extra_plugins=[StoragePlugin],
        raw={
            "plugin": {
                "storage": {
                    "default": "uploads",
                    "signing_key": "test-signing-key-at-least-32-bytes-long",
                    "disks": {
                        "uploads": {
                            "driver": "local",
                            "root": str(tmp_path / "uploads"),
                            "pipeline": ["validate", STEP_NAME],
                            "validate": {"allow": ["image/jpeg", "image/png"]},
                            STEP_NAME: {"format": "webp", "quality": 82, **optimise},
                        }
                    },
                }
            }
        },
    )


def _disk(app: object) -> StorageBackend:
    registry = app.state.jfast.require("storage")  # type: ignore[attr-defined]
    return registry.disk("uploads")  # type: ignore[no-any-return]


async def test_a_configured_disk_stores_the_optimised_bytes(tmp_path: Path) -> None:
    disk = _disk(images_app(tmp_path, max_dimension=2400))
    original = _jpeg()
    stored = await disk.put("holiday.jpg", original)
    assert stored.key == "holiday.webp"
    assert stored.size < len(original)
    assert (await disk.get("holiday.webp"))[:4] == b"RIFF"


async def test_a_step_configured_but_not_run_is_still_a_startup_error(tmp_path: Path) -> None:
    with pytest.raises(PluginError, match=STEP_NAME):
        build_test_app(
            plugins=["observability", "storage"],
            extra_plugins=[StoragePlugin],
            raw={
                "plugin": {
                    "storage": {
                        "default": "d",
                        "disks": {
                            "d": {
                                "driver": "local",
                                "root": str(tmp_path),
                                STEP_NAME: {"format": "webp"},
                            }
                        },
                    }
                }
            },
        )


# -- the number in the docs ----------------------------------------------


async def test_the_saving_quoted_in_the_docs_is_the_measured_one() -> None:
    """Regenerate with `pytest -s -k saving_quoted`; the print is the source.

    The flat-colour row is in the table on purpose. It is the case where the
    step is nearly pointless, and a docs table that only showed the photograph
    would be selling a number the reader will not reproduce.
    """
    built = step(format="webp", quality=82, max_dimension=2400)
    measured: dict[str, tuple[int, int]] = {}
    for label, original, key in (
        ("jpeg q90 photograph 1600x1200", _jpeg(), "photo.jpg"),
        ("png photograph 1600x1200", _png(_photo()), "photo.png"),
        ("png flat-colour screenshot 1600x1200", _png(), "shot.png"),
        ("png photograph with alpha 800x600", _png_with_alpha(), "alpha.png"),
    ):
        result = await run(built, key, original)
        measured[label] = (len(original), len(result.data))

    for label, (before, after) in measured.items():
        print(f"{label}: {before} -> {after} bytes ({100 - after * 100 // before}% smaller)")

    # Loose bounds: the exact bytes move with the Pillow release, the shape of
    # the claim in the docs does not.
    photograph = measured["jpeg q90 photograph 1600x1200"]
    assert photograph[1] < photograph[0] * 0.6
    flat = measured["png flat-colour screenshot 1600x1200"]
    assert flat[1] <= flat[0]
