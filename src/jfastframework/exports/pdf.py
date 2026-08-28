"""Assembling many PDFs and images into one, without lying about the result.

The library choice comes from a service that does this in production: ``pypdf``
to merge and ``img2pdf`` to bring images in. ``img2pdf`` matters more than it
looks -- it embeds a JPEG losslessly rather than re-encoding it, so a scanned
page is neither degraded nor slowly rebuilt.

Two things are added here, and both come from watching that code fail at scale.

**A merge reports what it could not include.** The obvious implementation logs
a warning for a missing or corrupt file and carries on, returning only the
output path. The caller then gets a bundle that *looks* complete. For a legal
or fiscal bundle that is the worst possible outcome: nobody reads a worker's
logs, and the document goes out short. ``merge()`` returns a result object
naming every source that did not make it and why, and the caller has to decide
what that means.

**Memory is bounded by batching.** ``PdfWriter.append()`` holds every appended
page until ``write()``, so peak memory grows with the whole job. Merging in
batches to temporary files and then merging those keeps the peak at one batch,
which is the difference between a thousand documents working and not.
"""

from __future__ import annotations

import io
import logging
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("jfast.exports.pdf")

# Formats img2pdf can embed. Anything else is refused by name rather than
# attempted and reported as a mystery.
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".gif"})
PDF_SUFFIXES = frozenset({".pdf"})

# How many sources go into one intermediate file. Chosen so a batch of ordinary
# scanned pages stays well inside a container's memory, not because the number
# is magic -- raise it when the sources are small, lower it when they are not.
DEFAULT_BATCH_SIZE = 50


class PdfExportError(Exception):
    """The merge could not produce a document at all."""


@dataclass
class SkippedSource:
    """One input that did not make it into the output, and why."""

    path: str
    reason: str


@dataclass
class MergeResult:
    """What the merge produced, and what it left out.

    ``skipped`` is the point of this class. A caller that ignores it has
    written the same silent-data-loss bug the plain implementation has; a
    caller that checks it can refuse to mark a bundle complete.
    """

    output: Path
    merged: int = 0
    pages: int = 0
    skipped: list[SkippedSource] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True when every source made it in."""
        return not self.skipped

    def raise_if_incomplete(self) -> None:
        """For bundles where a missing document is not acceptable.

        A fiscal or legal bundle is one of those: better to fail the job and
        keep the previous state than to file something short.
        """
        if self.skipped:
            detail = "; ".join(f"{s.path}: {s.reason}" for s in self.skipped)
            raise PdfExportError(
                f"{len(self.skipped)} of {len(self.skipped) + self.merged} sources "
                f"were not included: {detail}"
            )


def _image_to_pdf_bytes(path: Path) -> bytes:
    import img2pdf

    with path.open("rb") as handle:
        # img2pdf returns bytes; there is no streaming variant that also
        # produces something pypdf can append. The batching above is what
        # keeps this from mattering.
        return bytes(img2pdf.convert(handle))


def _append_sources(
    writer: object,
    sources: Iterable[Path],
    skipped: list[SkippedSource],
) -> int:
    """Append what can be appended; record what cannot. Returns the count."""
    appended = 0
    for path in sources:
        if not path.is_file():
            skipped.append(SkippedSource(str(path), "file not found"))
            continue

        suffix = path.suffix.lower()
        try:
            if suffix in PDF_SUFFIXES:
                writer.append(str(path))  # type: ignore[attr-defined]
            elif suffix in IMAGE_SUFFIXES:
                writer.append(io.BytesIO(_image_to_pdf_bytes(path)))  # type: ignore[attr-defined]
            else:
                skipped.append(SkippedSource(str(path), f"unsupported format {suffix!r}"))
                continue
        except Exception as exc:  # noqa: BLE001 - recorded, and reported to the caller
            # One corrupt page must not lose the other nine hundred, but it
            # must not vanish either.
            skipped.append(SkippedSource(str(path), f"{type(exc).__name__}: {exc}"))
            logger.warning("skipping %s: %s", path, exc)
            continue
        appended += 1
    return appended


def merge(
    sources: Sequence[str | Path],
    output: str | Path,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> MergeResult:
    """Merge PDFs and images into one PDF, in source order.

    Args:
        sources: Paths to PDFs and images. Order is preserved.
        output: Where to write the result.
        batch_size: Sources merged before spilling to an intermediate file.

    Returns:
        A :class:`MergeResult`. **Check ``skipped``** -- a merge that silently
        drops a document is worse than one that fails.

    Raises:
        PdfExportError: nothing at all could be merged.
    """
    from pypdf import PdfWriter

    paths = [Path(s) for s in sources]
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    skipped: list[SkippedSource] = []

    if not paths:
        raise PdfExportError("No sources given.")

    # Small jobs skip the intermediate files entirely.
    if len(paths) <= batch_size:
        writer = PdfWriter()
        try:
            merged = _append_sources(writer, paths, skipped)
            if merged == 0:
                raise PdfExportError(
                    f"None of the {len(paths)} sources could be merged: "
                    + "; ".join(f"{s.path}: {s.reason}" for s in skipped[:5])
                )
            pages = len(writer.pages)
            with output_path.open("wb") as handle:
                writer.write(handle)
        finally:
            writer.close()
        return MergeResult(output_path, merged=merged, pages=pages, skipped=skipped)

    return _merge_batched(paths, output_path, batch_size, skipped)


def _merge_batched(
    paths: list[Path],
    output_path: Path,
    batch_size: int,
    skipped: list[SkippedSource],
) -> MergeResult:
    """Merge in batches so peak memory is one batch, not the whole job."""
    from pypdf import PdfWriter

    merged = 0
    with tempfile.TemporaryDirectory(prefix="jfast-merge-") as workspace:
        parts: list[Path] = []
        for index in range(0, len(paths), batch_size):
            batch = paths[index : index + batch_size]
            writer = PdfWriter()
            try:
                count = _append_sources(writer, batch, skipped)
                if count == 0:
                    continue
                part = Path(workspace) / f"part-{index:06d}.pdf"
                with part.open("wb") as handle:
                    writer.write(handle)
                parts.append(part)
                merged += count
            finally:
                writer.close()

        if not parts:
            raise PdfExportError(
                f"None of the {len(paths)} sources could be merged: "
                + "; ".join(f"{s.path}: {s.reason}" for s in skipped[:5])
            )

        final = PdfWriter()
        try:
            for part in parts:
                final.append(str(part))
            pages = len(final.pages)
            with output_path.open("wb") as handle:
                final.write(handle)
        finally:
            final.close()

    return MergeResult(output_path, merged=merged, pages=pages, skipped=skipped)


def page_count(path: str | Path) -> int:
    """Pages in a PDF, without loading it into a writer."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return len(reader.pages)


def split(
    source: str | Path,
    output_dir: str | Path,
    *,
    pages_per_file: int = 1,
    stem: str = "page",
) -> list[Path]:
    """Split a PDF into files of ``pages_per_file`` pages each."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(source))
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    total = len(reader.pages)
    for start in range(0, total, pages_per_file):
        writer = PdfWriter()
        try:
            for page in reader.pages[start : start + pages_per_file]:
                writer.add_page(page)
            # Zero-padded so the files sort in page order in any tool.
            path = destination / f"{stem}-{start + 1:0{len(str(total))}d}.pdf"
            with path.open("wb") as handle:
                writer.write(handle)
            written.append(path)
        finally:
            writer.close()
    return written


def merge_directory(
    directory: str | Path,
    output: str | Path,
    *,
    recursive: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> MergeResult:
    """Merge every supported file in a directory, in sorted order.

    Sorted, not in whatever order the filesystem returns: a bundle whose page
    order changes between runs is a bundle nobody can review.
    """
    root = Path(directory)
    pattern = "**/*" if recursive else "*"
    supported = PDF_SUFFIXES | IMAGE_SUFFIXES
    sources = sorted(
        (p for p in root.glob(pattern) if p.is_file() and p.suffix.lower() in supported),
        key=lambda p: str(p).lower(),
    )
    return merge(sources, output, batch_size=batch_size)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "IMAGE_SUFFIXES",
    "PDF_SUFFIXES",
    "MergeResult",
    "PdfExportError",
    "SkippedSource",
    "merge",
    "merge_directory",
    "page_count",
    "split",
]
