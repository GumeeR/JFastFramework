"""Contracts: what they catch, what they deliberately do not.

A checker that cries wolf gets an ignore file within a week, so the
false-negative tests below matter as much as the positive ones.
"""

from __future__ import annotations

from pathlib import Path

from jfastframework.contracts import Contract, check, render, waivers
from jfastframework.contracts.checker import layer_matches

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


# -- coverage -----------------------------------------------------------


def test_the_files_each_layer_governs_are_counted(tmp_path: Path) -> None:
    contract, root = build(
        tmp_path,
        {
            "modules/order/domain.py": "",
            "modules/order/http.py": "",
            "modules/order/use_cases/create.py": "",
            "modules/order/use_cases/cancel.py": "",
        },
    )
    counts = layer_matches(contract, root)
    assert counts == {"domain": 1, "use_cases": 2, "storage": 0, "http": 1}


def test_a_contract_aimed_at_another_layout_is_reported_not_passed(tmp_path: Path) -> None:
    """The defect, at the level the checker can see it.

    Every layer glob names a file this tree does not have, so no rule applied
    to anything and `check` reported a clean pass. That report is worse than no
    check at all: it is the reason nobody looked for four releases.
    """
    contract, root = build(
        tmp_path,
        {
            "modules/order/adapters/api.py": "import sqlalchemy\n",
            "modules/order/domain/entities.py": "",
        },
    )
    violations = check(contract, root)
    assert rules(violations) == ["layer-unmatched"] * 4
    assert "matched 0 of the 2 file(s) under modules/" in violations[0].message
    assert "modules/order/adapters/api.py" in violations[0].message


def test_a_layer_with_no_files_yet_is_not_a_violation(tmp_path: Path) -> None:
    # Half the layers of a fresh service are empty because the code is not
    # written yet. Failing on that would make a one-module service unbuildable,
    # which is how a real check gets an ignore file within a week.
    contract, root = build(tmp_path, {"modules/order/domain.py": "class Order:\n    pass\n"})
    assert layer_matches(contract, root)["http"] == 0
    assert check(contract, root) == []


def test_a_layer_shadowed_by_a_more_specific_one_counts_as_empty(tmp_path: Path) -> None:
    # `layer_for` decides which rules apply, so it is what coverage counts. A
    # layer whose every match is taken by a narrower pattern enforces nothing,
    # and glob counting would report it as busy.
    narrow = '\n[layers.narrow]\npaths = ["modules/order/domain.py"]\nmay_import = []\n'
    contract, root = build(tmp_path, {"modules/order/domain.py": ""}, contract=CONTRACT + narrow)
    assert layer_matches(contract, root)["domain"] == 0
    assert layer_matches(contract, root)["narrow"] == 1


# -- how a layer path matches -------------------------------------------

#: The layered contract's storage layer, over a hexagonal tree. This is the
#: shape the whole `layer-unmatched` finding was built for, and the one
#: `fnmatch` used to hide: `*` translated to `.*`, which crosses a `/`.
LAYERED_OVER_HEXAGONAL = """
[project]
name = "billing"

[layers.http]
paths = ["modules/*/router.py"]
may_import = []

[layers.storage]
paths = ["modules/*/repository.py", "modules/*/models.py"]
may_import = []
"""


def test_a_layer_path_star_does_not_cross_a_directory(tmp_path: Path) -> None:
    """`modules/*/repository.py` is one directory deep, and says so.

    The three separate bug reports this closes were all the same sentence:
    the layered `storage` layer claimed `infrastructure/repository.py`, so it
    counted as governing something and the contract passed while enforcing
    nothing on either tree.
    """
    contract, _ = build(tmp_path, {}, contract=LAYERED_OVER_HEXAGONAL)
    assert contract.layer_for("modules/invoice/infrastructure/repository.py") is None
    matched = contract.layer_for("modules/invoice/repository.py")
    assert matched is not None and matched.name == "storage"


def test_a_layered_contract_over_a_hexagonal_tree_reports_every_layer(tmp_path: Path) -> None:
    """The half that can break, by the path a user takes: `contracts check`.

    Every layer of this contract names a file the tree does not have, so every
    one of them has to be reported. Before `*` stopped at `/`, `storage`
    matched `infrastructure/repository.py` and was quietly omitted -- one layer
    short of the truth, and the check still said the contract governed
    something.
    """
    contract, root = build(
        tmp_path,
        {
            "modules/invoice/infrastructure/repository.py": "",
            "modules/invoice/adapters/router.py": "",
            "modules/invoice/domain/entities.py": "",
        },
        contract=LAYERED_OVER_HEXAGONAL,
    )
    reported = {
        v.message.split("'")[1] for v in check(contract, root) if v.rule == "layer-unmatched"
    }
    assert reported == {"http", "storage"}


def test_a_double_star_still_crosses_directories(tmp_path: Path) -> None:
    """`**` is the way to say "at any depth", and it stays that way.

    Tightening `*` without leaving `**` behind would mean a contract that wants
    a whole subtree has no way left to say so.
    """
    deep = """
[project]
name = "billing"

[layers.storage]
paths = ["modules/**/repository.py"]
may_import = []
"""
    contract, _ = build(tmp_path, {}, contract=deep)
    for relative in (
        "modules/invoice/repository.py",
        "modules/invoice/infrastructure/repository.py",
        "modules/invoice/a/b/c/repository.py",
    ):
        assert contract.layer_for(relative) is not None, relative


def test_the_screaming_catch_all_no_longer_claims_a_modules_test_file(tmp_path: Path) -> None:
    """`modules/*/[!_]*.py` is the shipped pattern most affected, so it is asserted.

    A negated class must not become a way back across the separator: with
    `fnmatch`, `[!_]*` swallowed `tests/test_invoice.py` and the domain layer
    -- which forbids pydantic and may import only shared/ -- was applied to
    every test file in the project.
    """
    screaming = """
[project]
name = "billing"

[layers.domain]
paths = ["modules/*/[!_]*.py"]
may_import = ["shared"]
"""
    contract, _ = build(tmp_path, {}, contract=screaming)
    assert contract.layer_for("modules/invoice/tests/test_invoice.py") is None
    assert contract.layer_for("modules/invoice/__init__.py") is None
    claimed = contract.layer_for("modules/invoice/invoice.py")
    assert claimed is not None and claimed.name == "domain"


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
