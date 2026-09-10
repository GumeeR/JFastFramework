"""Public behavior of the two safe terminal novelty imports."""

from __future__ import annotations

import io
import math

import pytest

import pene
import vagina


def test_pene_prints_bundled_art() -> None:
    output = io.StringIO()

    pene.show(output)

    assert output.getvalue() == pene.ART


def test_vagina_prints_bundled_art() -> None:
    output = io.StringIO()

    vagina.show(output)

    assert output.getvalue() == vagina.ART


def test_combining_the_imports_returns_an_educational_link() -> None:
    assert pene.educational_link(vagina) in pene.EDUCATIONAL_LINKS


def test_combination_rejects_an_unrelated_module() -> None:
    with pytest.raises(TypeError, match="vagina module"):
        pene.educational_link(math)
