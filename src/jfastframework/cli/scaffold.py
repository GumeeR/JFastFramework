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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from jfastframework import languages

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "templates"
STAMP_FILE = ".jfast-template"

MODULE_LAYOUTS = ("layered", "screaming")
MODULE_UIS = ("api", "htmx")
SERVICE_KINDS = ("api", "web", "spa", "gateway")
FRONTENDS = ("vue", "react")


@dataclass(frozen=True)
class PluginSpec:
    """What the installer needs to know to offer a plugin as a choice."""

    extra: str
    label: str
    # Offered by `jfast init`'s datastore prompt.
    is_datastore: bool = False


# The menu the installer shows, and the source of the extras a generated
# service pins. Keep it in step with the entry points in pyproject.toml.
PLUGIN_CATALOG: dict[str, PluginSpec] = {
    "observability": PluginSpec("", "Structured JSON logging with request ids"),
    "metrics": PluginSpec("metrics", "Prometheus RED metrics at /metrics"),
    "database": PluginSpec("db", "PostgreSQL + pgvector (SQLAlchemy, Alembic)", True),
    "cache": PluginSpec("cache", "Redis cache, pub/sub and queue", True),
    "mongo": PluginSpec("mongo", "MongoDB for document-shaped data", True),
    "qdrant": PluginSpec("qdrant", "Qdrant vector database", True),
    "rag": PluginSpec("rag", "Semantic search over pgvector or Qdrant"),
    "queue": PluginSpec("queue", "Background jobs on PostgreSQL, Redis or RabbitMQ"),
    "auth": PluginSpec("auth", "JWT verification, scopes, rotation, revocation"),
    "events": PluginSpec("kafka", "Kafka event streaming between services"),
    "web": PluginSpec("web", "Jinja2 templates + HTMX (server-rendered pages)"),
    "sentry": PluginSpec("sentry", "Sentry error and performance reporting"),
    "gateway": PluginSpec("gateway", "Prefix-based reverse proxy"),
    "storage": PluginSpec("storage", "File storage on local disks, S3 or MinIO"),
    "tenancy": PluginSpec("", "Multi-tenancy by subdomain, token claim or path"),
    "notifications": PluginSpec("fcm", "Push notifications via Firebase (FCM)"),
}

DATASTORE_PLUGINS = tuple(n for n, s in PLUGIN_CATALOG.items() if s.is_datastore)

# Always on, in this order, regardless of what else was chosen.
BASE_PLUGINS = ("observability", "metrics")

# File kinds whose own syntax uses ``{{ }}`` at runtime: Vue interpolation,
# JSX inline style objects, and server-rendered HTML. They render through the
# square-bracket environment so their braces survive scaffolding.
_RUNTIME_BRACE_SUFFIXES = (".html", ".htm.", ".vue", ".jsx", ".tsx")

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


def resolve_plugins(kind: str, chosen: Sequence[str]) -> list[str]:
    """Full, ordered plugin list for a generated service.

    Unknown names are rejected here rather than at the service's first boot,
    and `web` is forced on for a `web` service because the kind is meaningless
    without it.
    """
    unknown = [name for name in chosen if name not in PLUGIN_CATALOG]
    if unknown:
        raise ValueError(
            f"Unknown plugin(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(PLUGIN_CATALOG))}"
        )

    selected = list(BASE_PLUGINS)
    if kind == "web" and "web" not in chosen:
        selected.append("web")
    if kind == "gateway":
        selected.append("gateway")

    for name in chosen:
        if name not in selected:
            selected.append(name)

    # rag without a store is a service that boots and then fails on first use.
    if "rag" in selected and not ({"database", "qdrant"} & set(selected)):
        selected.insert(selected.index("rag"), "database")
    # Same for the queue: its default backend is PostgreSQL.
    if "queue" in selected and not ({"database", "cache"} & set(selected)):
        selected.insert(selected.index("queue"), "database")
    return selected


def extras_for(plugins: Sequence[str]) -> str:
    """The pip extras string a generated service pins."""
    extras = {"server"}
    for name in plugins:
        spec = PLUGIN_CATALOG.get(name)
        if spec and spec.extra:
            extras.add(spec.extra)
    return ",".join(sorted(extras))


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
        """Pick the delimiters that will not collide with the file's own syntax.

        Vue interpolates with ``{{ }}``, JSX inlines style objects as ``{{ }}``,
        and Jinja-rendered HTML has runtime ``{{ }}`` of its own. Rendering
        those with the default delimiters eats the very syntax the file exists
        to emit, so they get the square-bracket environment instead.
        """
        name = relative.name
        return (
            self.html_env if any(marker in name for marker in _RUNTIME_BRACE_SUFFIXES) else self.env
        )

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


def service_context(
    name: str,
    *,
    kind: str = "api",
    port: int = 8000,
    plugins: Sequence[str] = (),
    rag_store: str | None = None,
    queue_backend: str | None = None,
    workspace_name: str = "workspace",
    api_base_url: str = "",
    frontend: str | None = None,
    routes: Sequence[dict[str, str]] = (),
    language: str = "python",
    sample_module: str = "item",
    grpc: bool = False,
) -> dict[str, Any]:
    snake = to_snake(name)
    enabled = resolve_plugins(kind, plugins)
    # Pick the vector store the service can actually reach. Enabling `rag` and
    # `qdrant` but writing `store = "pgvector"` produces a service that boots
    # and then fails on the first search -- the exact trap the plugin's own
    # startup check exists to catch, and one the generator should never set.
    if rag_store is None:
        rag_store = "pgvector" if "database" in enabled else "qdrant"
    # Same reasoning for the queue: point it at a backend the service has.
    if queue_backend is None:
        queue_backend = "postgres" if "database" in enabled else "redis"

    # Datastores a non-Python service declares for itself: it has no plugin
    # graph, so the workspace reads this list to build its compose entry.
    datastores = [name for name in DATASTORE_PLUGINS if name in enabled]

    context: dict[str, Any] = {
        "project": snake,
        "Project": snake.replace("_", " ").title(),
        "service": snake,
        "Service": to_pascal(name),
        "service_slug": to_kebab(name),
        "service_title": snake.replace("_", " ").title(),
        "kind": kind,
        "language": language,
        "port": port,
        "is_web": kind == "web",
        "grpc": grpc,
        "grpc_port": port + 9,
        "frontend": frontend,
        "workspace_name": workspace_name,
        "api_base_url": api_base_url or f"http://localhost:{port}",
        "routes": list(routes),
        "rag_store": rag_store,
        "queue_backend": queue_backend,
        "datastores": datastores,
        "enabled_plugins": enabled,
        "extras": extras_for(enabled),
        "available_plugins": [
            (n, spec.extra) for n, spec in PLUGIN_CATALOG.items() if n not in enabled and spec.extra
        ],
        **{f"has_{n}": n in enabled for n in PLUGIN_CATALOG},
    }
    if language != "python":
        # Non-Python templates ship one sample domain module and need the same
        # naming vocabulary the Python module templates use.
        context.update(module_context(sample_module))
    return context


def view_context(name: str, *, frontend: str = "vue") -> dict[str, Any]:
    """Context for a frontend module (his `Modulo<Name>` structure)."""
    pascal = to_pascal(name)
    slug = to_kebab(name)
    return {
        "View": pascal,
        "view_slug": slug,
        "view_snake": to_snake(name),
        # "BillingAccount" -> "Billing Account", which is what a sidebar label
        # and a page heading should read.
        "view_title": re.sub(r"(?<=[a-z])(?=[A-Z])", " ", pascal),
        "view_path": f"/{slug}",
        "frontend": frontend,
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


def service_trees(
    kind: str,
    frontend: str | None,
    target: Path,
    *,
    language: str = "python",
    grpc: bool = False,
) -> list[tuple[str, Path]]:
    """Which template trees make up a service of this kind."""
    if kind not in SERVICE_KINDS:
        raise ValueError(f"Unknown kind {kind!r}. Choose from: {', '.join(SERVICE_KINDS)}")

    if language != "python":
        spec = languages.get(language)
        if kind not in spec.kinds:
            raise ValueError(
                f"{spec.label} does not support --kind {kind}. Supported: {', '.join(spec.kinds)}."
            )
        polyglot: list[tuple[str, Path]] = [(spec.template, target)]
        if grpc:
            polyglot.append(("proto", target))
        return polyglot

    if kind == "spa":
        if frontend not in FRONTENDS:
            raise ValueError(
                f"Frontend {frontend!r} is not implemented. "
                f"Choose from: {', '.join(FRONTENDS)}. "
                f"Angular is not generated -- see PLAN.md phase 3."
            )
        return [(f"frontend_{frontend}", target)]

    if kind == "gateway":
        # The gateway shares nothing with an application service: no modules,
        # no migrations, no database. Its own tree keeps it that way.
        return [("service_gateway", target)]

    # Every service is born with a contract. Adding one later means writing it
    # against code that already drifted; starting with one means the first
    # violation is caught on the first commit.
    trees: list[tuple[str, Path]] = [("service_base", target), ("contracts_layered", target)]
    if kind == "web":
        trees.append(("service_web", target))
    return trees


def detect_frontend(root: Path) -> str | None:
    """Work out which framework a frontend project uses.

    Asking the user to repeat ``--frontend react`` inside a React project is
    how you end up with Vue files in it. The router filename is the cheapest
    reliable signal; package.json is the fallback for a project whose router
    has been moved.
    """
    router_dir = root / "src" / "router"
    if (router_dir / "index.jsx").is_file():
        return "react"
    if (router_dir / "index.js").is_file():
        return "vue"

    package = root / "package.json"
    if package.is_file():
        try:
            deps = json.loads(package.read_text(encoding="utf-8")).get("dependencies", {})
        except ValueError:
            return None
        if "react" in deps:
            return "react"
        if "vue" in deps:
            return "vue"
    return None


def view_trees(frontend: str, target: Path) -> list[tuple[str, Path]]:
    if frontend not in FRONTENDS:
        raise ValueError(
            f"Frontend {frontend!r} is not supported for view generation. "
            f"Choose from: {', '.join(FRONTENDS)}."
        )
    return [(f"view_{frontend}", target)]
