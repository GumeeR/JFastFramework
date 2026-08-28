"""Marker patching: idempotent, loud, and marker-preserving.

These are the three properties the naive version of this gets wrong, and each
failure mode is silent in a way that costs an afternoon: duplicate routes, a
blank page from a no-op, or a marker consumed so the next module has nowhere
to go.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jfastframework.cli.patcher import (
    PatchError,
    ensure_import,
    ensure_named_import,
    insert_at_marker,
)

ROUTER = """import { createRouter } from 'vue-router'

const routes = [
  { path: '/', name: 'home' },
  /*nuevaRuta*/
]

export default routes
"""

MENU = """import { mdiHomeOutline } from '@mdi/js'

export default [
  { to: '/', icon: mdiHomeOutline, label: 'Home' },
  /*nuevoModulo*/
]
"""


def write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_a_block_lands_at_the_marker(tmp_path: Path) -> None:
    path = write(tmp_path, "router.js", ROUTER)
    result = insert_at_marker(path, "nuevaRuta", "...ModuloFacturas,", guard="...ModuloFacturas,")

    assert result.changed
    assert "...ModuloFacturas," in path.read_text(encoding="utf-8")


def test_the_marker_survives_so_the_next_module_has_a_home(tmp_path: Path) -> None:
    path = write(tmp_path, "router.js", ROUTER)
    insert_at_marker(path, "nuevaRuta", "...ModuloA,", guard="...ModuloA,")
    insert_at_marker(path, "nuevaRuta", "...ModuloB,", guard="...ModuloB,")

    content = path.read_text(encoding="utf-8")
    assert "...ModuloA," in content
    assert "...ModuloB," in content
    assert content.count("/*nuevaRuta*/") == 1


def test_running_twice_does_not_duplicate(tmp_path: Path) -> None:
    path = write(tmp_path, "menu.js", MENU)
    entry = "{ to: '/facturas', label: 'Facturas' },"

    first = insert_at_marker(path, "nuevoModulo", entry, guard="to: '/facturas'")
    second = insert_at_marker(path, "nuevoModulo", entry, guard="to: '/facturas'")

    assert first.changed and not second.changed
    assert path.read_text(encoding="utf-8").count("'/facturas'") == 1


def test_a_missing_marker_raises_instead_of_doing_nothing(tmp_path: Path) -> None:
    path = write(tmp_path, "router.js", "const routes = []\n")
    with pytest.raises(PatchError, match="marker /\\*nuevaRuta\\*/ not found"):
        insert_at_marker(path, "nuevaRuta", "x", guard="x")


def test_a_missing_file_raises_with_the_path(tmp_path: Path) -> None:
    with pytest.raises(PatchError, match="does not exist"):
        insert_at_marker(tmp_path / "nope.js", "nuevaRuta", "x", guard="x")


def test_a_reformatted_marker_still_matches(tmp_path: Path) -> None:
    # Someone runs prettier and the marker becomes /* nuevaRuta */. Matching
    # strictly would turn that into a silent no-op.
    path = write(tmp_path, "router.js", ROUTER.replace("/*nuevaRuta*/", "/* nuevaRuta */"))
    result = insert_at_marker(path, "nuevaRuta", "...ModuloA,", guard="...ModuloA,")
    assert result.changed


def test_an_import_is_added_after_the_existing_ones(tmp_path: Path) -> None:
    path = write(tmp_path, "router.js", ROUTER)
    statement = "import { ModuloFacturas } from '@/ModuloFacturas/Routes/router.js'"
    result = ensure_import(path, statement)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert result.changed
    assert lines[1] == statement


def test_an_import_is_not_added_twice(tmp_path: Path) -> None:
    path = write(tmp_path, "router.js", ROUTER)
    statement = "import { ModuloFacturas } from '@/ModuloFacturas/Routes/router.js'"
    ensure_import(path, statement)
    result = ensure_import(path, statement)

    assert not result.changed
    assert path.read_text(encoding="utf-8").count(statement) == 1


def test_a_named_import_joins_the_existing_statement(tmp_path: Path) -> None:
    path = write(tmp_path, "menu.js", MENU)
    result = ensure_named_import(path, "@mdi/js", "mdiViewDashboardOutline")

    content = path.read_text(encoding="utf-8")
    assert result.changed
    # One import of @mdi/js, not two.
    assert content.count("from '@mdi/js'") == 1
    assert "mdiHomeOutline, mdiViewDashboardOutline" in content


def test_a_named_import_already_present_is_left_alone(tmp_path: Path) -> None:
    path = write(tmp_path, "menu.js", MENU)
    result = ensure_named_import(path, "@mdi/js", "mdiHomeOutline")
    assert not result.changed


def test_a_named_import_falls_back_to_a_new_statement(tmp_path: Path) -> None:
    path = write(tmp_path, "menu.js", "export default []\n")
    result = ensure_named_import(path, "@mdi/js", "mdiHomeOutline")

    assert result.changed
    assert "import { mdiHomeOutline } from '@mdi/js'" in path.read_text(encoding="utf-8")
