"""Two claims the docs make that have to stay true.

Documentation drifts in silence, so the two statements that were wrong enough
to cost someone an afternoon are pinned here instead of trusted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"
LOCAL_SETUP = [DOCS / "local-setup.md", DOCS / "es" / "local-setup.md"]
DATASTORES = [DOCS / "datastores.md", DOCS / "es" / "datastores.md"]


@pytest.mark.parametrize("page", LOCAL_SETUP, ids=lambda p: str(p.relative_to(DOCS)))
def test_the_install_instruction_does_not_ask_for_pre_releases(page: Path) -> None:
    # `--pre` is not needed to resolve a package whose every release is a
    # pre-release, and it opts every transitive dependency in as well. Prose
    # explaining that is fine; the instruction is what has to go.
    text = page.read_text(encoding="utf-8")
    assert "pip install --pre" not in text, f"{page.name} still installs with --pre"


@pytest.mark.parametrize("page", LOCAL_SETUP, ids=lambda p: str(p.relative_to(DOCS)))
def test_the_install_instruction_is_still_there(page: Path) -> None:
    assert "pip install jfastframework" in page.read_text(encoding="utf-8")


@pytest.mark.parametrize("page", DATASTORES, ids=lambda p: str(p.relative_to(DOCS)))
def test_the_enum_column_says_what_it_does_not_enforce(page: Path) -> None:
    # Two questions a reader has to be able to answer before choosing an enum:
    # does adding a member need DDL, and can an invalid value reach the table.
    text = page.read_text(encoding="utf-8")
    assert "native_enum" in text, f"{page.name} never mentions native_enum"
    assert "create_constraint" in text, f"{page.name} never mentions create_constraint"
