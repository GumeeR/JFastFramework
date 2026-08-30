"""What `jfast inspect`, `analyze` and `graph` read out of a directory.

Every test here builds a tiny project on disk rather than mocking the
filesystem: the whole point of this module is that it works on real files it
never imports, and a mock would test the mock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jfastframework import project as project_model
from jfastframework.cli import insight
from jfastframework.cli.scaffold import (
    CONTRACT_TEMPLATE_FOR,
    Scaffolder,
    module_context,
    module_trees,
)
from jfastframework.contracts import Contract, check

CONFIG = """\
[app]
name = "shop"
version = "1.2.3"
env = "local"

[plugins]
enabled = ["observability", "database"]

[modules.invoice]
layout = "layered"
ui = "api"
"""

MAIN = """\
from jfastframework import create_app

from modules.invoice import router as invoice_router

app = create_app(routers=[invoice_router])
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write(tmp_path / "jfast.toml", CONFIG)
    write(tmp_path / "main.py", MAIN)
    write(tmp_path / "contracts.toml", '[project]\nname = "shop"\n')
    write(tmp_path / "modules" / "__init__.py", "")
    write(tmp_path / "modules" / "invoice" / "__init__.py", "from .router import router\n")
    write(
        tmp_path / "modules" / "invoice" / "router.py",
        'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/invoices")\n',
    )
    write(
        tmp_path / "modules" / "invoice" / "models.py",
        'class Invoice:\n    __tablename__ = "invoices"\n',
    )
    write(tmp_path / "modules" / "invoice" / "tests" / "test_it.py", "def test_x():\n    pass\n")
    write(tmp_path / "shared" / "enums.py", "STATUS = 1\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_reads_the_project_without_importing_it(root: Path) -> None:
    project = project_model.load(root)
    assert project.name == "shop"
    assert project.version == "1.2.3"
    assert project.plugins == ("observability", "database")
    assert project.module_names == ("invoice",)


def test_module_carries_its_shape(root: Path) -> None:
    module = project_model.load(root).module("invoice")
    assert module is not None
    assert module.layout == "layered"
    assert module.ui == "api"
    assert module.route_prefixes == ("/invoices",)
    assert module.tables == ("invoices",)
    assert module.registered is True
    assert module.has_tests is True
    assert "fastapi" in module.external


def test_a_file_that_does_not_parse_does_not_stop_the_scan(root: Path) -> None:
    """A half-edited file is exactly when you want the other twenty described."""
    write(root / "modules" / "invoice" / "broken.py", "def (:\n")
    project = project_model.load(root)
    module = project.module("invoice")
    assert module is not None
    assert module.route_prefixes == ("/invoices",)


def test_relative_cross_module_import_is_resolved(root: Path) -> None:
    write(root / "modules" / "order" / "__init__.py", "")
    write(root / "modules" / "order" / "service.py", "from ..invoice.models import Invoice\n")
    module = project_model.load(root).module("order")
    assert module is not None
    assert module.imports == ("invoice",)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def codes(findings: list[project_model.Finding]) -> set[str]:
    return {finding.code for finding in findings}


def test_a_generated_project_is_clean(root: Path) -> None:
    """The one that matters most.

    A checker that reports on a brand new project is a checker people mute,
    and then it is worth nothing on the day it is right.
    """
    assert project_model.analyze(project_model.load(root)) == []


def test_cycle_is_critical(root: Path) -> None:
    write(root / "modules" / "order" / "__init__.py", "")
    write(root / "modules" / "order" / "s.py", "from modules.invoice.models import Invoice\n")
    write(root / "modules" / "invoice" / "s.py", "from modules.order.s import x\n")
    findings = project_model.analyze(project_model.load(root))
    cycle = [f for f in findings if f.code == "module-cycle"]
    assert len(cycle) == 1
    assert cycle[0].severity == "critical"


def test_unregistered_module_with_routes(root: Path) -> None:
    write(root / "modules" / "ghost" / "__init__.py", "")
    write(
        root / "modules" / "ghost" / "router.py",
        'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/ghosts")\n',
    )
    findings = project_model.analyze(project_model.load(root))
    assert "module-unregistered" in codes(findings)


def test_a_module_without_routes_is_not_reported_as_unregistered(root: Path) -> None:
    """A support module nothing serves is a design choice, not a mistake."""
    write(root / "modules" / "pricing" / "__init__.py", "")
    write(root / "modules" / "pricing" / "rules.py", "RATE = 1\n")
    findings = project_model.analyze(project_model.load(root))
    assert "module-unregistered" not in codes(findings)


def test_two_modules_on_one_prefix(root: Path) -> None:
    write(root / "modules" / "order" / "__init__.py", "")
    write(
        root / "modules" / "order" / "router.py",
        'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/invoices")\n',
    )
    findings = project_model.analyze(project_model.load(root))
    conflict = [f for f in findings if f.code == "route-conflict"]
    assert len(conflict) == 1
    assert "invoice" in conflict[0].message and "order" in conflict[0].message


def test_shared_importing_a_module(root: Path) -> None:
    write(root / "shared" / "enums.py", "from modules.invoice.models import Invoice\n")
    findings = project_model.analyze(project_model.load(root))
    reported = [f for f in findings if f.code == "shared-imports-module"]
    assert reported and reported[0].path == "shared/enums.py"


def test_missing_migration_only_once_revisions_exist(root: Path) -> None:
    """With no revisions at all the project has simply not migrated yet."""
    assert "module-no-migration" not in codes(project_model.analyze(project_model.load(root)))

    write(
        root / "migrations" / "versions" / "0001_init.py",
        'def upgrade():\n    op.create_table("customers")\n',
    )
    findings = project_model.analyze(project_model.load(root))
    assert "module-no-migration" in codes(findings)

    write(
        root / "migrations" / "versions" / "0002_invoices.py",
        'def upgrade():\n    op.create_table("invoices")\n',
    )
    assert "module-no-migration" not in codes(project_model.analyze(project_model.load(root)))


def test_unknown_plugin(root: Path) -> None:
    findings = project_model.analyze(
        project_model.load(root), known_plugins=frozenset({"observability"})
    )
    unknown = [f for f in findings if f.code == "plugin-unknown"]
    assert len(unknown) == 1
    assert "database" in unknown[0].message


# ---------------------------------------------------------------------------
# The contract, as a fact about the shape
# ---------------------------------------------------------------------------


def scaffold(tmp_path: Path, *, contract: str) -> Path:
    """A generated service with hexagonal modules and *contract*'s layer globs.

    Generated rather than assembled here. The defect was a pairing `jfast new`
    made -- modules in one layout, a contract written for another -- and a
    contract composed in this file would test the composing, not the pairing.
    """
    scaffolder = Scaffolder()
    scaffolder.render_trees(
        module_trees("hexagonal", "api", tmp_path / "modules", tmp_path),
        module_context("invoice", layout="hexagonal"),
    )
    write(tmp_path / "shared" / "enums.py", "STATUS = 1\n")
    scaffolder.render_tree(
        CONTRACT_TEMPLATE_FOR[contract],
        tmp_path,
        {"project": "billing", "layout": contract, "Project": "Billing"},
        force=True,
    )
    return tmp_path


def test_a_contract_whose_layers_govern_nothing_is_a_finding(tmp_path: Path) -> None:
    """The inconsistency this replaces.

    On this project `jfast next` said the contract check failed and `jfast
    analyze` said there was nothing to report. Both read the same directory,
    so one of them was wrong and a reader had no way to tell which.
    """
    root = scaffold(tmp_path, contract="layered")
    findings = [
        finding
        for finding in project_model.analyze(project_model.load(root))
        if finding.code == "contract-governs-nothing"
    ]
    assert findings
    assert {finding.severity for finding in findings} == {"high"}
    assert all(finding.path == "contracts.toml" for finding in findings)

    violations = check(Contract.load(root / "contracts.toml"), root)
    unmatched = [v for v in violations if v.rule == "layer-unmatched"]
    assert [f.message for f in findings] == [v.message for v in unmatched]


def test_a_contract_that_describes_its_own_tree_is_not_reported(tmp_path: Path) -> None:
    """The half that has to stay quiet.

    Same generator on both sides, which is what `jfast new module` produces
    now. A finding here would fire on every fresh project, and the check would
    be muted long before the day it was right.
    """
    root = scaffold(tmp_path, contract="hexagonal")
    assert check(Contract.load(root / "contracts.toml"), root) == []
    assert "contract-governs-nothing" not in codes(project_model.analyze(project_model.load(root)))


def test_a_project_with_no_contract_is_not_reported(root: Path) -> None:
    (root / "contracts.toml").unlink()
    assert "contract-governs-nothing" not in codes(project_model.analyze(project_model.load(root)))


def test_a_contract_that_does_not_parse_is_left_to_contracts_check(root: Path) -> None:
    """One broken contract, one report. `jfast next` already carries this one."""
    write(root / "contracts.toml", "[layers.http\npaths = [\n")
    assert "contract-governs-nothing" not in codes(project_model.analyze(project_model.load(root)))


def test_findings_are_sorted_worst_first(root: Path) -> None:
    write(root / "modules" / "order" / "__init__.py", "")
    write(root / "modules" / "order" / "s.py", "from modules.invoice.models import Invoice\n")
    write(root / "modules" / "invoice" / "s.py", "from modules.order.s import x\n")
    write(root / "helpers.py", "X = 1\n")
    severities = [f.severity for f in project_model.analyze(project_model.load(root))]
    ranks = [project_model.SEVERITY_ORDER.index(s) for s in severities]
    assert ranks == sorted(ranks)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_graph_formats(root: Path) -> None:
    write(root / "modules" / "order" / "__init__.py", "")
    write(root / "modules" / "order" / "s.py", "from modules.invoice.models import Invoice\n")
    project = project_model.load(root)

    assert "invoice" in insight.render_graph(project, output_format="ascii")
    assert "order --> invoice" in insight.render_graph(project, output_format="mermaid")
    assert '"order" -> "invoice"' in insight.render_graph(project, output_format="dot")
    assert '"from": "order"' in insight.render_graph(project, output_format="json")

    with pytest.raises(ValueError, match="unknown format"):
        insight.render_graph(project, output_format="svg")


def test_graph_can_be_narrowed_to_one_module(root: Path) -> None:
    write(root / "modules" / "order" / "__init__.py", "")
    write(root / "modules" / "pricing" / "__init__.py", "")
    project = project_model.load(root)
    rendered = insight.render_graph(project, output_format="ascii", root="order")
    assert "order" in rendered
    assert "pricing" not in rendered


def test_render_project_names_every_module(root: Path) -> None:
    project = project_model.load(root)
    rendered = insight.render_project(project, [])
    assert "invoice" in rendered
    assert "/invoices" in rendered
    assert "1.2.3" in rendered


def test_render_analysis_carries_the_fix_not_only_the_problem(root: Path) -> None:
    write(root / "shared" / "enums.py", "from modules.invoice.models import Invoice\n")
    findings = project_model.analyze(project_model.load(root))
    rendered = insight.render_analysis(findings)
    assert "shared-imports-module" in rendered
    assert "modules import shared/" in rendered


def test_severity_counts_omits_zeroes(root: Path) -> None:
    write(root / "helpers.py", "X = 1\n")
    counts = insight.severity_counts(project_model.analyze(project_model.load(root)))
    assert counts == {"low": 1}
