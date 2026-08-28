"""Bulk PDF assembly and streamed spreadsheets.

The assertions that matter are about failure. A merge that silently drops a
document produces a bundle that looks complete, and for a fiscal or legal
bundle that is worse than an error -- so most of this file is about what
`skipped` contains.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from jfastframework.capabilities import CATALOG, get, names
from jfastframework.exports import excel, pdf


def _pdf(path: Path, pages: int = 1) -> Path:
    """A real PDF, written with the library under test."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        writer.write(handle)
    writer.close()
    return path


def _png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (40, 40), (200, 30, 40)).save(path)
    return path


# -- merging ------------------------------------------------------------


def test_merge_preserves_order_and_counts_pages(tmp_path: Path) -> None:
    sources = [_pdf(tmp_path / f"{i}.pdf", pages=i + 1) for i in range(3)]
    result = pdf.merge(sources, tmp_path / "out.pdf")

    assert result.complete
    assert result.merged == 3
    assert result.pages == 1 + 2 + 3
    assert pdf.page_count(result.output) == 6


def test_images_are_merged_alongside_pdfs(tmp_path: Path) -> None:
    sources = [_pdf(tmp_path / "a.pdf"), _png(tmp_path / "b.png")]
    result = pdf.merge(sources, tmp_path / "out.pdf")

    assert result.merged == 2
    assert result.pages == 2


def test_a_missing_file_is_reported_not_swallowed(tmp_path: Path) -> None:
    """The whole point: the caller must be able to see the bundle is short."""
    sources = [_pdf(tmp_path / "a.pdf"), tmp_path / "gone.pdf"]
    result = pdf.merge(sources, tmp_path / "out.pdf")

    assert result.merged == 1
    assert not result.complete
    assert len(result.skipped) == 1
    assert "not found" in result.skipped[0].reason
    assert result.skipped[0].path.endswith("gone.pdf")


def test_a_corrupt_file_does_not_lose_the_others(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a pdf")
    sources = [_pdf(tmp_path / "a.pdf"), broken, _pdf(tmp_path / "c.pdf")]

    result = pdf.merge(sources, tmp_path / "out.pdf")

    assert result.merged == 2, "one bad page must not lose the good ones"
    assert len(result.skipped) == 1
    assert result.skipped[0].path.endswith("broken.pdf")


def test_an_unsupported_format_is_named(tmp_path: Path) -> None:
    other = tmp_path / "notes.docx"
    other.write_bytes(b"whatever")
    result = pdf.merge([_pdf(tmp_path / "a.pdf"), other], tmp_path / "out.pdf")

    assert "unsupported format" in result.skipped[0].reason


def test_raise_if_incomplete_is_available_for_bundles_that_must_be_whole(
    tmp_path: Path,
) -> None:
    result = pdf.merge([_pdf(tmp_path / "a.pdf"), tmp_path / "gone.pdf"], tmp_path / "out.pdf")
    with pytest.raises(pdf.PdfExportError, match="were not included"):
        result.raise_if_incomplete()


def test_nothing_mergeable_is_an_error_not_an_empty_pdf(tmp_path: Path) -> None:
    with pytest.raises(pdf.PdfExportError, match="None of the"):
        pdf.merge([tmp_path / "a.pdf", tmp_path / "b.pdf"], tmp_path / "out.pdf")


def test_no_sources_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(pdf.PdfExportError, match="No sources"):
        pdf.merge([], tmp_path / "out.pdf")


def test_batching_produces_the_same_document(tmp_path: Path) -> None:
    """Peak memory changes; the result must not."""
    sources = [_pdf(tmp_path / f"{i:03d}.pdf") for i in range(12)]

    whole = pdf.merge(sources, tmp_path / "whole.pdf", batch_size=100)
    batched = pdf.merge(sources, tmp_path / "batched.pdf", batch_size=3)

    assert whole.pages == batched.pages == 12
    assert whole.merged == batched.merged == 12
    assert batched.complete


def test_batching_still_reports_what_it_skipped(tmp_path: Path) -> None:
    sources: list[Path] = []
    for index in range(9):
        sources.append(_pdf(tmp_path / f"{index}.pdf"))
    sources.append(tmp_path / "missing.pdf")

    result = pdf.merge(sources, tmp_path / "out.pdf", batch_size=2)

    assert result.merged == 9
    assert len(result.skipped) == 1


def test_merge_directory_is_sorted(tmp_path: Path) -> None:
    """A bundle whose page order changes between runs cannot be reviewed."""
    source = tmp_path / "docs"
    for name in ("c.pdf", "a.pdf", "b.pdf"):
        _pdf(source / name)

    result = pdf.merge_directory(source, tmp_path / "out.pdf")
    assert result.merged == 3


def test_split_round_trips(tmp_path: Path) -> None:
    original = _pdf(tmp_path / "big.pdf", pages=5)
    parts = pdf.split(original, tmp_path / "parts", pages_per_file=2)

    assert len(parts) == 3
    assert [pdf.page_count(p) for p in parts] == [2, 2, 1]
    # Zero-padded, so the parts sort in page order.
    assert [p.name for p in parts] == sorted(p.name for p in parts)


# -- spreadsheets -------------------------------------------------------


def test_streamed_rows_round_trip(tmp_path: Path) -> None:
    columns = [
        excel.Column("RFC", width=16),
        excel.Column("Total", width=12, number_format="#,##0.00"),
        excel.Column("Fecha", width=12, number_format="yyyy-mm-dd"),
    ]
    rows = [["AAA010101AAA", Decimal("1234.50"), date(2026, 1, 15)]]

    written = excel.stream_rows(tmp_path / "r.xlsx", columns, rows)
    assert written == 1

    back = excel.read_rows(tmp_path / "r.xlsx")
    assert back[0] == ["RFC", "Total", "Fecha"]
    assert back[1][0] == "AAA010101AAA"


def test_a_generator_is_consumed_lazily(tmp_path: Path) -> None:
    """A cursor can be handed straight in; nothing is held whole."""

    def produce():  # type: ignore[no-untyped-def]
        for index in range(500):
            yield [f"row-{index}", index]

    written = excel.stream_rows(
        tmp_path / "big.xlsx",
        [excel.Column("Name"), excel.Column("N")],
        produce(),
    )
    assert written == 500


def test_unwritable_values_are_coerced_not_crashed(tmp_path: Path) -> None:
    """openpyxl fails deep in the serialiser; failing at the row is kinder."""
    written = excel.stream_rows(
        tmp_path / "c.xlsx",
        [excel.Column("A"), excel.Column("B")],
        [[{"a": 1}, ["x", "y"]]],
    )
    assert written == 1
    back = excel.read_rows(tmp_path / "c.xlsx")
    assert back[1][1] == "x, y"


# -- the catalogue ------------------------------------------------------


def test_every_capability_explains_itself() -> None:
    """A catalogue entry with no rationale is a pip alias."""
    for name in names():
        spec = CATALOG[name]
        assert spec.packages, f"{name} installs nothing"
        assert spec.rationale, f"{name} does not say why these packages"
        assert spec.summary


def test_opencv_is_the_headless_wheel() -> None:
    """The GUI wheel installs cleanly and fails at import inside a container."""
    packages = " ".join(get("vision").packages)
    assert "opencv-python-headless" in packages
    assert "opencv-python>=" not in packages


def test_weasyprint_declares_its_system_packages() -> None:
    """Installing the wheel is not enough, and the failure looks unrelated."""
    assert get("render").system_packages


def test_an_unknown_capability_lists_the_known_ones() -> None:
    with pytest.raises(KeyError, match="Known:"):
        get("blockchain")
