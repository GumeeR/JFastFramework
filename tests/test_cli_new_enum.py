"""`jfast new enum` has to write into the layer the layout actually uses.

A hexagonal module keeps its vocabulary in `domain/`, and the generated
contract scopes that layer to `modules/*/domain/*.py`. An enum dropped at the
module root matches no layer glob at all, so `may_import = ["shared"]` and the
`forbid_packages` list covering sqlalchemy, fastapi, httpx and redis never
reach it. It imports fine, `contracts check` says nothing, and the file is
silently outside the architecture -- which is the failure mode that makes it
worth a test rather than a reading.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jfastframework.cli.main import app

runner = CliRunner()

# Where each layout's own template puts enums.py, which is the only authority
# on this: module_hexagonal ships {{module}}/domain/enums.py, the other three
# ship {{module}}/enums.py.
LAYOUTS = [
    ("layered", "modules/order/enums.py"),
    ("modular", "modules/order/enums.py"),
    ("screaming", "modules/order/enums.py"),
    ("hexagonal", "modules/order/domain/enums.py"),
]

CONFIG = """\
[app]
name = "shop"
version = "0.1.0"
env = "local"
port = 8000

[plugins]
enabled = []
disabled = []

[modules.order]
layout = "{layout}"
ui = "api"
"""

# The directories each layout's module tree actually creates.
TREE = {
    "layered": [],
    "modular": ["api", "models", "repositories", "services", "validations"],
    "screaming": ["use_cases"],
    "hexagonal": ["domain", "application", "infrastructure", "adapters"],
}


def _service(tmp_path: Path, layout: str) -> Path:
    root = tmp_path / "shop"
    (root / "modules" / "order").mkdir(parents=True)
    (root / "shared").mkdir()
    for folder in TREE[layout]:
        (root / "modules" / "order" / folder).mkdir()
    (root / "jfast.toml").write_text(CONFIG.format(layout=layout), encoding="utf-8")
    return root


@pytest.mark.parametrize(("layout", "expected"), LAYOUTS)
def test_the_enum_lands_in_the_layer_the_layout_uses(
    layout: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _service(tmp_path, layout)
    monkeypatch.chdir(root)

    result = runner.invoke(
        app, ["new", "enum", "OrderStatus", "--module", "order", "--values", "DRAFT,PAID"]
    )

    assert result.exit_code == 0, result.output
    assert (root / expected).is_file(), f"{layout}: not at {expected}\n{result.output}"
    assert "class OrderStatus(" in (root / expected).read_text(encoding="utf-8")


@pytest.mark.parametrize(("layout", "expected"), LAYOUTS)
def test_the_import_path_it_prints_is_the_one_that_works(
    layout: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _service(tmp_path, layout)
    monkeypatch.chdir(root)

    result = runner.invoke(
        app, ["new", "enum", "OrderStatus", "--module", "order", "--values", "DRAFT"]
    )

    dotted = expected[: -len(".py")].replace("/", ".")
    assert f"from {dotted} import OrderStatus" in result.output, result.output


def test_an_unrecorded_hexagonal_module_is_read_off_its_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Modules generated before the layout was recorded still have to land
    # correctly: the only signal left is the tree on disk.
    root = _service(tmp_path, "hexagonal")
    (root / "jfast.toml").write_text(
        CONFIG.format(layout="hexagonal").split("[modules.order]")[0], encoding="utf-8"
    )
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["new", "enum", "OrderStatus", "--module", "order"])

    assert result.exit_code == 0, result.output
    assert (root / "modules" / "order" / "domain" / "enums.py").is_file(), result.output


def test_shared_is_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _service(tmp_path, "hexagonal")
    monkeypatch.chdir(root)

    result = runner.invoke(app, ["new", "enum", "Currency", "--shared", "--values", "MXN,USD"])

    assert result.exit_code == 0, result.output
    assert (root / "shared" / "enums.py").is_file()
