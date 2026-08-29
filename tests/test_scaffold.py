"""Scaffolding: naming, tree composition, and the two Jinja environments."""

from __future__ import annotations

from pathlib import Path

import pytest

from jfastframework.cli.scaffold import (
    MODULE_LAYOUTS,
    Scaffolder,
    module_context,
    module_trees,
    pluralize,
    to_pascal,
    to_snake,
)


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("order", "orders"),
        ("invoice", "invoices"),
        ("category", "categories"),
        ("box", "boxes"),
        ("address", "addresses"),
        ("day", "days"),
        ("payment_method", "payment_methods"),
    ],
)
def test_pluralize(word: str, expected: str) -> None:
    assert pluralize(word) == expected


def test_names_are_normalised() -> None:
    assert to_snake("BillingAccount") == "billing_account"
    assert to_snake("billing account") == "billing_account"
    assert to_pascal("billing_account") == "BillingAccount"


def test_table_defaults_to_the_plural_and_dodges_reserved_words() -> None:
    # "order" is a SQL reserved word; "orders" is not.
    assert module_context("order")["table"] == "orders"


def test_table_can_be_overridden() -> None:
    assert module_context("order", table="sales_order")["table"] == "sales_order"


def test_api_ui_renders_one_tree_and_htmx_adds_the_overlay() -> None:
    target, root = Path("modules"), Path(".")
    assert module_trees("layered", "api", target, root) == [("module_layered", target)]
    assert module_trees("screaming", "htmx", target, root) == [
        ("module_screaming", target),
        ("ui_htmx", root),
    ]


def test_unknown_layout_and_ui_are_rejected() -> None:
    # "onion" rather than "hexagonal": the latter is a real layout now, and a
    # test whose invalid example quietly becomes valid stops testing anything.
    with pytest.raises(ValueError, match="Unknown layout"):
        module_trees("onion", "api", Path("m"), Path("."))
    with pytest.raises(ValueError, match="Unknown ui"):
        module_trees("layered", "vue", Path("m"), Path("."))


def test_every_layout_renders_and_exports_a_router(tmp_path: Path) -> None:
    """`router` is the name the CLI splices into main.py.

    Without it a generated module is written, registered, and then crashes the
    service on import -- which looks like a framework bug rather than a missing
    export.
    """
    scaffolder = Scaffolder()
    for layout in MODULE_LAYOUTS:
        target = tmp_path / layout
        context = module_context("order", layout=layout)
        scaffolder.render_tree(f"module_{layout}", target, context)
        init = (target / "order" / "__init__.py").read_text(encoding="utf-8")
        assert "router" in init, f"{layout} does not export router"


def test_the_original_layouts_still_expose_build_service(tmp_path: Path) -> None:
    """The shared factory is what makes the HTMX overlay layout-agnostic."""
    scaffolder = Scaffolder()
    for layout in ("layered", "screaming"):
        target = tmp_path / layout
        context = module_context("order", layout=layout)
        scaffolder.render_tree(f"module_{layout}", target, context)
        init = (target / "order" / "__init__.py").read_text(encoding="utf-8")
        assert "def build_service(" in init
        assert '"router"' in init


def test_every_layout_has_a_contract_template() -> None:
    """`jfast contracts init --layout X` must not fail for a layout we offer."""
    from jfastframework.cli.scaffold import CONTRACT_TEMPLATE_FOR, TEMPLATE_ROOT

    for layout in MODULE_LAYOUTS:
        template = CONTRACT_TEMPLATE_FOR[layout]
        assert (TEMPLATE_ROOT / template / "contracts.toml.j2").is_file(), template


def test_html_templates_keep_their_runtime_jinja(tmp_path: Path) -> None:
    scaffolder = Scaffolder()
    context = module_context("product", ui="htmx")
    scaffolder.render_tree("ui_htmx", tmp_path, context)

    row = (tmp_path / "templates" / "product" / "_row.html").read_text(encoding="utf-8")
    # Scaffold-time values were substituted, under the /ui/ prefix the HTML
    # surface uses. Sharing the prefix with the JSON router meant whichever
    # registered first answered, so the page returned JSON and the form POSTed
    # into the API handler.
    assert 'hx-delete="/ui/products/' in row
    # ...and runtime Jinja survived untouched.
    assert "{{ item.id }}" in row
    assert "{{ item.name }}" in row


def test_existing_files_are_skipped_unless_forced(tmp_path: Path) -> None:
    scaffolder = Scaffolder()
    context = module_context("order")
    scaffolder.render_tree("module_layered", tmp_path, context)

    models = tmp_path / "order" / "models.py"
    models.write_text("# edited by hand\n", encoding="utf-8")

    again = scaffolder.render_tree("module_layered", tmp_path, context)
    assert models.read_text(encoding="utf-8") == "# edited by hand\n"
    assert any(not item.created for item in again)

    scaffolder.render_tree("module_layered", tmp_path, context, force=True)
    assert models.read_text(encoding="utf-8") != "# edited by hand\n"


def test_a_stamp_records_the_template_for_future_upgrades(tmp_path: Path) -> None:
    import json

    scaffolder = Scaffolder()
    scaffolder.render_tree("module_layered", tmp_path, module_context("order"))
    stamp = json.loads((tmp_path / ".jfast-template").read_text(encoding="utf-8"))
    assert "module_layered" in stamp["templates"]
    assert stamp["templates"]["module_layered"]["context"]["table"] == "orders"
