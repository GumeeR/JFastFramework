"""Flags that had to exist for the command to be usable at all.

Both of these were reachable only by hand-editing what the CLI generated, or
by giving up half the command: a logged-out page meant undoing the sidebar
splice, and a taken port 5173 meant `--no-web` and a second terminal.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jfastframework.cli import dev as devtools
from jfastframework.cli.main import app

runner = CliRunner()

MINIMAL_CONFIG = """\
[app]
name = "shop"
version = "0.1.0"
env = "local"
port = 8000

[plugins]
enabled = []
disabled = []
"""


@pytest.fixture
def vue_project(tmp_path: Path) -> Path:
    """The two files `jfast new view` patches, with their marker comments."""
    root = tmp_path / "web"
    (root / "src" / "router").mkdir(parents=True)
    (root / "src" / "router" / "index.js").write_text(
        "const routes = [\n  /*nuevaRuta*/\n]\nexport default routes\n", encoding="utf-8"
    )
    (root / "src" / "menuAside.js").write_text(
        "import { mdiHome } from '@mdi/js'\n\nexport default [\n  /*nuevoModulo*/\n]\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text('{"dependencies": {"vue": "^3"}}', encoding="utf-8")
    return root


def test_a_view_can_be_scaffolded_without_a_sidebar_entry(vue_project: Path) -> None:
    # A login page is routed but never listed: the entry has to be opt-out,
    # because removing it afterwards means editing generated code by hand.
    menu_before = (vue_project / "src" / "menuAside.js").read_text(encoding="utf-8")

    result = runner.invoke(
        app,
        ["new", "view", "Login", "--frontend", "vue", "--root", str(vue_project), "--no-sidebar"],
    )

    assert result.exit_code == 0, result.output
    assert (vue_project / "src" / "ModuloLogin").is_dir()
    router = (vue_project / "src" / "router" / "index.js").read_text(encoding="utf-8")
    assert "ModuloLogin" in router, "the route still has to be registered"
    assert (vue_project / "src" / "menuAside.js").read_text(encoding="utf-8") == menu_before


def test_the_sidebar_entry_is_still_the_default(vue_project: Path) -> None:
    result = runner.invoke(
        app, ["new", "view", "Facturas", "--frontend", "vue", "--root", str(vue_project)]
    )

    assert result.exit_code == 0, result.output
    menu = (vue_project / "src" / "menuAside.js").read_text(encoding="utf-8")
    assert "/facturas" in menu


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A service directory `jfast dev` accepts, with every stage but the
    servers disabled, and the servers themselves captured rather than run."""
    root = tmp_path / "shop"
    root.mkdir()
    (root / "jfast.toml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    front = tmp_path / "shop-web"
    (front / "node_modules").mkdir(parents=True)
    (front / "package.json").write_text('{"dependencies": {"vue": "^3"}}', encoding="utf-8")

    monkeypatch.setattr(devtools, "supervise", lambda processes: 0)
    monkeypatch.setattr(devtools, "terminate", lambda processes, grace=5.0: None)
    yield root


def _spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []

    def fake_spawn(command: list[str], *, cwd: Path, name: str, env: dict[str, str] | None = None):
        commands.append(command)
        return devtools.Process(name, None)  # type: ignore[arg-type]

    monkeypatch.setattr(devtools, "spawn", fake_spawn)
    return commands


def test_the_frontend_port_can_be_chosen(
    service: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Port 5173 taken is the ordinary case on a machine running two projects.
    # Without this the only answer was --no-web, which is not an answer.
    commands = _spawned(monkeypatch)

    result = runner.invoke(
        app,
        [
            "dev",
            "--path",
            str(service),
            "--frontend",
            str(tmp_path / "shop-web"),
            "--no-infra",
            "--no-migrate",
            "--web-port",
            "5199",
        ],
    )

    assert result.exit_code == 0, result.output
    web = next((c for c in commands if c[:1] == ["npm"]), None)
    assert web is not None, f"the frontend was never started: {commands}"
    assert "--port" in web and "5199" in web, web


def test_the_frontend_port_is_reported(
    service: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spawned(monkeypatch)

    result = runner.invoke(
        app,
        [
            "dev",
            "--path",
            str(service),
            "--frontend",
            str(tmp_path / "shop-web"),
            "--no-infra",
            "--no-migrate",
            "--web-port",
            "5199",
        ],
    )

    assert "5199" in result.output, result.output
