"""What `jfast upgrade --check` promises, and the one promise that matters.

Every test builds a real project on disk. The command reads files and reports
on what it found there, so a mocked filesystem would test the mock.

The test that keeps this command worth reading is
`test_a_project_without_auth_is_never_told_about_refresh_tokens`. A report that
lists changes the project cannot be affected by is release notes with extra
steps, and after the second irrelevant warning nobody reads the first one
either.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from jfastframework import upgrades
from jfastframework.cli.exits import Code
from jfastframework.cli.upgrade import register

runner = CliRunner()

#: Rich decides where the escape codes go, and that decision moves between
#: versions. Anything asserting on what a reader sees has to read through it.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(rendered: str) -> str:
    """The help as text: no styling, no soft-wrap artefacts."""
    return re.sub(r"\s+", " ", _ANSI.sub("", rendered))


def build() -> typer.Typer:
    """A Typer app carrying nothing but this command.

    `main.py` is wired by hand at integration time; a test that imported it
    would fail for reasons belonging to a sibling command.
    """
    app = typer.Typer()
    register(app)

    # Typer collapses a one-command app into a bare callback, and `upgrade`
    # stops being a name you can type. The real app has dozens of commands;
    # this stands in for them so the invocation under test is the real one.
    @app.command("filler")
    def _filler() -> None:  # pragma: no cover
        pass

    return app


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


CONFIG = """\
[app]
name = "billing"
version = "0.1.0"
env = "local"

[plugins]
enabled = ["observability", "database"]
disabled = []
"""

AUTH_CONFIG = """\
[app]
name = "billing"
version = "0.1.0"
env = "local"

[plugins]
enabled = ["observability", "database", "auth"]
disabled = []

[plugin.auth]
mode = "jwks"
issue_tokens = true
"""

# `may_import` without "shared" is exactly what 0.1.0a3 generated.
CONTRACTS_A3 = """\
[project]
name = "billing"

[layers.http]
paths = ["modules/*/router.py"]
may_import = ["service", "schemas"]

[layers.service]
paths = ["modules/*/service.py"]
may_import = ["storage", "schemas"]

[layers.schemas]
paths = ["modules/*/schemas.py"]
may_import = []

[layers.shared]
paths = ["shared/*.py"]
may_import = []
"""

MODELS = """\
from jfastframework.db import Base, TimestampMixin


class Invoice(Base, TimestampMixin):
    __tablename__ = "invoices"
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A service pinned to 0.1.0a3, with timestamps and a stale contract."""
    write(tmp_path / "jfast.toml", CONFIG)
    write(tmp_path / "requirements.txt", "jfastframework[db,server]==0.1.0a3\n")
    write(tmp_path / "contracts.toml", CONTRACTS_A3)
    write(tmp_path / "main.py", "from modules.invoice import router\n")
    write(tmp_path / "modules" / "invoice" / "__init__.py", "")
    write(tmp_path / "modules" / "invoice" / "models.py", MODELS)
    return tmp_path


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def test_prerelease_numbers_compare_numerically() -> None:
    # The string comparison this replaces reads "0.1.0a10" < "0.1.0a9".
    assert upgrades.parse_version("0.1.0a10") > upgrades.parse_version("0.1.0a9")


def test_ordering_matches_packaging() -> None:
    """The vendored key has to agree with the reference implementation.

    `packaging` is not a runtime dependency of this framework, so the shipped
    code cannot import it -- but it is installed for development, which makes
    it usable as an oracle here.
    """
    packaging_version = pytest.importorskip("packaging.version")
    corpus = [
        "0.1.0a1",
        "0.1.0a2",
        "0.1.0a9",
        "0.1.0a10",
        "0.1.0b1",
        "0.1.0rc1",
        "0.1.0",
        "0.1.0.post1",
        "0.1.1",
        "0.2.0.dev1",
        "0.2.0",
        "1.0",
        "1.0.0",
    ]
    ours = sorted(corpus, key=upgrades.parse_version)
    theirs = sorted(corpus, key=packaging_version.Version)
    assert ours == theirs


def test_an_unparseable_version_is_rejected() -> None:
    with pytest.raises(ValueError):
        upgrades.parse_version("not-a-version")


# ---------------------------------------------------------------------------
# Finding the version the project pins
# ---------------------------------------------------------------------------


def test_the_pin_comes_from_requirements(project: Path) -> None:
    found = upgrades.pinned_version(project)
    assert found is not None
    assert found.version == "0.1.0a3"
    assert found.source == "requirements.txt"


def test_the_stamp_is_the_fallback(tmp_path: Path) -> None:
    """A project with no requirements file still stamps what scaffolded it."""
    write(tmp_path / "jfast.toml", CONFIG)
    write(
        tmp_path / ".jfast-template",
        '{"templates": {"service_base": {"framework_version": "0.1.0a3"}}}',
    )
    found = upgrades.pinned_version(tmp_path)
    assert found is not None
    assert found.version == "0.1.0a3"
    assert found.source == ".jfast-template"


def test_no_pin_at_all_is_a_config_error(tmp_path: Path) -> None:
    write(tmp_path / "jfast.toml", CONFIG)
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(tmp_path)])
    assert result.exit_code == Code.CONFIG, result.output


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_a_project_on_the_installed_version_has_nothing_to_report(project: Path) -> None:
    from jfastframework import __version__

    write(project / "requirements.txt", f"jfastframework[db,server]=={__version__}\n")
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert result.exit_code == Code.OK, result.output


def test_changes_that_apply_exit_with_the_compatibility_code(project: Path) -> None:
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert result.exit_code == Code.COMPATIBILITY, result.output


def test_the_timestamp_migration_names_the_real_table(project: Path) -> None:
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "ALTER TABLE invoices" in result.output
    # Without USING, PostgreSQL converts through the implicit cast and shifts
    # the whole table on any server whose TimeZone is not UTC.
    assert "USING created_at AT TIME ZONE 'UTC'" in result.output


def test_a_project_with_no_timestamps_is_not_told_to_migrate(project: Path) -> None:
    (project / "modules" / "invoice" / "models.py").write_text(
        'class Invoice:\n    __tablename__ = "invoices"\n', encoding="utf-8"
    )
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "ALTER TABLE" not in result.output


def test_layers_missing_shared_are_named(project: Path) -> None:
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    for layer in ("http", "service", "schemas"):
        assert layer in result.output
    # `shared` itself must never appear as an offender: an empty may_import is
    # the entry that makes every other one safe.
    assert "layers.shared" not in result.output


def test_fixing_the_contract_removes_the_finding(project: Path) -> None:
    before = runner.invoke(build(), ["upgrade", "--check", "--json", "--path", str(project)])
    assert "contracts-shared-import" in before.output

    (project / "contracts.toml").write_text(
        CONTRACTS_A3.replace('may_import = ["service", "schemas"]', 'may_import = ["shared"]')
        .replace('may_import = ["storage", "schemas"]', 'may_import = ["shared"]')
        .replace('[layers.schemas]\npaths = ["modules/*/schemas.py"]\nmay_import = []', ""),
        encoding="utf-8",
    )
    after = runner.invoke(build(), ["upgrade", "--check", "--json", "--path", str(project)])
    assert "contracts-shared-import" not in after.output


# ---------------------------------------------------------------------------
# Not reporting what cannot apply. The point of the whole command.
# ---------------------------------------------------------------------------


def test_a_project_without_auth_is_never_told_about_refresh_tokens(project: Path) -> None:
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "refresh" not in result.output.lower()
    assert "logout" not in result.output.lower()


def test_auth_without_issue_tokens_is_still_not_told(project: Path) -> None:
    """Enabling the plugin is not enough: it has to be the thing minting tokens."""
    write(
        project / "jfast.toml",
        AUTH_CONFIG.replace("issue_tokens = true", "issue_tokens = false"),
    )
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "refresh" not in result.output.lower()


def test_a_token_issuing_project_is_told(project: Path) -> None:
    write(project / "jfast.toml", AUTH_CONFIG)
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "refresh" in result.output.lower()
    assert "/auth/logout" in result.output


def test_explicit_limits_suppress_the_defaults_notice(project: Path) -> None:
    write(
        project / "jfast.toml",
        CONFIG.replace(
            'env = "local"', 'env = "local"\nmax_body_bytes = 1024\nrequest_timeout = 5.0'
        ),
    )
    result = runner.invoke(build(), ["upgrade", "--check", "--json", "--path", str(project)])
    assert "request-limit-defaults" not in result.output


def test_the_storage_defaults_are_the_ones_reported(project: Path) -> None:
    write(project / "jfast.toml", CONFIG.replace('"database"', '"database", "storage"'))
    result = runner.invoke(build(), ["upgrade", "--check", "--path", str(project)])
    assert "26214400" in result.output
    assert "120" in result.output


def test_the_exit_code_change_is_unconditional(project: Path) -> None:
    """Nothing on disk says whether CI branches on an exit code, so it is stated."""
    change = next(c for c in upgrades.CHANGES if c.code == "cli-exit-codes")
    assert change.detect is None


# ---------------------------------------------------------------------------
# Shape of the output
# ---------------------------------------------------------------------------


def test_json_is_machine_readable(project: Path) -> None:
    import json

    result = runner.invoke(build(), ["upgrade", "--check", "--json", "--path", str(project)])
    payload = json.loads(result.output)
    assert payload["from"] == "0.1.0a3"
    assert payload["ok"] is False
    codes = {change["code"] for change in payload["changes"]}
    assert "timestamps-timezone-aware" in codes
    assert "refresh-tokens-rejected" not in codes
    for change in payload["changes"]:
        assert set(change) >= {"version", "kind", "code", "summary", "detail", "remedy", "affected"}


def test_apply_is_out_of_scope_and_says_so() -> None:
    """The help says the flag exists and is not implemented.

    Read through `_plain`: `result.output` is rich's rendering, and rich is
    free to put an escape code between the dashes and the word. Searching the
    styled string for a literal `--apply` tests the colour scheme, and it fails
    on some interpreters and not others for that reason alone. What this cares
    about is the text a reader sees.
    """
    # Both axes pinned. COLUMNS stops rich soft-wrapping the flag across a
    # line -- which no amount of escape-stripping can put back together --
    # and _plain reads through the styling.
    result = runner.invoke(build(), ["upgrade", "--help"], env={"COLUMNS": "200"})
    help_text = _plain(result.output)
    assert "--apply" in help_text, help_text
    assert "not implemented" in help_text.lower(), help_text


def test_apply_is_refused(project: Path) -> None:
    result = runner.invoke(build(), ["upgrade", "--apply", "--path", str(project)])
    assert result.exit_code == Code.USAGE, result.output
