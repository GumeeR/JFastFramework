"""Contracts: what they catch, what they deliberately do not.

A checker that cries wolf gets an ignore file within a week, so the
false-negative tests below matter as much as the positive ones.
"""

from __future__ import annotations

from pathlib import Path

from jfastframework.contracts import Contract, check, render, waivers

CONTRACT = """
[project]
name = "billing"
owns = "Invoices."
does_not_own = "Customers."

[layers.domain]
paths = ["modules/*/domain.py"]
may_import = []
forbid_packages = ["fastapi", "sqlalchemy"]

[layers.use_cases]
paths = ["modules/*/use_cases/*.py"]
may_import = ["domain"]

[layers.storage]
paths = ["modules/*/storage.py"]
may_import = ["domain"]

[layers.http]
paths = ["modules/*/http.py"]
may_import = ["use_cases", "domain"]

[[rules.forbid_call]]
pattern = "os.getenv"
except_in = ["settings.py"]
why = "Configuration is typed."

[[rules.forbid_call]]
pattern = "print"
why = "Use the logger."

[[rules.require]]
path = "tests"
applies_to = "modules/*"
why = "Untested modules cannot be changed safely."

[invariants]
rules = ["Money is an integer in minor units."]
"""


def build(tmp_path: Path, files: dict[str, str], contract: str = CONTRACT) -> tuple[Contract, Path]:
    (tmp_path / "contracts.toml").write_text(contract, encoding="utf-8")
    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    # Every module gets a tests/ dir unless the test is about that rule.
    for module in (tmp_path / "modules").glob("*"):
        if module.is_dir() and not (module / "tests").exists():
            (module / "tests").mkdir()
    return Contract.load(tmp_path / "contracts.toml"), tmp_path


def rules(violations: list) -> list[str]:  # type: ignore[type-arg]
    return [v.rule for v in violations]


# -- layers -------------------------------------------------------------


def test_a_clean_project_has_no_violations(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/order/domain.py": "class Order:\n    pass\n",
            "modules/order/use_cases/create.py": "from ..domain import Order\n",
            "modules/order/http.py": "from .use_cases.create import *\n",
        },
    )
    assert check(contract, root) == []


def test_the_domain_may_not_import_the_framework(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/order/domain.py": "from sqlalchemy.orm import Mapped\n"},
    )
    violations = check(contract, root)
    assert "layer-package" in rules(violations)
    assert "sqlalchemy" in violations[0].message


def test_an_inward_import_is_allowed(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/order/storage.py": "from .domain import Order\n", "modules/order/domain.py": ""},
    )
    assert check(contract, root) == []


def test_an_outward_import_is_a_violation(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/order/domain.py": "from .http import router\n", "modules/order/http.py": ""},
    )
    violations = check(contract, root)
    # The domain reaching for the transport is the failure the layout exists
    # to prevent, and it is invisible in review once there are 40 modules.
    assert "layer" in rules(violations)
    assert "may import: nothing" in violations[0].message


def test_a_sideways_import_between_leaf_layers_is_a_violation(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/order/use_cases/create.py": "from ..storage import Repo\n",
            "modules/order/storage.py": "",
        },
    )
    assert "layer" in rules(check(contract, root))


def test_an_absolute_import_of_a_layer_is_resolved(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/order/domain.py": "from modules.order.storage import Repo\n",
            "modules/order/storage.py": "",
        },
    )
    assert "layer" in rules(check(contract, root))


def test_files_outside_every_layer_are_not_checked(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "scripts/backfill.py": "from modules.order.storage import Repo\nimport sqlalchemy\n",
            "modules/order/storage.py": "",
        },
    )
    # You opt a path in by naming it in a layer. Guessing would produce
    # noise on every script, migration and notebook in the repository.
    assert check(contract, root) == []


# -- calls --------------------------------------------------------------


def test_a_forbidden_call_is_reported_with_its_reason(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {"modules/order/http.py": "import os\nos.getenv('X')\n"})
    violations = check(contract, root)
    assert rules(violations) == ["forbid-call"]
    assert violations[0].why == "Configuration is typed."


def test_a_forbidden_call_is_allowed_where_the_contract_says_so(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {"settings.py": "import os\nos.getenv('X')\n"})
    assert check(contract, root) == []


def test_a_dotted_pattern_also_catches_the_bare_import(tmp_path: Path) -> None:
    # `from os import getenv` is the same mistake; the contract should not
    # have to list both spellings.
    contract, root = build(
        tmp_path, {"modules/order/http.py": "from os import getenv\ngetenv('X')\n"}
    )
    assert rules(check(contract, root)) == ["forbid-call"]


def test_a_bare_pattern_does_not_match_an_attribute_of_the_same_name(tmp_path: Path) -> None:
    contract, root = build(tmp_path, {"modules/order/http.py": "report.print()\n"})
    # Forbidding `print` must not flag `report.print()`.
    assert check(contract, root) == []


# -- requirements -------------------------------------------------------


def test_a_module_without_tests_is_reported(tmp_path: Path) -> None:
    (tmp_path / "contracts.toml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "modules" / "order").mkdir(parents=True)
    (tmp_path / "modules" / "order" / "domain.py").write_text("", encoding="utf-8")

    contract = Contract.load(tmp_path / "contracts.toml")
    violations = check(contract, tmp_path)
    assert rules(violations) == ["missing"]
    assert "tests" in violations[0].message


# -- waivers ------------------------------------------------------------


def test_a_waiver_suppresses_the_violation(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {"modules/order/domain.py": "import sqlalchemy  # contracts: allow one-off, JF-412\n"},
    )
    assert check(contract, root) == []


def test_waivers_are_listed_so_they_can_be_reviewed(tmp_path: Path) -> None:
    _, root = build(
        tmp_path,
        {"modules/order/domain.py": "import sqlalchemy  # contracts: allow one-off, JF-412\n"},
    )
    found = waivers(root)
    assert len(found) == 1
    assert found[0].message == "one-off, JF-412"


# -- model and rendering ------------------------------------------------


def test_the_contract_is_machine_readable(tmp_path: Path) -> None:
    contract, _ = build(tmp_path, {})
    described = contract.describe()
    # This is what an agent reads before writing a line.
    assert described["does_not_own"] == "Customers."
    assert described["layers"]["domain"]["may_import"] == []
    assert described["invariants"] == ["Money is an integer in minor units."]


def test_the_more_specific_pattern_wins_not_the_longer_one(tmp_path: Path) -> None:
    # `modules/*/[!_]*.py` is longer than `modules/*/storage.py`. Ranking by
    # length would classify every storage file as domain code and then reject
    # its imports for a reason that makes no sense.
    catch_all = '\n[layers.domain_all]\npaths = ["modules/*/[!_]*.py"]\nmay_import = []\n'
    contract, _ = build(tmp_path, {}, contract=CONTRACT + catch_all)

    layer = contract.layer_for("modules/order/storage.py")
    assert layer is not None and layer.name == "storage"

    # A file only the catch-all matches still lands in it.
    fallback = contract.layer_for("modules/order/pricing.py")
    assert fallback is not None and fallback.name == "domain_all"


def test_two_layers_claiming_one_path_is_a_contract_error(tmp_path: Path) -> None:
    clash = '\n[layers.other]\npaths = ["modules/*/storage.py"]\nmay_import = []\n'
    contract, root = build(tmp_path, {"modules/order/storage.py": ""}, contract=CONTRACT + clash)

    violations = check(contract, root)
    # Whichever wins the tie decides the rules, and the answer would be
    # arbitrary. Better to refuse than to be confidently wrong.
    assert rules(violations) == ["contract"]
    assert "claimed by" in violations[0].message


def test_may_import_naming_an_unknown_layer_is_a_contract_error(tmp_path: Path) -> None:
    typo = '\n[layers.extra]\npaths = ["extra/*.py"]\nmay_import = ["storge"]\n'
    contract, root = build(tmp_path, {}, contract=CONTRACT + typo)

    violations = check(contract, root)
    assert rules(violations) == ["contract"]
    assert "not a layer" in violations[0].message


def test_rendering_leads_with_what_the_service_does_not_own(tmp_path: Path) -> None:
    contract, _ = build(tmp_path, {})
    markdown = render(contract)
    assert "**Does not own:** Customers." in markdown
    assert "may import" in markdown.lower()
    assert "contracts: allow" in markdown


def test_find_walks_up_from_a_nested_directory(tmp_path: Path) -> None:
    (tmp_path / "contracts.toml").write_text(CONTRACT, encoding="utf-8")
    nested = tmp_path / "modules" / "order" / "use_cases"
    nested.mkdir(parents=True)
    assert Contract.find(nested) == tmp_path / "contracts.toml"
