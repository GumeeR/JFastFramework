"""Which layout each module was generated with, remembered in ``jfast.toml``.

A modular monolith should not force every module to the same shape. A catalogue
is four files; an orders module that has to be testable without a database
wants ports and adapters. Letting each one choose is the easy half.

The hard half is that every later command then has to know which choice was
made -- ``jfast new use-case`` writes to ``application/`` in a hexagonal module
and nowhere at all in a layered one. Asking again each time gets a different
answer eventually, and guessing from the folders on disk breaks the moment
somebody adds a folder.

So the choice is written down once, next to the rest of the service's
configuration. ``jfast.toml`` rather than a new file because there is already
one config file per service and two would be one too many; the runtime ignores
the table (``extra="ignore"``, and the raw document is kept intact), so nothing
has to change to tolerate it.

Written by hand rather than with a TOML serialiser: the standard library reads
TOML and does not write it, and a whole dependency for four lines of key-value
is a poor trade. The writer only ever appends a table or replaces one it wrote,
so it cannot reformat the rest of the file.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

CONFIG_FILE = "jfast.toml"


def _config_path(root: Path) -> Path:
    return root / CONFIG_FILE


def read_all(root: Path) -> dict[str, dict[str, Any]]:
    """Every recorded module, or an empty mapping if there is nothing to read.

    Unreadable is treated as empty on purpose: a malformed ``jfast.toml`` is a
    problem for the command that loads the config, and failing here would turn
    it into a confusing error from an unrelated generator.
    """
    path = _config_path(root)
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    modules = raw.get("modules")
    if not isinstance(modules, dict):
        return {}
    return {name: entry for name, entry in modules.items() if isinstance(entry, dict)}


def layout_of(root: Path, module: str) -> str | None:
    """The layout ``module`` was generated with, if it was recorded."""
    entry = read_all(root).get(module)
    if entry is None:
        return None
    layout = entry.get("layout")
    return layout if isinstance(layout, str) else None


def record(root: Path, module: str, *, layout: str, ui: str) -> bool:
    """Remember how ``module`` was generated. Returns whether the file changed.

    Idempotent, and safe to call on a module that already exists: an existing
    table for the same module is replaced rather than duplicated, because two
    ``[modules.order]`` tables is a TOML parse error and would break the
    service at boot.
    """
    path = _config_path(root)
    if not path.is_file():
        return False

    content = path.read_text(encoding="utf-8")
    block = f'[modules.{module}]\nlayout = "{layout}"\nui = "{ui}"\n'

    # An existing table for this module: from its header up to the next header
    # or the end of the file.
    existing = re.compile(
        rf"^\[modules\.{re.escape(module)}\]\n(?:(?!^\[).*\n)*",
        re.MULTILINE,
    )
    if existing.search(content):
        updated = existing.sub(block, content, count=1)
    else:
        header = (
            "\n# How each module was generated, so later commands know where a\n"
            "# new file belongs. Written by `jfast new module`.\n"
            if "[modules." not in content
            else "\n"
        )
        updated = content.rstrip("\n") + "\n" + header + block

    if updated == content:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


__all__ = ["CONFIG_FILE", "layout_of", "read_all", "record"]
