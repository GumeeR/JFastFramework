"""The installer must not die because the terminal cannot draw a tick.

A Windows console is cp1252 or cp850 far more often than UTF-8. Writing ``✓``
to one does not degrade to a placeholder -- ``sys.stdout`` raises
``UnicodeEncodeError`` mid-write, so ``jfast start`` crashed with a traceback
after it had already created half a project.

The rendering tests below are the ones that matter: they push every helper
through a stream that really is cp1252 and assert nothing raises. Asserting on
which glyph was chosen would pass while the output still crashed.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from rich.console import Console

from jfastframework.cli import glyphs, ui

LEGACY = ("cp1252", "cp850", "ascii", "latin-1")


def _console(encoding: str) -> Console:
    """A Console whose writes really are encoded, so a bad glyph really fails."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="")
    # force_terminal keeps the styling path alive; without it rich takes the
    # plain-text branch and the test stops exercising what it means to.
    return Console(file=stream, force_terminal=True, width=80)


# -- resolution ---------------------------------------------------------


@pytest.mark.parametrize("encoding", LEGACY)
def test_a_legacy_codepage_gets_the_ascii_set(encoding: str) -> None:
    resolved = glyphs.Glyphs.resolve(encoding)
    assert resolved.unicode is False
    assert resolved.tick == "+"


def test_utf8_keeps_the_drawn_set() -> None:
    resolved = glyphs.Glyphs.resolve("utf-8")
    assert resolved.unicode is True
    assert resolved.tick == "✓"


def test_an_unknown_encoding_is_treated_as_hostile() -> None:
    """A name Python cannot look up is not one to bet the output on."""
    assert glyphs.Glyphs.resolve("not-a-real-codec").unicode is False
    assert glyphs.Glyphs.resolve(None).unicode is False


@pytest.mark.parametrize("encoding", LEGACY)
def test_every_ascii_glyph_survives_its_own_codepage(encoding: str) -> None:
    resolved = glyphs.Glyphs.resolve(encoding)
    for value in (
        resolved.tick,
        resolved.cross,
        resolved.arrow,
        resolved.pointer,
        resolved.bullet,
        resolved.bar,
        resolved.dash,
    ):
        value.encode(encoding)


def test_the_probe_covers_every_character_the_package_prints() -> None:
    """The invariant `glyphs.py` states about itself, enforced.

    The probe decides whether the drawn set is safe here. A character printed
    somewhere in this package but absent from the probe is the worst case:
    the probe passes on a codepage that cannot hold it, the drawn set is
    chosen, and the write raises `UnicodeEncodeError` halfway through -- after
    a command has already created half a project. `▛`, `▜` and `—` were all in
    that state.

    Source characters, not printed ones, because a literal is the only thing
    that can be checked without running every command. That over-counts by
    whatever appears in comments, which is the safe direction: the fix is
    always to add the character to the probe.
    """
    root = Path(glyphs.__file__).parent
    missing: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for char in line:
                if ord(char) > 127 and char not in glyphs._PROBE:
                    missing.setdefault(char, []).append(f"{path.name}:{number}")

    # "sí" is read from the user, never written to the console.
    missing.pop("í", None)

    assert not missing, (
        f"characters printed by this package but absent from _PROBE: "
        f"{ {c: w[:3] for c, w in missing.items()} }"
    )


# -- rendering ----------------------------------------------------------


@pytest.fixture
def legacy_ui(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Point the ui module at a cp1252 stream, glyphs and all."""
    console = _console("cp1252")
    monkeypatch.setattr(ui, "console", console)
    monkeypatch.setattr(ui, "G", glyphs.for_console(console))
    return console


# rich writes a colour escape between every styled run, so "jfast" and
# "framework" are not adjacent in the raw bytes even though they are adjacent
# on screen. Assertions should see what a reader sees.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _written(console: Console, encoding: str = "cp1252") -> str:
    console.file.flush()
    raw = console.file.buffer.getvalue().decode(encoding)  # type: ignore[attr-defined]
    return _ANSI.sub("", raw)


def test_the_banner_does_not_crash_on_cp1252(legacy_ui: Console) -> None:
    """This is the exact call that killed `jfast start` with a traceback."""
    ui.banner("the opinionated default stack")
    assert "jfastframework" in _written(legacy_ui)


def test_the_whole_toolkit_renders_on_cp1252(legacy_ui: Console) -> None:
    ui.rule("datastores")
    ui.created("demoapp/main.py", "entry point")
    ui.step("writing templates")
    ui.note("nothing to do")
    ui.warn("port 8000 is in use")
    ui.summary("demoapp", [("stack", "modular monolith")])
    ui.next_steps("Run it", [("docker compose up", "everything")])
    ui.file_tree("demoapp/", [("main.py", True), ("shared/enums.py", False)])

    out = _written(legacy_ui)
    assert "demoapp/main.py" in out
    assert "port 8000 is in use" in out
    # The tree still nests, it just draws with ASCII guides.
    assert "shared" in out and "enums.py" in out


def test_a_skipped_file_is_distinguishable_from_a_written_one() -> None:
    """The one line worth reading in a wall of forty must not look like the rest."""
    console = _console("utf-8")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ui, "console", console)
        patch.setattr(ui, "G", glyphs.for_console(console))
        ui.file_tree("demoapp/", [("main.py", True), ("jfast.toml", False)])

    out = _written(console, "utf-8")
    assert "✓ main.py" in out
    assert "exists, left alone" in out


def test_the_probe_covers_every_glyph_actually_printed() -> None:
    """A glyph missing from the probe passes the check and still crashes."""
    drawn = glyphs.Glyphs.resolve("utf-8")
    for value in (drawn.tick, drawn.cross, drawn.arrow, drawn.pointer, drawn.bullet, drawn.bar):
        assert value in glyphs._PROBE, f"{value!r} is printed but not probed"
