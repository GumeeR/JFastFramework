"""A plugin the project declares, resolved by the command CI actually runs.

``[plugins.paths]`` names a module inside the project. ``python -m
jfastframework`` puts the working directory on ``sys.path`` and the ``jfast``
console script does not, so for three releases the documented layout reported
its own plugin missing -- a false CRITICAL out of `check`, a crash out of
`workspace compose` -- while every test passed, because every test drove the
CLI in-process or through ``python -m``.

So these run the installed entry point in a subprocess. It is the only spelling
that can see the defect: ``CliRunner`` and ``-m`` both share an interpreter that
already imported the project.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - driving the installed console script is the point
import sys
from pathlib import Path

import pytest

from jfastframework.cli.exits import Code

JFAST = shutil.which(
    "jfast",
    path=os.pathsep.join(
        p for p in (str(Path(sys.executable).parent), os.environ.get("PATH")) if p
    ),
)

pytestmark = pytest.mark.skipif(
    JFAST is None, reason="jfast is not installed as a console script in this environment"
)

PLUGIN_MODULE = """\
from __future__ import annotations

from jfastframework.plugins.base import Plugin, PluginMeta


class SocialRuntimePlugin(Plugin):
    meta = PluginMeta(
        name="social_runtime",
        version="1.0.0",
        description="Declared in jfast.toml, not installed as a distribution.",
    )
"""

SERVICE_CONFIG = """\
[app]
name = "social"
port = 8010

[plugins]
enabled = ["social_runtime"]

[plugins.paths]
social_runtime = "social_plugins.runtime:SocialRuntimePlugin"
"""

MISSING_CONFIG = """\
[app]
name = "ghost"

[plugins]
enabled = ["ghost_runtime"]

[plugins.paths]
ghost_runtime = "ghost_plugins.runtime:GhostPlugin"
"""


def run(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """The console script, in a process that inherits nothing importable.

    ``PYTHONPATH`` is cleared on purpose: leaving it set would let the harness
    put the project on ``sys.path`` and hide exactly what is under test.
    """
    assert JFAST is not None
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(  # nosec B603 - fixed argv, no shell
        [JFAST, *arguments],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def write_service(root: Path, config: str = SERVICE_CONFIG) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "jfast.toml").write_text(config, encoding="utf-8")
    package = root / "social_plugins"
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "runtime.py").write_text(PLUGIN_MODULE, encoding="utf-8")
    return root


@pytest.fixture
def service(tmp_path: Path) -> Path:
    """The layout the plugin documentation tells people to write."""
    return write_service(tmp_path / "social")


def test_check_passes_on_a_plugin_the_project_declares(service: Path) -> None:
    result = run("check", "--only", "plugins", cwd=service)
    assert result.returncode == Code.OK, result.stdout + result.stderr
    assert "social_plugins" not in result.stdout, result.stdout


def test_the_console_script_and_python_m_agree(service: Path) -> None:
    """The defect itself: two spellings of one command, two different answers."""
    script = run("check", "--only", "plugins", cwd=service)
    module = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-m", "jfastframework", "check", "--only", "plugins"],
        cwd=str(service),
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    assert script.returncode == module.returncode, (
        f"jfast exited {script.returncode}, python -m exited {module.returncode}\n"
        f"jfast: {script.stdout}{script.stderr}\npython -m: {module.stdout}{module.stderr}"
    )


def test_doctor_resolves_the_graph(service: Path) -> None:
    result = run("doctor", cwd=service)
    assert result.returncode == Code.OK, result.stdout + result.stderr
    assert "social_runtime" in result.stdout, result.stdout


def test_analyze_does_not_call_a_declared_plugin_missing(service: Path) -> None:
    result = run("analyze", cwd=service)
    assert result.returncode == Code.OK, result.stdout + result.stderr
    assert "plugin-unknown" not in result.stdout, result.stdout


def test_plugins_list_shows_it(service: Path) -> None:
    result = run("plugins", "list", cwd=service)
    assert result.returncode == Code.OK, result.stdout + result.stderr
    assert "social_runtime" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# The half that must still fail.
# ---------------------------------------------------------------------------


@pytest.fixture
def missing(tmp_path: Path) -> Path:
    """A declaration pointing at a module that is nowhere on disk."""
    root = tmp_path / "ghost"
    root.mkdir()
    (root / "jfast.toml").write_text(MISSING_CONFIG, encoding="utf-8")
    return root


def test_check_is_still_critical_when_the_plugin_really_is_absent(missing: Path) -> None:
    """Tolerating an unimportable path must not turn the true finding off."""
    result = run("check", "--only", "plugins", cwd=missing)
    assert result.returncode == Code.ENVIRONMENT, result.stdout + result.stderr
    assert "CRITICAL" in result.stdout, result.stdout
    assert "ghost_runtime" in result.stdout, result.stdout


def test_analyze_still_reports_a_plugin_nothing_provides(missing: Path) -> None:
    result = run("analyze", cwd=missing)
    assert result.returncode == Code.VALIDATION, result.stdout + result.stderr
    assert "plugin-unknown" in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# `workspace compose` keeps the promise its own docstring makes.
# ---------------------------------------------------------------------------


WORKSPACE = """\
[workspace]
name = "shopws"
base_port = 8000

[[workspace.services]]
name = "social"
kind = "api"
port = 8010
path = "social"
"""

UNINSPECTABLE_CONFIG = """\
[app]
name = "social"
port = 8010

[plugins]
enabled = ["social_runtime"]

[plugins.paths]
social_runtime = "nowhere_at_all.runtime:SocialRuntimePlugin"
"""


def test_compose_writes_the_file_when_a_plugin_cannot_be_inspected(tmp_path: Path) -> None:
    """One service's missing extra used to take the whole workspace's file.

    The generator's own docstring promised a warning and the rest of the file.
    It got a `PluginError` traceback and no file at all.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "jfast.workspace.toml").write_text(WORKSPACE, encoding="utf-8")
    write_service(workspace / "social", config=UNINSPECTABLE_CONFIG)

    result = run("workspace", "compose", cwd=workspace)

    assert result.returncode == Code.OK, result.stdout + result.stderr
    assert "cannot inspect plugin" in result.stderr, result.stderr
    written = workspace / "docker-compose.yml"
    assert written.is_file(), result.stdout + result.stderr
    assert "social" in written.read_text(encoding="utf-8")


def test_compose_resolves_a_plugin_the_workspace_root_provides(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "jfast.workspace.toml").write_text(WORKSPACE, encoding="utf-8")
    write_service(workspace / "social")
    # `compose` runs at the workspace root, so that is where its interpreter
    # looks for a module a service names by dotted path.
    write_service(workspace)
    (workspace / "jfast.toml").unlink()

    result = run("workspace", "compose", cwd=workspace)

    assert result.returncode == Code.OK, result.stdout + result.stderr
    assert "cannot inspect plugin" not in result.stderr, result.stderr
