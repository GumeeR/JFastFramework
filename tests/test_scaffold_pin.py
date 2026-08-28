"""The version a generated service pins.

This exists because of a shipped bug: the requirements template carried a
literal ``~=0.7``, the package was renumbered to ``0.1.0a1``, and every
generated project shipped a requirements file pip could not satisfy. A version
written by hand in a template is a version that goes stale silently.
"""

from __future__ import annotations

import re

from jfastframework import __version__
from jfastframework.cli.scaffold import framework_pin


def test_the_pin_matches_the_running_version() -> None:
    """Whatever it emits has to be satisfiable by the framework that emitted it."""
    pin = framework_pin()
    if pin.startswith("=="):
        assert pin == f"=={__version__}"
    else:
        major, minor = __version__.split(".")[:2]
        assert pin == f"~={major}.{minor}"


def test_a_prerelease_is_pinned_exactly(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`~=0.1` does not match `0.1.0a1`.

    A compatible-release clause normalises to `>= 0.1, == 0.*`, and `0.1.0a1`
    sorts below `0.1.0` -- so it is out of range even with `--pre`. Exact is
    also the honest pin while the API is still moving.
    """
    import jfastframework

    for version in ("0.1.0a1", "0.2.0b3", "1.0.0rc1", "0.3.0.dev2"):
        monkeypatch.setattr(jfastframework, "__version__", version)
        assert framework_pin() == f"=={version}"


def test_a_final_release_gets_a_compatible_pin(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import jfastframework

    monkeypatch.setattr(jfastframework, "__version__", "1.4.2")
    assert framework_pin() == "~=1.4"

    monkeypatch.setattr(jfastframework, "__version__", "0.9.0")
    assert framework_pin() == "~=0.9"


def test_no_template_hardcodes_a_version() -> None:
    """The bug was a literal in a template; keep it from coming back."""
    from pathlib import Path

    import jfastframework

    templates = Path(jfastframework.__file__).parent / "templates"
    offenders = []
    for path in templates.rglob("requirements*.j2"):
        body = path.read_text(encoding="utf-8")
        if re.search(r"jfastframework\[[^\]]*\]\s*[~=><]=\s*\d", body):
            offenders.append(str(path.relative_to(templates)))
    assert not offenders, f"hardcoded framework version in: {offenders}"
