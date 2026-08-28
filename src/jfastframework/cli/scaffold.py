"""File generation from Jinja2 templates.

Templates live as real files under ``jfastframework/templates/``. Not as string
literals inside Python functions -- that was the v0 mistake: templates you
cannot lint, diff or test.

Two Jinja environments, because HTML templates are themselves Jinja::

    *.py.j2     scaffold-time vars use {{ }}
    *.html.j2   scaffold-time vars use [[ ]], so the runtime {{ }} the browser
                template needs passes through untouched

Every generated tree carries a ``.jfast-template`` stamp recording which
template produced it, so ``jfast upgrade`` can later re-apply a newer template
and show a diff instead of a rewrite.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "templates"
STAMP_FILE = ".jfast-template"

MODULE_LAYOUTS = ("layered", "screaming")
MODULE_UIS = ("api", "htmx")
SERVICE_KINDS = ("api", "web")

_SNAKE_RE = re.compile(r"(?<!^)(?=[A-Z])")


def to_snake(name: str) -> str:
    cleaned = re.sub(r"[\s\-]+", "_", name.strip())
    return _SNAKE_RE.sub("_", cleaned).lower().replace("__", "_")


def to_pascal(name: str) -> str:
    return "".join(part.capitalize() for part in to_snake(name).split("_") if part)


def to_kebab(name: str) -> str:
    return to_snake(name).replace("_", "-")


def pluralize(word: str) -> str:
    """Naive English pluralisation, good enough for table names.

    It also sidesteps a real problem: singular nouns collide with SQL reserved
    words far more often than plurals do (``order``, ``user``, ``group``).
    Override with ``--table`` when it guesses wrong.
    """
    if word.endswith("y") and not word.endswith(("ay", "ey", "iy", "oy", "uy")):
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


@dataclass
class WrittenFile:
    path: Path
    created: bool


class Scaffolder:
    def __init__(self, template_root: Path = TEMPLATE_ROOT) -> None:
        self.template_root = template_root
        loader = FileSystemLoader(str(template_root))
        common: dict[str, Any] = {
            "loader": loader,
            "undefined": StrictUndefined,
            "keep_trailing_newline": True,
            "trim_blocks": True,
            "lstrip_blocks": True,
        }
        self.env = Environment(**common)
        # Alternate delimiters so browser-side Jinja survives scaffolding.
        self.html_env = Environment(
            **common,
            variable_start_string="[[",
            variable_end_string="]]",
            block_start_string="[%",
            block_end_string="%]",
            comment_start_string="[#",
            comment_end_string="#]",
        )

    def _env_for(self, relative: Path) -> Environment:
        name = relative.name
        return self.html_env if ".html" in name or ".htm." in name else self.env

    def available_templates(self) -> list[str]:
        return sorted(p.name for p in self.template_root.iterdir() if p.is_dir())

    def render_tree(
        self,
        template: str,
        target: Path,
        context: dict[str, Any],
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> list[WrittenFile]:
        source = self.template_root / template
        if not source.is_dir():
            raise FileNotFoundError(
                f"Unknown template {template!r}. "
                f"Available: {', '.join(self.available_templates()) or '<none>'}"
            )

        written: list[WrittenFile] = []
        for path in sorted(source.rglob("*")):
            if path.is_dir() or path.name == STAMP_FILE:
                continue
            relative = path.relative_to(source)
            # Directory and file names are themselves templated, so a module
            # named "orders" lands in modules/orders/, not modules/{{module}}/.
            rendered_name = self.env.from_string(str(relative).replace(".j2", "")).render(**context)
            destination = target / rendered_name

            if destination.exists() and not force:
                written.append(WrittenFile(destination, created=False))
                continue

            env = self._env_for(relative)
            content = env.get_template(f"{template}/{relative.as_posix()}").render(**context)
            if not dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            written.append(WrittenFile(destination, created=True))

        if not dry_run:
            self._write_stamp(target, template, context)
        return written

    def render_trees(
        self,
        trees: list[tuple[str, Path]],
        context: dict[str, Any],
        *,
        force: bool = False,
        dry_run: bool = False,
    ) -> list[WrittenFile]:
        """Compose several template trees into (possibly different) targets.

        Composition instead of multiplication: a layout tree plus an optional
        UI overlay covers layered/screaming x api/htmx with three trees rather
        than four copies that drift apart.
        """
        written: list[WrittenFile] = []
        for template, target in trees:
            written.extend(
                self.render_tree(template, target, context, force=force, dry_run=dry_run)
            )
        return written

    def _write_stamp(self, target: Path, template: str, context: dict[str, Any]) -> None:
        stamp_path = target / STAMP_FILE
        existing: dict[str, Any] = {}
        if stamp_path.is_file():
            try:
                existing = json.loads(stamp_path.read_text(encoding="utf-8"))
            except ValueError:
                existing = {}
        from jfastframework import __version__

        existing.setdefault("templates", {})
        existing["templates"][template] = {
            "framework_version": __version__,
            "context": {k: v for k, v in context.items() if isinstance(v, str | int | bool)},
        }
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


def module_context(
    name: str,
    *,
    layout: str = "layered",
    ui: str = "api",
    table: str | None = None,
    modules_dir: str = "modules",
) -> dict[str, Any]:
    snake = to_snake(name)
    plural = pluralize(snake)
    return {
        "module": snake,
        "Module": to_pascal(name),
        "module_title": snake.replace("_", " ").title(),
        "module_plural": plural.replace("_", " "),
        "table": table or plural,
        "layout": layout,
        "ui": ui,
        "modules_dir": modules_dir,
    }


def service_context(name: str, *, kind: str = "api", port: int = 8000) -> dict[str, Any]:
    snake = to_snake(name)
    return {
        "service": snake,
        "Service": to_pascal(name),
        "service_slug": to_kebab(name),
        "service_title": snake.replace("_", " ").title(),
        "kind": kind,
        "port": port,
        "is_web": kind == "web",
    }


def module_trees(layout: str, ui: str, target: Path, project_root: Path) -> list[tuple[str, Path]]:
    """Which template trees to render for a module, and where."""
    if layout not in MODULE_LAYOUTS:
        raise ValueError(f"Unknown layout {layout!r}. Choose from: {', '.join(MODULE_LAYOUTS)}")
    if ui not in MODULE_UIS:
        raise ValueError(f"Unknown ui {ui!r}. Choose from: {', '.join(MODULE_UIS)}")

    trees: list[tuple[str, Path]] = [(f"module_{layout}", target)]
    if ui == "htmx":
        # The overlay writes into the project root: the HTML router goes next
        # to the module, the templates go in the service-wide templates dir.
        trees.append(("ui_htmx", project_root))
    return trees
