"""The ``jfast`` command line.

Two audiences, one interface:

* humans get readable output;
* agents get ``--json`` on every inspection command, so an AI assistant reads
  machine state instead of grepping the source tree.
"""

from __future__ import annotations

import json as jsonlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import typer

from jfastframework import capabilities, languages
from jfastframework import project as project_model
from jfastframework.cli import ai as ai_cli
from jfastframework.cli import check as check_cli
from jfastframework.cli import dev as devtools
from jfastframework.cli import explain as explain_cli
from jfastframework.cli import insight, ui
from jfastframework.cli import migrations as migrations_cli
from jfastframework.cli import modules as module_registry
from jfastframework.cli import ui as cli_ui
from jfastframework.cli import upgrade as upgrade_cli
from jfastframework.cli.exits import Code
from jfastframework.cli.patcher import (
    PatchError,
    ensure_import,
    ensure_named_import,
    insert_at_marker,
)
from jfastframework.cli.scaffold import (
    BASE_PLUGINS,
    CONTRACT_TEMPLATE_FOR,
    DATASTORE_PLUGINS,
    FRONTENDS,
    MODULE_LAYOUTS,
    MODULE_UIS,
    PLUGIN_CATALOG,
    SERVICE_KINDS,
    Scaffolder,
    WrittenFile,
    detect_frontend,
    module_context,
    module_trees,
    service_context,
    service_trees,
    to_pascal,
    to_snake,
    view_context,
    view_trees,
)
from jfastframework.contracts import CONTRACTS_FILE, Contract, check, render, waivers
from jfastframework.graph import render_graph
from jfastframework.resources import RESOURCE_TYPES, Resource
from jfastframework.settings import DEFAULT_CONFIG_FILE, JFastConfig
from jfastframework.workspace import PORT_BLOCK_SIZE, WORKSPACE_FILE, ServiceEntry, Workspace

# Vite's own default. Only what `jfast dev` prints depends on it: the port is
# left to vite unless --web-port asks for another one.
WEB_PORT = 5173

app = typer.Typer(
    name="jfast",
    help="JFastFramework: build, inspect and deploy plugin-based FastAPI services.",
    no_args_is_help=True,
    add_completion=False,
)
new_app = typer.Typer(help="Generate services and modules.", no_args_is_help=True)
plugins_app = typer.Typer(help="Inspect the plugin graph.", no_args_is_help=True)
deploy_app = typer.Typer(help="Generate deployment artifacts.", no_args_is_help=True)
app.add_typer(new_app, name="new")
app.add_typer(plugins_app, name="plugins")
app.add_typer(deploy_app, name="deploy")


def _echo(payload: Any, as_json: bool, human: str) -> None:
    if as_json:
        typer.echo(jsonlib.dumps(payload, indent=2, default=str))
    else:
        typer.echo(human)


def _load(config_path: str) -> JFastConfig:
    return JFastConfig.load(config_path=config_path)


def _root_of(config_path: str | Path) -> Path:
    """The project directory a jfast.toml describes -- its own."""
    return Path(config_path).resolve().parent


def _declared_paths(config: JFastConfig) -> dict[str, str]:
    """``[plugins.paths]``: plugins that live in the project, not in a wheel."""
    paths: dict[str, str] = config.raw.get("plugins", {}).get("paths", {})
    return paths


@app.command()
def version() -> None:
    """Print the framework version."""
    from jfastframework import __version__

    typer.echo(__version__)


@plugins_app.command("list")
def plugins_list(
    config: str = typer.Option(DEFAULT_CONFIG_FILE, "--config", "-c"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
    all_available: bool = typer.Option(
        False, "--all", help="Include discovered plugins this service does not enable."
    ),
) -> None:
    """List the plugins this service loads."""
    from jfastframework.plugins import registry

    cfg = _load(config)

    if all_available:
        available = registry.discover(
            extra_paths=_declared_paths(cfg), search_path=_root_of(config)
        )
        broken: dict[str, str] = getattr(registry.discover, "broken", {})
        discovered: dict[str, Any] = {
            "available": {name: vars(cls.meta) for name, cls in available.items()},
            "unimportable": broken,
        }
        lines = [f"{name:<16} {cls.meta.description}" for name, cls in sorted(available.items())]
        lines += [f"{name:<16} UNIMPORTABLE: {err}" for name, err in sorted(broken.items())]
        _echo(discovered, json_out, "\n".join(lines) or "no plugins discovered")
        return

    enabled: list[dict[str, Any]] = [p.describe() for p in registry.build(cfg)]
    lines = [
        f"{p['name']:<16} v{p['version']:<8} provides: {', '.join(p['provides']) or '-'}"
        for p in enabled
    ]
    _echo(enabled, json_out, "\n".join(lines) or "no plugins enabled")


@app.command()
def describe(
    config: str = typer.Option(DEFAULT_CONFIG_FILE, "--config", "-c"),
    json_out: bool = typer.Option(True, "--json/--text"),
) -> None:
    """Full machine-readable description of the service.

    This is the command an AI agent should run first: it returns the settings
    schema, the plugin graph, the provider keys and the infra containers,
    without importing the application.
    """
    from jfastframework.plugins import registry

    cfg = _load(config)
    instances = registry.build(cfg)
    plugin_names = [plugin.meta.name for plugin in instances]
    providers = sorted({key for p in instances for key in p.meta.provides})
    infra = [service.name for p in instances for service in p.infra()]

    payload: dict[str, Any] = {
        "app": cfg.settings.model_dump(mode="json"),
        "plugins": [plugin.describe() for plugin in instances],
        "providers": providers,
        "infra": infra,
        "settings_schema": cfg.settings.model_json_schema(),
    }
    human = "\n".join(
        [
            f"service : {cfg.settings.app_name} v{cfg.settings.version} ({cfg.settings.env})",
            f"plugins : {', '.join(plugin_names) or '-'}",
            f"provides: {', '.join(providers) or '-'}",
            f"infra   : {', '.join(infra) or '-'}",
        ]
    )
    _echo(payload, json_out, human)


#: What each layout is for, in the order somebody should consider them. The
#: hint is the deciding question, not a description -- a list of four
#: architectures with no way to choose between them is not a choice.
LAYOUT_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("layered", "Layered", "router / service / repository. Start here."),
    ("modular", "Modular", "the same, in folders. For a module that outgrows four files."),
    ("screaming", "Screaming", "one file per use case. When the verbs matter more than the nouns."),
    (
        "hexagonal",
        "Hexagonal",
        "ports and adapters. When the domain must be testable with no database.",
    ),
)


def _ask_layout(module: str) -> str:
    """Ask which shape this module should have, when the flag did not say.

    Falls back to the default without asking when there is no terminal, so a
    script, a CI job and a piped install do not hang on a prompt nobody can
    see. A wizard that blocks a pipeline is worse than a flag nobody set.
    """
    if not ui.console.is_terminal:
        return "layered"
    return ui.select(
        f"Architecture for {module!r}",
        [ui.Choice(key, label, hint) for key, label, hint in LAYOUT_CHOICES],
        default="layered",
    )


def _register_module(root: Path, modules_dir: str, module: str, *, htmx: bool) -> None:
    """Splice a new module's router into main.py.

    The frontend has registered its own routes and menu entries since the
    beginning; the backend printed the two lines and left them to be pasted.
    Which meant the generated module was inert until somebody did, and a module
    that is not mounted looks exactly like a module that does not work.

    Non-fatal by design. A hand-edited main.py that lost its markers, or a
    module generated outside a service, should still leave the files on disk --
    so a failure here prints what to paste instead of unwinding the scaffold.
    """
    entry = root / "main.py"
    if not entry.is_file():
        cli_ui.note(f"No main.py in {root}; mount {module}_router yourself.")
        return

    imports = [f"from {modules_dir}.{module} import router as {module}_router"]
    routers = [f"{module}_router,"]
    if htmx:
        imports.append(f"from {modules_dir}.{module}.web import router as {module}_web_router")
        routers.append(f"{module}_web_router,")

    changed = False
    try:
        for statement in imports:
            result = insert_at_marker(entry, "jfast:imports", statement, guard=statement, indent="")
            changed = changed or result.changed
        for router in routers:
            result = insert_at_marker(
                entry, "jfast:routers", router, guard=f"    {router}", indent="    "
            )
            changed = changed or result.changed
    except PatchError as exc:
        cli_ui.warn(str(exc))
        cli_ui.note("Add these by hand:")
        for line in imports + routers:
            cli_ui.note(f"    {line}")
        return

    # Say which of the two happened. Reporting a mount that did not occur is
    # the same lie as reporting a file written that was already there.
    if changed:
        cli_ui.created("main.py", f"{module}_router mounted")
    else:
        cli_ui.note(f"main.py already mounts {module}_router")


def _report(written: list[Any], title: str = "") -> None:
    """Show what a scaffold wrote.

    Every generating command funnels through here, so the shape of the output
    is decided once. A tree rather than a flat list: forty paths in a column is
    a wall, and the line that actually matters -- a file left alone because it
    already existed -- reads the same as the thirty-nine that were written.

    Falls back to one line per file when the paths share no root to hang a tree
    from, which is the case for the commands that write a single file next to
    the caller.
    """
    if not written:
        return

    paths = [(str(item.path), bool(item.created)) for item in written]
    roots = {path.replace("\\", "/").split("/")[0] for path, _ in paths}
    if len(roots) == 1 and len(paths) > 1:
        # The root becomes the tree's label, so it is stripped from the
        # branches: printing it once at the top and again on every path is how
        # a tree ends up wider and less readable than the list it replaced.
        root = next(iter(roots))
        relative = [
            (path.replace("\\", "/").removeprefix(f"{root}/"), created) for path, created in paths
        ]
        ui.file_tree(title or f"{root}/", relative)
        return

    for path, was_created in paths:
        if was_created:
            ui.created(path)
        else:
            ui.note(f"{ui.G.bullet} {path}  exists, left alone")


@new_app.command("module")
def new_module(
    name: str = typer.Argument(..., help="Module name, e.g. 'order' or 'BillingAccount'."),
    layout: str | None = typer.Option(
        None,
        "--layout",
        "-l",
        help=(
            "layered, modular, screaming or hexagonal. "
            "Asked interactively when omitted; defaults to layered when piped."
        ),
    ),
    ui: str = typer.Option(
        "api",
        "--ui",
        "-u",
        help="api = JSON only. htmx = JSON plus server-rendered pages.",
    ),
    table: str | None = typer.Option(
        None, "--table", help="Table name. Defaults to the pluralised module name."
    ),
    target: Path = typer.Option(Path("modules"), "--target", "-t", help="Modules directory."),
    root: Path = typer.Option(
        Path("."), "--root", help="Project root, where the htmx overlay writes templates."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Scaffold a domain module.

    Two layouts and two UI options, composed rather than duplicated:

        jfast new module order
        jfast new module order --layout screaming
        jfast new module order --ui htmx
        jfast new module order --layout screaming --ui htmx
    """
    if layout is None:
        layout = _ask_layout(name)
    if layout not in MODULE_LAYOUTS:
        raise typer.BadParameter(f"choose from: {', '.join(MODULE_LAYOUTS)}", param_hint="--layout")
    if ui not in MODULE_UIS:
        raise typer.BadParameter(f"choose from: {', '.join(MODULE_UIS)}", param_hint="--ui")

    scaffolder = Scaffolder()
    context = module_context(name, layout=layout, ui=ui, table=table, modules_dir=target.name)
    trees = module_trees(layout, ui, target, root)
    # `ui` here is the --ui option, which shadows the ui module inside this one
    # function. The spinner is reached through the package to say which is meant.
    with cli_ui.working(f"scaffolding {name}"):
        written = scaffolder.render_trees(trees, context, force=force, dry_run=dry_run)
    _report(written)

    module = context["module"]
    if not dry_run:
        _register_module(root, target.name, module, htmx=ui == "htmx")
        # Remembered so `jfast new use-case` and friends know which folder this
        # module keeps that kind of file in, rather than asking again.
        if module_registry.record(root, module, layout=layout, ui=ui):
            cli_ui.created(module_registry.CONFIG_FILE, f"{module} is {layout}")

    steps = [
        (f"pytest {target}/{module}/tests", "the generated test"),
        (f"alembic revision --autogenerate -m 'add {context['table']}'", "the table"),
    ]
    if ui == "htmx":
        steps.insert(0, ('[plugins] enabled = [..., "web"]', "HTMX pages need it"))
    cli_ui.next_steps(f"{module} ({layout}, {ui})", steps)


def _split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def generate_service(
    name: str,
    *,
    kind: str,
    port: int | None,
    plugins: Sequence[str],
    frontend: str | None,
    target: Path | None,
    workspace: Workspace | None,
    language: str = "python",
    grpc: bool = False,
    agent_docs: bool = False,
    layout: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Render a service and register it in the workspace, if there is one.

    Shared by `jfast new service`, `jfast init` and `jfast start` so every path
    produces exactly the same tree -- a wizard that generates something slightly
    different from the flag-driven command is a wizard nobody trusts.
    """
    scaffolder = Scaffolder()
    slug = to_snake(name)
    resolved_port = port if port is not None else (workspace.next_port() if workspace else 8000)

    spec = languages.get(language)
    if not spec.installed():
        typer.echo(
            f"Warning: {spec.toolchain} is not on PATH. The files will be written, "
            f"but you cannot build or run this service until it is installed.",
            err=True,
        )

    context = service_context(
        name,
        kind=kind,
        port=resolved_port,
        plugins=plugins,
        frontend=frontend,
        language=language,
        grpc=grpc,
        agent_docs=agent_docs,
        workspace_name=workspace.name if workspace else slug,
        api_base_url=workspace.api_base_url() if workspace else f"http://localhost:{resolved_port}",
    )
    destination = target or Path(slug)

    trees = service_trees(
        kind,
        frontend,
        destination,
        language=language,
        grpc=grpc,
        agent_docs=agent_docs,
        layout=layout,
    )
    with ui.working("scaffolding"):
        written = scaffolder.render_trees(trees, context, force=force, dry_run=dry_run)
        written += _write_dockerignore(destination, dry_run=dry_run)
    _report(written)

    if workspace is not None and not dry_run:
        workspace.add(
            ServiceEntry(
                name=slug,
                kind=kind,
                port=resolved_port,
                path=str(destination),
                frontend=frontend,
                language=language,
                grpc=grpc,
                datastores=list(context["datastores"]),
            ),
            replace=force,
        )
        # New services get named resources rather than the legacy type list, so
        # the file a project starts with is the one the documentation teaches.
        # Idempotent, and it preserves every port.
        workspace.migrate_resources()
        workspace.save()
        ui.note(f"registered in {workspace.file}")

    return destination, context


def _write_dockerignore(destination: Path, *, dry_run: bool) -> list[WrittenFile]:
    """The exclude list, written with the service rather than with the image.

    `jfast deploy dockerfile` writes one too, but that command is run later --
    often after the first `cp .env.example .env`. A build that happens in
    between copies the filled-in .env into a layer, and by then the leak has
    already been made. It costs nothing to have the file from the start.
    """
    from jfastframework.deploy import render_dockerignore

    path = destination / ".dockerignore"
    if path.exists():
        return [WrittenFile(path, created=False)]
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_dockerignore(), encoding="utf-8")
    return [WrittenFile(path, created=True)]


#: Every plugin `--with` accepts, read off the catalog the installer uses. The
#: literal it replaced named 7 of the 20 that ship, so `--with storage` worked
#: while the help said no such thing existed.
_WITH_CHOICES = ",".join(n for n in PLUGIN_CATALOG if n not in BASE_PLUGINS)


@new_app.command("service")
def new_service(
    name: str = typer.Argument(..., help="Service name, e.g. 'billing'."),
    kind: str = typer.Option(
        "api",
        "--kind",
        "-k",
        help="api = JSON. web = server-rendered (Jinja+HTMX). spa = Vue/React. gateway = proxy.",
    ),
    with_: str | None = typer.Option(
        None,
        "--with",
        "-w",
        help=(
            f"Comma-separated plugins: {_WITH_CHOICES}. Defaults to database for backend services."
        ),
    ),
    frontend: str | None = typer.Option(
        None, "--frontend", "-f", help=f"For --kind spa: {', '.join(FRONTENDS)}."
    ),
    language: str = typer.Option(
        "python",
        "--language",
        "-L",
        help="python (full plugin system) or go (stdlib net/http, zero deps).",
    ),
    grpc: bool = typer.Option(
        False, "--grpc", help="Also generate the .proto contract for internal calls."
    ),
    agent_docs: bool = typer.Option(
        False,
        "--agent-docs",
        help="Also write AGENTS.md and .jfast/skills/, for AI agents working here.",
    ),
    layout: str | None = typer.Option(
        None,
        "--layout",
        "-l",
        help=(
            f"Write the contract for this layout now: {', '.join(MODULE_LAYOUTS)}. "
            "Only if you already know how every module here will be shaped -- "
            "otherwise leave it out and the first `jfast new module --layout X` "
            "writes the contract that matches what it generated."
        ),
    ),
    port: int | None = typer.Option(
        None, "--port", "-p", help="Base port. Defaults to the next free block in the workspace."
    ),
    target: Path | None = typer.Option(
        None, "--target", "-t", help="Destination directory. Defaults to ./<name>."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Scaffold a whole service.

    A frontend is a service like any other -- it deploys, logs and reports
    health identically:

        jfast new service billing --with database,cache
        jfast new service storefront --kind web
        jfast new service admin --kind spa --frontend vue
    """
    if kind not in SERVICE_KINDS:
        raise typer.BadParameter(f"choose from: {', '.join(SERVICE_KINDS)}", param_hint="--kind")
    if layout is not None and layout not in MODULE_LAYOUTS:
        raise typer.BadParameter(f"choose from: {', '.join(MODULE_LAYOUTS)}", param_hint="--layout")

    chosen = _split_csv(with_)
    if not chosen and kind in ("api", "web"):
        chosen = ["database"]

    workspace = Workspace.load_or_none()

    try:
        destination, context = generate_service(
            name,
            kind=kind,
            port=port,
            plugins=chosen,
            frontend=frontend,
            target=target,
            workspace=workspace,
            language=language,
            grpc=grpc,
            agent_docs=agent_docs,
            layout=layout,
            force=force,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    _print_next_steps(destination, context, kind)

    if workspace is not None and workspace.needs_gateway() and not dry_run:
        typer.echo(
            f"\nThe workspace now has {len(workspace.backends)} backends and no gateway.\n"
            f"Generating one so clients need a single hostname:"
        )
        _generate_gateway(workspace, force=False)


def _print_next_steps(destination: Path, context: dict[str, Any], kind: str) -> None:
    """The commands to run now, each with what it does.

    A list of commands with no explanation is a list somebody pastes without
    reading. The description column is the difference between following
    instructions and understanding them.
    """
    slug = context["service_slug"]
    port = context["port"]

    if context.get("language") == "go":
        steps = [
            (f"cd {destination}", ""),
            ("cp .env.example .env", "the generated defaults"),
            ("go test ./...", "the contract tests that ship with it"),
            ("go run .", f"serves on :{port}"),
        ]
        ui.next_steps(f"{slug}  go {kind}", steps)
        if context.get("grpc"):
            ui.note("The gRPC contract is in proto/. Generating stubs is a build step you own.")
        return

    if kind == "spa":
        ui.next_steps(
            f"{slug} {ui.G.dash} {context['frontend']}",
            [
                (f"cd {destination}", ""),
                ("npm install", ""),
                ("npm run dev", f"http://localhost:{port}"),
                ("jfast new view Facturas", "a page, wired into the router and sidebar"),
            ],
        )
        ui.note(f"VITE_API_URL already points at {context['api_base_url']}.")
        return

    if kind == "gateway":
        ui.next_steps(
            f"{slug} {ui.G.dash} gateway",
            [
                (f"cd {destination}", ""),
                (f'pip install "jfastframework[{context["extras"]}]"', ""),
                ("jfast serve", f"http://127.0.0.1:{port}"),
            ],
        )
        return

    steps = [
        (f"cd {destination}", ""),
        ("pip install -r requirements.txt", ""),
        ("cp .env.example .env", "then fill in the secrets"),
    ]
    if context["has_database"]:
        # `jfast deploy compose` first, because there is no docker-compose.yml
        # in the tree yet: the previous wording sent a new service straight to
        # `docker compose up -d` against a file that does not exist.
        steps.append(("jfast deploy compose -o docker-compose.yml", "writes it from the plugins"))
        steps.append(("docker compose up -d", "the datastores it needs"))
        steps.append(("alembic upgrade head", "creates the schema"))
    steps.append(
        ("jfast serve", f"http://127.0.0.1:{port}  {ui.G.bullet}  /docs  {ui.G.bullet}  /ready")
    )
    steps.append(
        (
            "jfast new module invoice" + (" --ui htmx" if kind == "web" else ""),
            "your first module",
        )
    )

    ui.next_steps(f"{slug}  {kind}", steps)
    ui.note(f"plugins: {', '.join(context['enabled_plugins'])}")


@deploy_app.command("compose")
def deploy_compose(
    config: str = typer.Option(DEFAULT_CONFIG_FILE, "--config", "-c"),
    output: Path = typer.Option(Path("docker-compose.generated.yml"), "--output", "-o"),
    base_port: int | None = typer.Option(None, "--base-port"),
    stdout: bool = typer.Option(False, "--stdout", help="Print instead of writing."),
) -> None:
    """Generate docker-compose.yml from the enabled plugin graph."""
    from jfastframework.deploy import build_compose, render_compose
    from jfastframework.plugins import registry

    cfg = _load(config)
    instances = registry.build(cfg)
    rendered = render_compose(build_compose(cfg, instances, base_port=base_port))
    if stdout:
        typer.echo(rendered)
        return
    output.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {output}")


@deploy_app.command("dockerfile")
def deploy_dockerfile(
    output: Path = typer.Option(Path("Dockerfile"), "--output", "-o"),
    python: str = typer.Option("3.12", "--python"),
    stdout: bool = typer.Option(False, "--stdout"),
) -> None:
    """Generate a production Dockerfile and the .dockerignore it needs."""
    from jfastframework.deploy import render_dockerfile, render_dockerignore

    rendered = render_dockerfile(python)
    if stdout:
        typer.echo(rendered)
        return
    output.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {output}")

    # Written next to the Dockerfile, never over an existing one: the exclude
    # list is a thing people edit, and silently replacing an edited copy is
    # how a build starts shipping a directory somebody had excluded. The
    # Dockerfile is generated and says so; this is generated once and owned.
    ignore = output.parent / ".dockerignore"
    if ignore.exists():
        typer.echo(f"kept {ignore} (already present)")
    else:
        ignore.write_text(render_dockerignore(), encoding="utf-8")
        typer.echo(f"wrote {ignore}")


@deploy_app.command("function")
def deploy_function(
    name: str = typer.Argument(..., help="Function / Cloud Run service name."),
    target: str = typer.Option("aws", "--target", "-t", help="aws | gcp"),
    region: str = typer.Option("us-east-1", "--region"),
    account_id: str = typer.Option("", "--account-id", help="AWS account id (12 digits)."),
    project: str = typer.Option("", "--project", help="GCP project id."),
    memory: int = typer.Option(512, "--memory", help="Memory in MB."),
    timeout: int = typer.Option(30, "--timeout", help="Request timeout in seconds."),
    public: bool = typer.Option(
        False, "--public", help="Expose without authentication. Off by default."
    ),
    output: Path = typer.Option(Path("."), "--output", "-o"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Generate the files that deploy this service as a serverless function.

    It writes scripts; it does not run them. Read them before you do -- they
    are the only generated artifacts that spend money.
    """
    from jfastframework.deploy.serverless import FunctionConfig, render

    try:
        files = render(
            FunctionConfig(
                name=name,
                target=target,
                region=region,
                account_id=account_id,
                project=project,
                memory_mb=memory,
                timeout_seconds=timeout,
                public=public,
            )
        )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1) from exc

    output.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        destination = output / relative
        if destination.exists() and not force:
            typer.secho(f"skipped {destination} (exists; --force to overwrite)", fg="yellow")
            continue
        destination.write_text(content, encoding="utf-8")
        if destination.suffix == ".sh":
            destination.chmod(0o755)
        typer.echo(f"wrote {destination}")

    if public:
        typer.secho(
            "This function will be reachable by anyone with the URL. "
            "Enable the auth plugin, or drop --public.",
            fg=typer.colors.YELLOW,
        )
    if target == "aws":
        typer.echo("\nAdd 'mangum' to requirements.txt, then: ./deploy-lambda.sh")
    else:
        typer.echo("\nThen: ./deploy-cloudrun.sh")


ENUM_HEADER = (
    '"""Enumerated values.\n'
    "\n"
    "The value is the wire format: it is stored in a column, serialised into JSON\n"
    "and read by a frontend. Renaming a member is free; changing its value is a\n"
    "data migration.\n"
    "\n"
    "`str, Enum` rather than `Enum`, so a member is a string everywhere -- in\n"
    "JSON, in SQL, and in a log line -- instead of `Status.DRAFT` in some paths\n"
    'and `"DRAFT"` in others.\n'
    '"""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "from enum import Enum\n"
    "\n"
    "\n"
)


def _render_enum(class_name: str, members: list[str]) -> str:
    lines = [f"class {class_name}(str, Enum):", f'    """{class_name}."""', ""]
    for member in members:
        lines.append(f'    {to_snake(member).upper()} = "{to_snake(member)}"')
    return "\n".join(lines) + "\n"


@new_app.command("enum")
def new_enum(
    name: str = typer.Argument(..., help="Enum name in PascalCase, e.g. DocumentStatus."),
    module: str | None = typer.Option(None, "--module", "-m", help="Put it in this module."),
    shared: bool = typer.Option(False, "--shared", help="Put it in shared/ instead."),
    values: str = typer.Option("", "--values", help="Comma-separated members."),
) -> None:
    """Add an enum, in the module that needs it or in shared/.

    The placement is the decision, not the file. An enum only one module speaks
    belongs to that module; one that two modules speak belongs in `shared/`,
    because the alternative is a cross-module import -- and that is the
    coupling that stops either module from ever being extracted.

    You do not have to predict which is which. Start it in the module, and
    `jfast contracts check` tells you the day a second module imports it.
    """
    if shared and module:
        raise typer.BadParameter("--shared and --module are the same decision, made twice")

    if not shared and module is None:
        shared = ui.confirm(
            "Will more than one module use it?",
            default=False,
            hint="yes puts it in shared/, no puts it in one module",
        )
        if not shared:
            candidates = sorted(
                p.name for p in Path("modules").glob("*") if p.is_dir() and p.name[0] != "_"
            )
            if not candidates:
                raise typer.BadParameter(
                    "no modules here. Run this inside a service, or pass --shared"
                )
            module = (
                candidates[0]
                if len(candidates) == 1
                else ui.select(
                    "Which module?",
                    [ui.Choice(c, "", "") for c in candidates],
                    default=candidates[0],
                )
            )

    members = [v.strip() for v in values.split(",") if v.strip()] or ["DRAFT", "ACTIVE"]
    class_name = to_pascal(name)

    if shared:
        target = Path("shared") / "enums.py"
        where = "shared/"
        why = "every module can import it, and none has to import another"
    else:
        # Hexagonal keeps its vocabulary in the domain layer, and the generated
        # contract scopes that layer to `modules/*/domain/*.py`. An enum at the
        # module root matches no layer glob, so the domain's `may_import` and
        # `forbid_packages` never apply to it -- it imports fine and sits
        # outside the architecture without the checker saying so. The other
        # three layouts do keep enums.py at the module root.
        parent = Path("modules") / str(module)
        recorded = module_registry.layout_of(Path("."), str(module))
        # Falling back to the tree on disk covers modules generated before the
        # layout was recorded; there is no other signal left for those.
        if recorded == "hexagonal" or (recorded is None and (parent / "domain").is_dir()):
            parent = parent / "domain"
        target = parent / "enums.py"
        where = f"{parent.as_posix()}/"
        why = "move it to shared/ the day a second module needs it"

    if not target.parent.is_dir():
        raise typer.BadParameter(f"{target.parent} does not exist. Run this inside a service.")

    body = _render_enum(class_name, members)
    if target.is_file():
        existing = target.read_text(encoding="utf-8")
        if f"class {class_name}(" in existing:
            typer.echo(f"{class_name} is already in {target}.")
            raise typer.Exit(1)
        target.write_text(existing.rstrip("\n") + "\n\n\n" + body, encoding="utf-8")
    else:
        target.write_text(ENUM_HEADER + body, encoding="utf-8")

    ui.created(str(target), class_name)
    dotted = str(target.with_suffix("")).replace("/", ".").replace("\\", ".")
    ui.summary(
        f"{class_name} in {where}",
        [
            ("members", ", ".join(members)),
            ("why here", why),
            ("import", f"from {dotted} import {class_name}"),
        ],
    )


@app.command("add")
def add_capability(
    capability: str | None = typer.Argument(None, help="What to add. Omit to see the catalogue."),
    service: str | None = typer.Option(
        None, "--service", "-s", help="Which service. Asked when a workspace has several."
    ),
    pandas: bool = typer.Option(
        False, "--pandas", help="dataframes only: install pandas instead of polars."
    ),
    install: bool = typer.Option(
        True, "--install/--no-install", help="Run pip after editing requirements."
    ),
) -> None:
    """Add a capability to a service: the packages, the extra, and the advice.

    Not a nicer `pip install`. Each entry carries the decision somebody would
    otherwise make badly -- which of two libraries and why, the packaging trap
    that makes the obvious wheel fail inside a container, and what has to
    happen besides installing.
    """
    if capability is None:
        _list_capabilities()
        return

    try:
        spec = capabilities.get(capability)
    except KeyError as exc:
        ui.console.print(f"  [{ui.ACCENT}]{exc}[/]")
        raise typer.Exit(1) from exc

    target = _resolve_service_dir(service)
    requirements = target / "requirements.txt"
    if not requirements.is_file():
        ui.console.print(
            f"  [{ui.ACCENT}]No requirements.txt in {target}.[/]\n"
            f"  Run this inside a generated service, or pass --service."
        )
        raise typer.Exit(1)

    extra = "pandas" if (spec.name == "dataframes" and pandas) else spec.extra
    packages = ("pandas>=2.2",) if extra == "pandas" else spec.packages

    body = requirements.read_text(encoding="utf-8")
    if f"[{extra}" in body or f",{extra}]" in body or f",{extra}," in body:
        ui.note(f"{target.name} already has {extra}.")
        raise typer.Exit(0)

    updated = _add_extra(body, extra)
    requirements.write_text(updated, encoding="utf-8")

    ui.summary(
        f"{spec.name} {ui.G.arrow} {target.name}",
        [
            ("packages", ", ".join(packages)),
            ("extra", extra),
            ("why", spec.rationale or ui.G.dash),
        ],
    )
    ui.created(str(requirements), f"now pins [{extra}]")

    if spec.system_packages:
        ui.warn("This needs system packages in the image:")
        ui.note("apt-get install -y " + " ".join(spec.system_packages))

    if spec.plugin:
        ui.note(f'Enable the plugin: add "{spec.plugin}" to [plugins].enabled in jfast.toml')

    if spec.after:
        ui.note(spec.after)

    if install:
        import subprocess

        ui.step(f"pip install -r {requirements}")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)],
            check=False,
        )
        if result.returncode != 0:
            ui.warn("pip failed. requirements.txt is updated; install it yourself.")
            raise typer.Exit(1)
        ui.console.print(f"  [{ui.OK}]installed[/]")


def _list_capabilities() -> None:
    from rich.table import Table

    table = Table(box=None, padding=(0, 2))
    table.add_column("", style=ui.ACCENT, no_wrap=True)
    table.add_column("", style="white")
    table.add_column("", style=ui.DIM)

    for name in capabilities.names():
        spec = capabilities.CATALOG[name]
        note = "heavy" if spec.heavy else ""
        table.add_row(name, spec.summary, note)

    ui.console.print()
    ui.console.print(table)
    ui.console.print()
    ui.note("jfast add <name>          adds it to this service")
    ui.note("jfast add <name> -s api   adds it to one service in a workspace")


def _add_extra(requirements: str, extra: str) -> str:
    """Put an extra into the existing jfastframework pin, in sorted order."""
    import re

    def rewrite(match: re.Match[str]) -> str:
        current = [e for e in match.group(1).split(",") if e]
        if extra not in current:
            current.append(extra)
        return "jfastframework[" + ",".join(sorted(current)) + "]"

    updated, count = re.subn(r"jfastframework\[([^\]]*)\]", rewrite, requirements, count=1)
    if count:
        return updated
    # No extras yet, or no framework line at all: append rather than guess.
    return requirements.rstrip("\n") + f"\njfastframework[{extra}]\n"


def _resolve_service_dir(service: str | None) -> Path:
    """Which service this applies to.

    In a single service, the current directory. In a workspace with several,
    ask -- because adding a heavy dependency to the wrong one is invisible
    until the image is built.
    """
    if service is not None:
        workspace = Workspace.load_or_none()
        if workspace is not None:
            entry = workspace.get(service)
            if entry is None:
                known = ", ".join(s.name for s in workspace.services) or "none"
                raise typer.BadParameter(f"no service {service!r}. Known: {known}")
            return Path(entry.path).resolve()
        return Path(service).resolve()

    if (Path.cwd() / "requirements.txt").is_file():
        return Path.cwd()

    workspace = Workspace.load_or_none()
    if workspace is None:
        return Path.cwd()

    backends = [s for s in workspace.services if not s.is_frontend]
    if not backends:
        return Path.cwd()
    if len(backends) == 1:
        return Path(backends[0].path).resolve()

    chosen = ui.select(
        "Which service?",
        [ui.Choice(s.name, "", f"{s.kind} :{s.port}") for s in backends],
        default=backends[0].name,
    )
    entry = workspace.get(chosen)
    assert entry is not None
    return Path(entry.path).resolve()


@app.command()
def serve(
    path: Path = typer.Option(
        Path("."), "--path", "-p", help="Service directory. Defaults to the current one."
    ),
    port: int | None = typer.Option(None, "--port", help="Overrides the port in jfast.toml."),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind."),
    reload: bool = typer.Option(True, "--reload/--no-reload", help="Restart on a file change."),
    app_path: str = typer.Option("main:app", "--app", help="Import path of the ASGI app."),
) -> None:
    """Run this service locally.

    ``create_app()`` resolves ``jfast.toml`` against the working directory, so a
    service started from anywhere else boots with framework defaults -- the name
    ``jfast-service``, two plugins, no database, no cache -- and says nothing
    about it. The symptom is a service that runs and is missing everything.

    This changes into the service directory before importing, and refuses to
    start when there is no ``jfast.toml`` there, which is the case that used to
    boot silently wrong.

    The default host is loopback rather than ``0.0.0.0``: a development server
    should not be reachable from the rest of the network unless you say so.
    """
    service_dir = path.resolve()
    if not service_dir.is_dir():
        typer.echo(f"{service_dir} is not a directory.", err=True)
        raise typer.Exit(1)

    config_file = service_dir / DEFAULT_CONFIG_FILE
    if not config_file.is_file():
        typer.echo(
            f"No {DEFAULT_CONFIG_FILE} in {service_dir}.\n"
            f"\nRun this from a service directory, or point at one:\n"
            f"    jfast serve --path ./billing\n"
            f"\nStarting anyway would boot with framework defaults and no database,"
            f"\nwhich looks like it worked.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - depends on the install
        typer.echo(
            "uvicorn is not installed. Add the server extra:\n"
            '    pip install "jfastframework[server]"',
            err=True,
        )
        raise typer.Exit(1) from exc

    # chdir alone is not enough. Python fixed sys.path[0] to wherever the CLI
    # lives when the interpreter started, so `main` would not be importable;
    # and with --reload uvicorn spawns a child that builds its own sys.path, so
    # the directory has to travel in PYTHONPATH to survive the reload.
    os.chdir(service_dir)
    sys.path.insert(0, str(service_dir))
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (
        f"{service_dir}{os.pathsep}{existing}" if existing else str(service_dir)
    )

    settings = JFastConfig.load(config_path=DEFAULT_CONFIG_FILE).settings
    resolved_port = port if port is not None else settings.port

    typer.echo(
        f"  {settings.app_name}  ({settings.env})\n"
        f"  http://{host}:{resolved_port}\n"
        f"  docs      {settings.effective_docs_url or 'closed in this environment'}\n"
        f"  probes    /health  /ready\n"
    )
    uvicorn.run(
        app_path,
        host=host,
        port=resolved_port,
        reload=reload,
        reload_dirs=[str(service_dir)] if reload else None,
        # uvicorn ships this on, with loopback trusted, and it rewrites the
        # client address from X-Forwarded-For before any application middleware
        # runs -- so trusted_proxies would never see a real peer. This host is
        # loopback by default, which is exactly the address uvicorn believes:
        # a rate limit validated here would pass for the wrong reason and fail
        # in production. There is no flag to put it back; trusted_proxies in
        # jfast.toml is the one place that policy is written.
        proxy_headers=False,
    )


@app.command()
def dev(
    path: Path = typer.Option(
        Path("."), "--path", "-p", help="Service directory. Defaults to the current one."
    ),
    frontend: Path | None = typer.Option(
        None, "--frontend", help="Frontend directory. Found from the workspace when omitted."
    ),
    port: int | None = typer.Option(None, "--port", help="Overrides the port in jfast.toml."),
    web_port: int | None = typer.Option(
        None, "--web-port", help=f"Frontend dev server port. Vite's {WEB_PORT} when omitted."
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind."),
    infra: bool = typer.Option(True, "--infra/--no-infra", help="Bring up database and cache."),
    migrate: bool = typer.Option(True, "--migrate/--no-migrate", help="Run alembic upgrade head."),
    web: bool = typer.Option(True, "--web/--no-web", help="Also start the frontend dev server."),
) -> None:
    """Everything needed to develop: infrastructure, migrations, API and frontend.

    `jfast serve` starts the backend and nothing else. This is the other thing:
    the four steps somebody does every morning, in the order that makes the
    failures land where they belong.

    Every stage degrades rather than blocking. No Docker, no compose file, no
    Alembic, no frontend -- each is announced and skipped, and what remains
    still runs. The one stage that does stop the run is a failing migration:
    booting against a schema that is behind produces errors in requests that
    have nothing to do with it.

    Both servers can be moved: `--port` for the API, `--web-port` for the
    frontend. Without the second one, a machine already using 5173 left
    `--no-web` as the only way through, which gives up half the command.
    """
    service_dir = path.resolve()
    config_file = service_dir / DEFAULT_CONFIG_FILE
    if not config_file.is_file():
        ui.warn(f"No {DEFAULT_CONFIG_FILE} in {service_dir}.")
        ui.note("Run this from a service directory, or point at one:")
        ui.note("    jfast dev --path ./billing")
        raise typer.Exit(1)

    ui.banner("everything needed to develop, in order")

    settings = JFastConfig.load(config_path=str(config_file)).settings
    resolved_port = port if port is not None else settings.port
    workspace = Workspace.load_or_none()

    processes: list[devtools.Process] = []

    # -- infrastructure --------------------------------------------------
    compose_file = _find_compose(service_dir)
    if not infra:
        ui.note("infra    skipped (--no-infra)")
    elif compose_file is None:
        ui.note("infra    skipped: no docker-compose.yml found")
    elif not devtools.docker_available():
        ui.note("infra    skipped: docker is not on PATH")
    else:
        services = devtools.compose_services(compose_file, ("-database", "-cache"))
        target = services or []
        ui.step(f"starting {', '.join(target) if target else 'every compose service'}")
        try:
            with ui.working("waiting for containers"):
                devtools.run(
                    ["docker", "compose", "-f", str(compose_file), "up", "-d", *target],
                    cwd=compose_file.parent,
                    what="docker compose up",
                )
                healthy = devtools.wait_for_healthy(compose_file, compose_file.parent)
        except devtools.DevError as exc:
            ui.warn(str(exc))
            raise typer.Exit(1) from exc
        if healthy:
            ui.created("infra", "up and healthy")
        else:
            ui.warn("containers did not report healthy; continuing anyway")

    # Everything from here runs on the host, where the generated .env is wrong
    # twice over: it addresses containers by service name, and leaves the
    # password as a ${...} only compose interpolates. Computed once, because
    # Alembic needs the same translation the server does -- and it runs first,
    # so getting this only onto the server means the migration fails with a DNS
    # error naming a host that was never meant to resolve here.
    env = {"PYTHONPATH": str(service_dir) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    if compose_file is not None:
        env.update(
            devtools.host_environment(
                service_dir / ".env", compose_file.parent / ".env", compose_file
            )
        )

    # -- migrations ------------------------------------------------------
    if not migrate:
        ui.note("migrate  skipped (--no-migrate)")
    elif not (service_dir / "alembic.ini").is_file():
        ui.note("migrate  skipped: no alembic.ini")
    else:
        try:
            with ui.working("alembic upgrade head"):
                devtools.run(
                    [devtools.python_executable(), "-m", "alembic", "upgrade", "head"],
                    cwd=service_dir,
                    what="alembic upgrade head",
                    env=env,
                )
        except devtools.DevError as exc:
            # The one hard stop. A server on a stale schema fails later, in a
            # request that has nothing to do with the missing column.
            ui.warn(str(exc))
            raise typer.Exit(1) from exc
        ui.created("schema", "at head")

    # -- the servers -----------------------------------------------------
    processes.append(
        devtools.spawn(
            [
                devtools.python_executable(),
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                host,
                "--port",
                str(resolved_port),
                "--reload",
                # Same reason as `jfast serve`: uvicorn's own X-Forwarded-For
                # handling replaces the client address before trusted_proxies
                # can decide, and it trusts the loopback address this binds.
                "--no-proxy-headers",
            ],
            cwd=service_dir,
            name="api",
            env=env,
        )
    )

    front_dir = frontend or _find_frontend(service_dir, workspace)
    resolved_web_port = web_port if web_port is not None else WEB_PORT
    if not web:
        ui.note("web      skipped (--no-web)")
    elif front_dir is None:
        ui.note("web      skipped: no frontend project found")
    elif not (front_dir / "node_modules").is_dir():
        ui.warn(f"{front_dir}/node_modules is missing. Run npm install there first.")
    else:
        # The bare `--` is npm's, not vite's: without it npm eats the flag
        # instead of forwarding it to the script.
        command = ["npm", "run", "dev"]
        if web_port is not None:
            command += ["--", "--port", str(web_port)]
        processes.append(devtools.spawn(command, cwd=front_dir, name="web"))

    ui.next_steps(
        "Running",
        [
            (f"http://{host}:{resolved_port}", "the API"),
            (f"http://{host}:{resolved_port}/docs", "its docs"),
            *(
                [(f"http://localhost:{resolved_web_port}", "the frontend")]
                if len(processes) > 1
                else []
            ),
            ("Ctrl-C", "stops everything it started" if len(processes) > 1 else "stops it"),
        ],
    )

    code = devtools.supervise(processes)
    ui.note("stopped")
    raise typer.Exit(code)


def _find_compose(service_dir: Path) -> Path | None:
    """The compose file for this service, which usually lives one level up.

    A workspace writes one compose file at its root covering every service, so
    looking only in the service directory finds nothing in the normal case.
    """
    for candidate in (service_dir, service_dir.parent):
        found = candidate / "docker-compose.yml"
        if found.is_file():
            return found
    return None


def _find_frontend(service_dir: Path, workspace: Workspace | None) -> Path | None:
    """The frontend project belonging to this service.

    The workspace knows, when there is one. Without it, fall back to the
    convention `jfast start` uses: a sibling directory named `<service>-web`.
    """
    if workspace is not None and workspace.file is not None:
        # Service paths in the workspace are relative to the workspace file, not
        # to the service being started.
        base = workspace.file.parent
        for entry in workspace.services:
            if entry.frontend:
                candidate = (base / entry.path).resolve()
                if (candidate / "package.json").is_file():
                    return candidate
    sibling = service_dir.parent / f"{service_dir.name}-web"
    if (sibling / "package.json").is_file():
        return sibling
    return None


@app.command()
def doctor(
    config: str = typer.Option(DEFAULT_CONFIG_FILE, "--config", "-c"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Check that the configuration resolves and the service can be built."""
    from jfastframework.plugins import registry

    problems: list[str] = []
    checks: dict[str, Any] = {}

    config_path = Path(config)
    checks["config_file"] = (
        str(config_path) if config_path.is_file() else "missing (using env only)"
    )

    try:
        cfg = _load(config)
        checks["app_name"] = cfg.settings.app_name
        checks["env"] = cfg.settings.env
    except Exception as exc:
        problems.append(f"config failed to load: {exc}")
        _echo({"ok": False, "problems": problems}, json_out, f"FAIL  {problems[-1]}")
        raise typer.Exit(Code.CONFIG) from exc

    try:
        instances = registry.build(cfg)
        checks["plugins"] = [p.meta.name for p in instances]
    except Exception as exc:  # noqa: BLE001
        problems.append(f"plugin graph failed to resolve: {exc}")
        instances = []

    broken = getattr(registry.discover, "broken", {})
    for name in cfg.settings.plugins:
        if name in broken:
            problems.append(f"plugin {name!r} is enabled but cannot import: {broken[name]}")

    # Resolving the graph instantiates the plugins; it does not register them,
    # and registration is where every plugin checks its own settings. A fresh
    # `jfast new service --with auth` resolves cleanly and then raises
    # `auth mode "jwks" needs jwks_url` on the first import of main.py -- so
    # the command whose job is to say "this is configured" was answering from
    # the half of the boot that cannot fail on configuration. Building the app
    # runs the same code path `create_app` does, minus the lifespan: no
    # connection is opened and nothing is started.
    if not problems:
        build_failure = check_cli.build_error(cfg)
        if build_failure is not None:
            problems.append(f"the service cannot be built: {build_failure}")

    if "database" in checks.get("plugins", []):
        # The service pins its own sessions to UTC, so its answers are
        # right. Everything else touching that database -- psql, a BI tool,
        # a migration run by hand -- computes date_trunc, CURRENT_DATE and
        # now()::date in the server's zone, and reports a different day.
        # Not a failure of this service; worth saying once, out loud.
        import asyncio

        from jfastframework.plugins.builtin.database import (
            DatabaseSettings,
            server_timezone,
        )

        try:
            db_settings = DatabaseSettings(**cfg.plugin_config("database"))
            server, session = asyncio.run(
                server_timezone(
                    db_settings.dsn_for(db_settings.default_name()),
                    session_timezone=db_settings.session_timezone,
                )
            )
            checks["db_timezone"] = f"server {server}, session {session}"
            if server != "UTC":
                problems.append(
                    f"the database's TimeZone is {server!r}, not UTC. This service pins "
                    f"its own sessions, but every other client of that database computes "
                    f"date_trunc, CURRENT_DATE and now()::date in {server!r} and will "
                    f"report a different day."
                )
        except Exception as exc:  # noqa: BLE001 -- every failure reads the same here
            checks["db_timezone"] = f"unreachable ({exc})"

    ok = not problems
    payload = {"ok": ok, "checks": checks, "problems": problems}
    human_lines = [f"{k:<14} {v}" for k, v in checks.items()]
    human_lines += [f"PROBLEM        {p}" for p in problems]
    human_lines.append("OK" if ok else f"{len(problems)} problem(s)")
    _echo(payload, json_out, "\n".join(human_lines))
    if not ok:
        raise typer.Exit(Code.ENVIRONMENT)


ICON = "mdiViewDashboardOutline"


@new_app.command("view")
def new_view(
    name: str = typer.Argument(..., help="View name in PascalCase, e.g. 'Facturas'."),
    frontend: str | None = typer.Option(
        None,
        "--frontend",
        "-f",
        help=f"Choose from: {', '.join(FRONTENDS)}. Detected from the project when omitted.",
    ),
    root: Path = typer.Option(
        Path("."), "--root", "-r", help="Frontend project root (the folder holding src/)."
    ),
    sidebar: bool = typer.Option(
        True, "--sidebar/--no-sidebar", help="Also add the entry to src/menuAside.js."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Scaffold a frontend module and register it.

    Creates ``src/Modulo<Name>/`` with Pages, Routes, Services and Components,
    then splices the route into the router and, unless ``--no-sidebar``, the
    entry into the sidebar at their marker comments.

    ``--no-sidebar`` is for the pages a logged-out visitor reaches: login,
    password reset, a public invoice. They are routed like any other view and
    listed in no menu, and removing the entry afterwards means hand-editing
    generated code.

    Running it twice is safe: an already-registered module is detected and
    skipped rather than duplicated.
    """
    resolved = frontend or detect_frontend(root)
    if resolved is None:
        typer.echo(
            f"Cannot tell which framework {root} uses, and --frontend was not given.\n"
            f"Run this from a frontend project root, or pass "
            f"--frontend {'|'.join(FRONTENDS)}.",
            err=True,
        )
        raise typer.Exit(1)

    scaffolder = Scaffolder()
    try:
        context = view_context(name, frontend=resolved)
        trees = view_trees(resolved, root)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    frontend = resolved

    with ui.working("scaffolding"):
        written = scaffolder.render_trees(trees, context, force=force, dry_run=dry_run)
    _report(written)

    if dry_run:
        typer.echo("\n(dry run: router and menu not patched)")
        return

    view = context["View"]
    ext = "js" if frontend == "vue" else "jsx"
    router_file = root / "src" / "router" / f"index.{ext}"
    menu_file = root / "src" / "menuAside.js"

    try:
        results = [
            ensure_import(
                router_file,
                f"import {{ Modulo{view} }} from '@/Modulo{view}/Routes/router.{ext}'",
                guard=f"Modulo{view}/Routes/router",
            ),
            insert_at_marker(
                router_file,
                "nuevaRuta",
                f"...Modulo{view},",
                guard=f"...Modulo{view},",
            ),
        ]
        if sidebar:
            results += [
                ensure_named_import(menu_file, "@mdi/js", ICON),
                insert_at_marker(
                    menu_file,
                    "nuevoModulo",
                    "{\n"
                    f"  to: '{context['view_path']}',\n"
                    f"  icon: {ICON},\n"
                    f"  label: '{context['view_title']}',\n"
                    "},",
                    guard=f"to: '{context['view_path']}'",
                ),
            ]
    except PatchError as exc:
        typer.echo(f"\nFiles were written, but registration failed:\n  {exc}", err=True)
        raise typer.Exit(1) from exc

    for result in results:
        typer.echo(str(result))

    typer.echo(
        f"\nModule 'Modulo{view}' scaffolded and registered.\n"
        f"  route:   {context['view_path']}\n"
        f"  page:    src/Modulo{view}/Pages/{view}View."
        + ("vue" if frontend == "vue" else "jsx")
        + f"\n  service: src/Modulo{view}/Services/{context['view_slug']}.service.js\n"
        + ("" if sidebar else "  sidebar: not listed (--no-sidebar)\n")
        + f"\nThe service calls {context['view_path']} on VITE_API_URL. Point it at a real\n"
        f"backend module with: jfast new module {context['view_snake']}"
    )


# --------------------------------------------------------------------------
# workspace
# --------------------------------------------------------------------------

workspace_app = typer.Typer(help="Manage a multi-service workspace.", no_args_is_help=True)
app.add_typer(workspace_app, name="workspace")


def _require_workspace() -> Workspace:
    workspace = Workspace.load_or_none()
    if workspace is None:
        typer.echo(
            f"No {WORKSPACE_FILE} found here or above. Create one with:\n"
            f"    jfast workspace init <name>",
            err=True,
        )
        raise typer.Exit(1)
    return workspace


def _generate_gateway(workspace: Workspace, *, force: bool) -> Path:
    """Render the gateway from the workspace's current backends."""
    existing = workspace.gateway
    port = existing.port if existing else workspace.next_port()
    destination = Path(existing.path) if existing else Path("gateway")

    routes = [
        {"prefix": service.prefix, "target": service.internal_url} for service in workspace.backends
    ]

    scaffolder = Scaffolder()
    context = service_context(
        "gateway",
        kind="gateway",
        port=port,
        workspace_name=workspace.name,
        routes=routes,
    )
    with ui.working("scaffolding the gateway"):
        written = scaffolder.render_trees(
            service_trees("gateway", None, destination), context, force=force
        )
    _report(written)

    if existing is None:
        workspace.add(
            ServiceEntry(name="gateway", kind="gateway", port=port, path=str(destination))
        )
    workspace.save()

    typer.echo(
        f"\nGateway on port {port}, routing {len(routes)} backend(s):\n"
        + "\n".join(f"  {r['prefix']:<16} -> {r['target']}" for r in routes)
    )
    return destination


@workspace_app.command("init")
def workspace_init(
    name: str = typer.Argument(..., help="Workspace name."),
    base_port: int = typer.Option(8000, "--base-port", help="First port block starts above this."),
) -> None:
    """Create jfast.workspace.toml in the current directory."""
    path = Path(WORKSPACE_FILE)
    if path.exists():
        typer.echo(f"{path} already exists.", err=True)
        raise typer.Exit(1)
    # save() writes the .gitignore rule for the .env `workspace env` generates.
    workspace = Workspace(name=to_snake(name), base_port=base_port, file=path)
    workspace.save()

    typer.echo(
        f"created           {path}\n"
        f"\nServices created from here register themselves, get a free port block,\n"
        f"and a gateway is generated once there is more than one backend.\n"
        f"\n    jfast new service billing --with database\n"
        f"    jfast new service admin --kind spa --frontend vue"
    )


@workspace_app.command("list")
def workspace_list(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show every service, its kind and its port block."""
    workspace = _require_workspace()
    rows = [
        f"{s.name:<16} {s.kind:<8} :{s.port:<6} {s.path}"
        + (f"  ({s.frontend})" if s.frontend else "")
        for s in workspace.services
    ]
    human = "\n".join(
        [
            f"workspace : {workspace.name}",
            f"api url   : {workspace.api_base_url()}",
            "",
            *(rows or ["no services yet"]),
        ]
    )
    _echo(workspace.describe(), json_out, human)


@workspace_app.command("gateway")
def workspace_gateway(
    force: bool = typer.Option(False, "--force", help="Rewrite an existing gateway's routes."),
) -> None:
    """Generate or refresh the API gateway from the workspace's backends."""
    workspace = _require_workspace()

    if len(workspace.backends) < 2 and workspace.gateway is None:
        typer.echo(
            f"Only {len(workspace.backends)} backend service. A gateway would add a hop\n"
            f"and an outage surface for nothing -- skipping. It is generated\n"
            f"automatically once a second backend exists."
        )
        raise typer.Exit(0)

    if workspace.gateway is not None and not force:
        typer.echo(
            "A gateway already exists. Re-run with --force to rewrite its routes\n"
            "from the current workspace."
        )
        raise typer.Exit(0)

    _generate_gateway(workspace, force=force)


@workspace_app.command("compose")
def workspace_compose(
    output: Path = typer.Option(Path("docker-compose.yml"), "--output", "-o"),
    caddy: bool = typer.Option(True, "--caddy/--no-caddy", help="Include Caddy at the edge."),
    stdout: bool = typer.Option(False, "--stdout", help="Print instead of writing."),
) -> None:
    """One compose file for every service in the workspace."""
    from jfastframework.deploy.workspace import render_workspace_compose

    workspace = _require_workspace()
    rendered = render_workspace_compose(workspace, with_caddy=caddy)
    if stdout:
        typer.echo(rendered)
        return
    output.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {output}")


@workspace_app.command("caddy")
def workspace_caddy(
    output: Path = typer.Option(Path("Caddyfile"), "--output", "-o"),
    hostname: str = typer.Option("localhost", "--hostname", "-H"),
    production: bool = typer.Option(
        False, "--production", help="Enable automatic HTTPS (needs a real hostname and DNS)."
    ),
    wildcard_tenants: bool = typer.Option(
        False,
        "--wildcard-tenants",
        help="Serve *.HOST as tenant subdomains, with on-demand TLS.",
    ),
    stdout: bool = typer.Option(False, "--stdout"),
) -> None:
    """Caddyfile putting the whole workspace behind one hostname.

    Caddy is the edge: TLS, HTTP/3, compression, the built SPA. The JFast
    gateway, when there is one, is the application proxy behind it.
    """
    from jfastframework.deploy.workspace import render_caddyfile

    workspace = _require_workspace()
    rendered = render_caddyfile(
        workspace,
        hostname=hostname,
        local_dev=not production,
        wildcard_tenants=wildcard_tenants,
    )
    if stdout:
        typer.echo(rendered)
        return
    output.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {output}")


@workspace_app.command("k8s")
def workspace_k8s(
    output: Path = typer.Option(Path("k8s"), "--output", "-o", help="Directory to write into."),
    namespace: str | None = typer.Option(None, "--namespace", "-n"),
    host: str = typer.Option("example.com", "--host", "-H", help="Ingress hostname."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing manifests."),
) -> None:
    """Kubernetes manifests for the whole workspace, as a kustomize tree.

    Databases are deliberately not generated -- the README it writes says why.
    """
    from jfastframework.deploy.kubernetes import build as build_k8s

    workspace = _require_workspace()
    files = build_k8s(workspace, namespace=namespace, host=host)

    for relative, contents in sorted(files.items()):
        destination = output / relative
        if destination.exists() and not force:
            typer.echo(f"  skipped (exists) {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents, encoding="utf-8")
        typer.echo(f"  created          {destination}")

    typer.echo(
        f"\n{len(files)} file(s) in {output}/.\n"
        f"\n    kubectl apply -k {output}/overlays/dev\n"
        f"\nBefore production: set real images (not :latest), wire your secret\n"
        f"manager, and point the DSNs at a managed database. {output}/README.md\n"
        f"explains why the database is not generated."
    )


def _write_workspace_secrets(workspace: Workspace, *, dry_run: bool = False) -> int:
    """Generate a password per resource into the workspace `.env`, once.

    Existing values are never overwritten: rotating a password is a decision,
    and silently changing one would lock a running container out of its own
    volume. The file is gitignored -- the DSNs that reference these live in
    each service's generated .env as `${NAME_PASSWORD}`, so the secret itself
    appears in exactly one place.
    """
    import secrets as _secrets

    needed = [r for r in workspace.all_resources() if r.spec.needs_credentials]
    if not needed:
        return 0

    path = Path(".env")
    existing: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value

    generated = 0
    for resource in needed:
        if resource.secret_var not in existing:
            existing[resource.secret_var] = _secrets.token_urlsafe(24)
            generated += 1

    if generated and not dry_run:
        body = "# Secrets for this workspace. Generated by `jfast workspace env`.\n"
        body += "# Never commit this file.\n"
        body += "".join(f"{key}={value}\n" for key, value in sorted(existing.items()))
        path.write_text(body, encoding="utf-8")
    return generated


@workspace_app.command("validate")
def workspace_validate() -> None:
    """Check the resource graph before anything is generated from it.

    A port claimed twice, a binding to a resource that does not exist, two
    resources landing in the same variable, a resource nobody uses. Each one
    produces output that is wrong in a way nobody notices until a container
    fails to start.
    """
    workspace = _require_workspace()
    problems = workspace.validate()
    if not problems:
        count = len(workspace.all_resources())
        typer.echo(f"OK  {workspace.name}: {len(workspace.services)} services, {count} resources")
        raise typer.Exit(0)
    for problem in problems:
        typer.echo(f"  {problem}")
    typer.echo(f"\n{len(problems)} problem(s).")
    raise typer.Exit(1)


@workspace_app.command("migrate-resources")
def workspace_migrate_resources(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the result, write nothing."),
) -> None:
    """Rewrite legacy per-service `datastores` lists as named resources.

    Ports are preserved, so the compose file this produces afterwards is the
    one it produced before. Idempotent: running it twice changes nothing.
    """
    workspace = _require_workspace()
    if not any(service.datastores for service in workspace.services):
        typer.echo("Nothing to migrate: no service declares a legacy `datastores` list.")
        raise typer.Exit(0)

    promoted = workspace.migrate_resources()
    if dry_run:
        typer.echo(workspace.render())
        raise typer.Exit(0)

    workspace.save()
    for resource in promoted:
        typer.echo(f"  resource          {resource.name}  ({resource.type}, port {resource.port})")
    typer.echo(f"\nwrote {workspace.file}")
    typer.echo(
        "Regenerate what derives from it:\n    jfast workspace compose && jfast workspace env"
    )


@workspace_app.command("resource")
def workspace_resource(
    name: str = typer.Argument(..., help="Name for the instance, e.g. core-db."),
    type_: str = typer.Option("postgres", "--type", "-t", help="postgres | redis | mongo | qdrant"),
    port: int | None = typer.Option(None, "--port", help="Published port. Allocated if omitted."),
    image: str = typer.Option("", "--image", help="Override the default image."),
    database: str = typer.Option("", "--database", help="Database name, for postgres and mongo."),
    remove: bool = typer.Option(False, "--remove", help="Delete the resource instead."),
) -> None:
    """Add a datastore instance the workspace owns.

    It is attached to nothing until something is linked to it, which is the
    point: a second database is now a thing you can name.
    """
    workspace = _require_workspace()

    if remove:
        existing = workspace.resource(name)
        if existing is None:
            typer.echo(f"No resource named {name!r}.")
            raise typer.Exit(1)
        holders = [s.name for s in workspace.services if any(b.resource == name for b in s.uses)]
        if holders:
            typer.echo(
                f"{name!r} is still used by {', '.join(sorted(holders))}. "
                f"Unlink it first:\n    jfast unlink {holders[0]} {name}"
            )
            raise typer.Exit(1)
        workspace.resources.remove(existing)
        workspace.save()
        typer.echo(f"  removed           {name}")
        typer.echo(
            "The container and its volume are not deleted. `docker compose down -v` does that."
        )
        raise typer.Exit(0)

    if type_ not in RESOURCE_TYPES:
        known = ", ".join(sorted(RESOURCE_TYPES))
        typer.echo(f"Unknown type {type_!r}. Known: {known}.")
        raise typer.Exit(1)

    resource = Resource(
        name=name,
        type=type_,
        port=port if port is not None else workspace.next_resource_port(),
        image=image,
        database=database,
    )
    try:
        workspace.add_resource(resource)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    workspace.save()
    typer.echo(f"  resource          {resource.name}  ({resource.type}, port {resource.port})")
    if resource.spec.needs_credentials:
        typer.echo(f"  secret            {resource.secret_var}  (set it in the workspace .env)")
    typer.echo(f"\nConnect a service to it:\n    jfast link <service> {resource.name}")


@app.command("link")
def link_resource(
    service: str = typer.Argument(..., help="Service that needs the resource."),
    resource: str = typer.Argument(..., help="Resource it should reach."),
    as_: str = typer.Option("", "--as", help="Variable to bind it to. Defaults per type."),
) -> None:
    """Connect a service to a resource, and regenerate what depends on that.

    This is the answer to "I added a second database -- which service talks to
    it?". The binding is the only place that question has an answer, and the
    .env, the compose file and the graph all read it.
    """
    workspace = _require_workspace()
    try:
        binding = workspace.link(service, resource, env=as_)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    workspace.save()
    entry = workspace.get(service)
    assert entry is not None
    instance = workspace.resource(resource)
    assert instance is not None
    typer.echo(f"  linked            {service} -> {resource}")
    typer.echo(f"  variable          {binding.resolved_env(instance)}")
    typer.echo("\nRegenerate:\n    jfast workspace compose && jfast workspace env")


@app.command("unlink")
def unlink_resource(
    service: str = typer.Argument(...),
    resource: str = typer.Argument(...),
) -> None:
    """Disconnect a service from a resource."""
    workspace = _require_workspace()
    if not workspace.unlink(service, resource):
        typer.echo(f"{service!r} was not bound to {resource!r}.")
        raise typer.Exit(1)
    workspace.save()
    typer.echo(f"  unlinked          {service} -> {resource}")


@workspace_app.command("graph")
def workspace_graph(
    output_format: str = typer.Option("mermaid", "--format", "-f", help="mermaid | dot"),
) -> None:
    """Draw the workspace: services, resources, and the variable between them.

    Text, not an image. A committed PNG is a blob nobody can review and that
    goes stale in silence; mermaid renders on GitHub and diffs line by line.
    """
    workspace = _require_workspace()
    typer.echo(render_graph(workspace, output_format=output_format))


def _write_service_envs(workspace: Workspace) -> list[Path]:
    """Write each service's and frontend's .env from the resource graph.

    The compose file lists ``./<service>/.env`` as an ``env_file``, and compose
    treats a missing one as an error rather than an empty set -- so a project
    that has never run ``jfast workspace env`` cannot ``docker compose up`` at
    all. Generating them alongside the compose file keeps the two consistent by
    construction instead of by instruction.
    """
    written: list[Path] = []
    url = workspace.api_base_url()

    for backend in workspace.services:
        # Written even when the service binds nothing -- the gateway is the
        # ordinary case. Compose names every service's env_file unconditionally
        # and treats a missing one as an error rather than an empty set, so
        # skipping the empty ones is what made `docker compose config` fail.
        variables = workspace.environment_for(backend)
        env_path = Path(backend.path) / ".env"
        body = "# Written by jfast from jfast.workspace.toml.\n"
        body += "".join(f"{key}={value}\n" for key, value in sorted(variables.items()))
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(body, encoding="utf-8")
        written.append(env_path)

    for frontend in workspace.frontends:
        env_path = Path(frontend.path) / ".env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(
            "# Written by jfast from jfast.workspace.toml.\n"
            f"VITE_API_URL={url}\n"
            f"VITE_APP_NAME={frontend.name.replace('_', ' ').title()}\n",
            encoding="utf-8",
        )
        written.append(env_path)

    return written


@workspace_app.command("env")
def workspace_env(
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Rewrite every frontend's .env from the workspace.

    The API base URL is the gateway when there is one and the single backend
    when there is not -- which is exactly the value that goes stale by hand the
    day a gateway appears.
    """
    workspace = _require_workspace()
    url = workspace.api_base_url()

    written = _write_workspace_secrets(workspace, dry_run=dry_run)
    if written:
        typer.echo(f"  secrets           .env  ({written} generated, existing values kept)")

    # Backends first: their connection strings are derived from the resource
    # bindings, and used to be the one generated thing left to a human.
    for backend in workspace.services:
        # A service that binds nothing still gets a file: the compose generator
        # names ./<service>/.env for every one of them, and compose fails on a
        # missing env_file rather than treating it as empty.
        variables = workspace.environment_for(backend)
        env_path = Path(backend.path) / ".env"
        body = "# Written by `jfast workspace env` from jfast.workspace.toml.\n"
        body += "".join(f"{key}={value}\n" for key, value in sorted(variables.items()))
        if dry_run:
            typer.echo(f"would write {env_path}:\n{body}")
            continue
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(body, encoding="utf-8")
        typer.echo(f"  wrote             {env_path}  ({len(variables)} variable(s))")

    if not workspace.frontends:
        raise typer.Exit(0)

    for frontend in workspace.frontends:
        env_path = Path(frontend.path) / ".env"
        body = (
            "# Written by `jfast workspace env` from jfast.workspace.toml.\n"
            f"VITE_API_URL={url}\n"
            f"VITE_APP_NAME={frontend.name.replace('_', ' ').title()}\n"
        )
        if dry_run:
            typer.echo(f"would write {env_path}:\n{body}")
            continue
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(body, encoding="utf-8")
        typer.echo(f"  wrote             {env_path}  (VITE_API_URL={url})")


# --------------------------------------------------------------------------
# contracts
# --------------------------------------------------------------------------

contracts_app = typer.Typer(
    help="Per-project contracts: declare the rules, enforce them, publish them.",
    no_args_is_help=True,
)
app.add_typer(contracts_app, name="contracts")


def _coverage_table(coverage: dict[str, int]) -> str:
    """How many files each layer governs, in the order the contract declares.

    Sorted by name would put the answer somewhere different in every project.
    Declaration order is the order the contract was written to be read in, and
    for the layered and hexagonal templates it is also outermost-inward.
    """
    if not coverage:
        return ""
    width = max(len(name) for name in coverage)
    rows = [
        f"  {name:<{width}}  {count:>4} file{'' if count == 1 else 's'}"
        + ("   governs nothing" if count == 0 else "")
        for name, count in coverage.items()
    ]
    return "layers\n" + "\n".join(rows) + "\n\n"


def _require_contract(path: Path | None) -> tuple[Contract, Path]:
    source = path or Contract.find()
    if source is None or not source.is_file():
        typer.echo(
            f"No {CONTRACTS_FILE} found here or above.\nCreate one with:\n    jfast contracts init",
            err=True,
        )
        raise typer.Exit(1)
    return Contract.load(source), source.parent


@contracts_app.command("init")
def contracts_init(
    name: str | None = typer.Argument(None, help="Project name. Defaults to the directory."),
    layout: str = typer.Option(
        "layered",
        "--layout",
        "-l",
        help=f"Defaults matching your modules: {', '.join(MODULE_LAYOUTS)}.",
    ),
    target: Path = typer.Option(Path("."), "--target", "-t"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing contracts.toml."),
) -> None:
    """Write a contracts.toml with defaults for your layout.

    The defaults are a floor, not the answer. The value is in the lines you
    add: what this service does *not* own, which interfaces are stable, which
    invariants a checker cannot see.
    """
    if layout not in MODULE_LAYOUTS:
        raise typer.BadParameter(f"choose from: {', '.join(MODULE_LAYOUTS)}", param_hint="--layout")

    if (target / CONTRACTS_FILE).exists() and not force:
        typer.echo(
            f"{target / CONTRACTS_FILE} already exists. Re-run with --force to replace it.",
            err=True,
        )
        raise typer.Exit(1)

    project = to_snake(name or target.resolve().name)
    written = Scaffolder().render_tree(
        CONTRACT_TEMPLATE_FOR[layout],
        target,
        {"project": project, "layout": layout, "Project": project.replace("_", " ").title()},
        force=force,
    )
    _report(written)
    typer.echo(
        f"\nContract for '{project}' written ({layout} layout).\n"
        f"\nEdit it -- the defaults are a floor, not the point. Then:\n"
        f"    jfast contracts check\n"
        f"    jfast contracts render      # CONTRACTS.md, for review and for agents"
    )


@contracts_app.command("check")
def contracts_check(
    path: Path | None = typer.Option(None, "--file", "-f", help="Path to contracts.toml."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Verify the code against its contract. Non-zero exit on a violation.

    The per-layer file counts print on every run, pass or fail. A layer at 0 is
    the one finding this command reports late -- `check_coverage` only raises it
    once files it should have claimed are also unclaimed -- and a layer that
    drops from 40 files to 3 after a refactor never raises it at all.
    """
    from jfastframework.contracts.checker import layer_matches

    contract, root = _require_contract(path)
    violations = check(contract, root)
    coverage = layer_matches(contract, root)

    payload = {
        "project": contract.project,
        "ok": not violations,
        "coverage": coverage,
        "violations": [
            {"path": v.path, "line": v.line, "rule": v.rule, "message": v.message, "why": v.why}
            for v in violations
        ],
    }
    human = _coverage_table(coverage)
    human += "\n".join(str(v) for v in violations) or f"OK  {contract.project}: no violations"
    if violations:
        human += f"\n\n{len(violations)} violation(s). Fix them, or waive one inline with"
        human += "\n    # contracts: allow <reason>"
    _echo(payload, json_out, human)

    if violations:
        raise typer.Exit(Code.CONTRACT)


@contracts_app.command("show")
def contracts_show(
    path: Path | None = typer.Option(None, "--file", "-f"),
    json_out: bool = typer.Option(True, "--json/--text"),
) -> None:
    """The contract itself.

    `--json` is what an agent should read before writing a line here: scope,
    layer boundaries, forbidden calls, interfaces and invariants.
    """
    contract, _ = _require_contract(path)
    human = "\n".join(
        [
            f"project      : {contract.project}",
            f"owns         : {contract.owns or '-'}",
            f"does not own : {contract.does_not_own or '-'}",
            f"layers       : {', '.join(contract.layers) or '-'}",
            f"provides     : {', '.join(i.name for i in contract.provides) or '-'}",
            f"consumes     : {', '.join(i.name for i in contract.consumes) or '-'}",
            f"invariants   : {len(contract.invariants)}",
        ]
    )
    _echo(contract.describe(), json_out, human)


@contracts_app.command("render")
def contracts_render(
    path: Path | None = typer.Option(None, "--file", "-f"),
    output: Path = typer.Option(Path("CONTRACTS.md"), "--output", "-o"),
    stdout: bool = typer.Option(False, "--stdout"),
) -> None:
    """Write CONTRACTS.md from contracts.toml."""
    contract, _ = _require_contract(path)
    rendered = render(contract)
    if stdout:
        typer.echo(rendered)
        return
    output.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {output}")


@contracts_app.command("waivers")
def contracts_waivers(
    path: Path | None = typer.Option(None, "--file", "-f"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List every inline waiver.

    A waiver is a decision. Decisions nobody revisits are how a contract stops
    meaning anything, so they are listed rather than hidden.
    """
    _, root = _require_contract(path)
    found = waivers(root)
    payload = [{"path": w.path, "line": w.line, "reason": w.message} for w in found]
    human = "\n".join(f"{w.path}:{w.line}: {w.message}" for w in found) or "no waivers"
    _echo(payload, json_out, human)


# --------------------------------------------------------------------------
# interactive installer
# --------------------------------------------------------------------------


@app.command()
def start(
    name: str = typer.Argument("app", help="Project name."),
    port: int = typer.Option(8000, "--port", "-p", help="Base port for the first block."),
    frontend: str = typer.Option("vue", "--frontend", "-f", help=f"{', '.join(FRONTENDS)}."),
    queue_backend: str = typer.Option("postgres", "--queue", help="postgres (default) or redis."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """The opinionated default stack, in one command.

    A modular monolith in Python with PostgreSQL + pgvector, Redis, background
    jobs and a Vue frontend, behind Caddy. No questions asked.

    Why a monolith and not three services: you do not know the seams yet.
    Splitting later is a move; un-splitting is a rewrite. Modules keep the
    boundaries visible until the seams are obvious, and then
    `jfast new service` stands up its deployment and rewires the workspace;
    moving the code across is still yours.
    """
    if frontend not in FRONTENDS:
        raise typer.BadParameter(f"choose from: {', '.join(FRONTENDS)}", param_hint="--frontend")
    if queue_backend not in ("postgres", "redis"):
        raise typer.BadParameter("choose from: postgres, redis", param_hint="--queue")

    slug = to_snake(name)
    ui.banner("the opinionated default stack")

    workspace = Workspace.load_or_none()
    if workspace is None:
        workspace = Workspace(
            name=slug, base_port=port - PORT_BLOCK_SIZE, file=Path(WORKSPACE_FILE)
        )
        workspace.save()
        ui.created(str(workspace.file), "workspace")

    plugins = ["database", "cache", "queue"]
    api_dir, api_context = generate_service(
        slug,
        kind="api",
        port=port,
        plugins=plugins,
        frontend=None,
        target=Path(slug),
        workspace=workspace,
        force=force,
    )

    # A monolith with one module is a monolith with nothing in it. Generate a
    # real one so the first `pytest` and the first migration have a subject.
    scaffolder = Scaffolder()
    module = module_context("item", modules_dir="modules")
    scaffolder.render_trees(
        module_trees("layered", "api", api_dir / "modules", api_dir),
        module,
        force=force,
    )
    ui.created(f"{api_dir}/modules/item/", "a real module, so the first test has a subject")

    front_dir, _ = generate_service(
        f"{slug}_web",
        kind="spa",
        port=None,
        plugins=[],
        frontend=frontend,
        target=Path(f"{slug}-web"),
        workspace=workspace,
        force=force,
    )

    from jfastframework.deploy.workspace import render_caddyfile, render_workspace_compose

    Path("docker-compose.yml").write_text(render_workspace_compose(workspace), encoding="utf-8")
    Path("Caddyfile").write_text(render_caddyfile(workspace), encoding="utf-8")
    ui.created("docker-compose.yml", "every service, one network")
    ui.created("Caddyfile", "one origin, so the browser sees no CORS")

    # Without this the compose file this command just wrote cannot start: it
    # interpolates ${SHOP_DATABASE_PASSWORD} and friends, and compose refuses
    # rather than defaulting. So `docker compose up --build`, which is the very
    # next thing this command tells you to run, failed on a fresh project.
    secrets_written = _write_workspace_secrets(workspace)
    if secrets_written:
        ui.created(".env", f"{secrets_written} generated, gitignored")
    for env_path in _write_service_envs(workspace):
        ui.created(str(env_path), "from the resource graph")

    ui.summary(
        f"{slug} is ready",
        [
            ("stack", "modular monolith"),
            ("data", "PostgreSQL + pgvector, Redis"),
            ("jobs", f"background jobs on {queue_backend}"),
            ("web", f"{frontend} frontend behind Caddy"),
        ],
    )

    # Docker first: it is the one path that needs nothing installed, and the
    # one that matches what runs in production.
    ui.next_steps(
        "Run it",
        [
            ("docker compose up --build", "all of it, nothing else to install"),
            ("", ""),
            (f"cd {api_dir}", "or run the backend directly"),
            ("pip install -r requirements.txt", "in a virtualenv"),
            ("cp .env.example .env", "the defaults already match compose"),
            ("alembic upgrade head", "after `alembic revision --autogenerate`"),
            (f"uvicorn main:app --reload --port {api_context['port']}", ""),
            ("", ""),
            (f"cd {front_dir} && npm install && npm run dev", "the frontend"),
        ],
    )

    ui.note("When a module outgrows the monolith:")
    ui.note("    jfast new service billing --with database")


@app.command()
def init(
    name: str | None = typer.Argument(None, help="Service name. Prompted if omitted."),
) -> None:
    """Interactive installer: pick a kind, a frontend and your datastores.

    The flag-driven `jfast new service` does the same thing without questions.
    This is the front door for the first service in a project.
    """
    ui.banner("The interactive installer. Every answer is also a flag.")

    service_name = name or ui.ask("service name", default="app")

    kind = ui.select(
        "What are you building?",
        [
            ui.Choice("api", "", "A JSON API"),
            ui.Choice("web", "", "Server-rendered pages: Jinja2 and HTMX, no build step"),
            ui.Choice("spa", "", "A frontend project: Vue or React, with Tailwind"),
            ui.Choice("gateway", "", "A reverse proxy in front of other services"),
        ],
        default="api",
    )

    frontend: str | None = None
    if kind == "spa":
        frontend = ui.select(
            "Which frontend?",
            [
                ui.Choice("vue", "Vue 3", "Vite, Tailwind v4, built in CI"),
                ui.Choice("react", "React", "Vite, Tailwind v4, built in CI"),
            ],
            default="vue",
        )
        ui.note("Angular is not generated: no CI job builds it, so it would be untested.")

    chosen: list[str] = []
    if kind in ("api", "web"):
        ui.rule("Datastores")
        chosen += ui.multiselect(
            "What does it store?",
            [ui.Choice(name, PLUGIN_CATALOG[name].label, "") for name in DATASTORE_PLUGINS],
            defaults={"database"},
        )

        ui.rule("Capabilities")
        optional: list[ui.Choice] = []
        if {"database", "qdrant"} & set(chosen):
            optional.append(ui.Choice("rag", "Semantic search", "Retrieval over the store above"))
        optional += [
            ui.Choice("queue", "Background jobs", "Retries, backoff, dead-lettering"),
            ui.Choice("mail", "Email", "Templates, queued by default"),
            ui.Choice("auth", "Authentication", "JWT, scopes, rotation, revocation"),
            ui.Choice("storage", "File storage", "Local disks, S3 or MinIO"),
            ui.Choice("tenancy", "Multi-tenancy", "One deployment, many customers"),
            ui.Choice("notifications", "Push", "Firebase Cloud Messaging"),
            ui.Choice("sentry", "Error reporting", "Off unless a DSN is set"),
        ]
        if kind == "api":
            optional.append(
                ui.Choice("web", "Server-rendered pages", "Jinja2 and HTMX alongside the API")
            )
        chosen += ui.multiselect("Anything else?", optional, defaults=set())

    extras_chosen: list[str] = []
    if kind in ("api", "web"):
        ui.rule("Packages")
        ui.note(
            "None of these are installed by default: a service that serves "
            "JSON should not carry numpy. Each one can be added later with "
            "`jfast add`."
        )
        extras_chosen = ui.multiselect(
            "Anything from the catalogue?",
            [
                ui.Choice(
                    name,
                    capabilities.CATALOG[name].summary,
                    "heavy" if capabilities.CATALOG[name].heavy else "",
                )
                for name in capabilities.names()
            ],
            defaults=set(),
        )

    ui.rule("Agents")
    ui.note(
        "An AGENTS.md and a skill under .jfast/skills/, so an AI agent reads the\n"
        "  rules of this project before it writes in it rather than guessing them."
    )
    agent_docs = ui.confirm(
        "Write the agent surface?",
        default=True,
        hint="a design skill too, if there is a frontend",
    )

    workspace = Workspace.load_or_none()
    if workspace is None:
        ui.rule("Workspace")
        ui.note(
            "A workspace gives every service a free port block, writes one compose "
            "file,\n  and generates a gateway once there is more than one backend."
        )
        if ui.confirm(f"Create {WORKSPACE_FILE} here?", default=True):
            workspace = Workspace(name=to_snake(service_name), file=Path(WORKSPACE_FILE))
            workspace.save()
            ui.created(str(workspace.file))

    default_port = workspace.next_port() if workspace else 8000
    port = ui.ask_int("base port, a block of ten", default=default_port)

    ui.summary(
        "About to generate",
        [
            ("service", service_name),
            ("kind", kind + (f" ({frontend})" if frontend else "")),
            ("ports", f"{port}-{port + 9}"),
            ("plugins", ", ".join(chosen) if chosen else "observability, metrics"),
            ("packages", ", ".join(extras_chosen) if extras_chosen else "none"),
            ("workspace", str(workspace.file) if workspace else "none"),
            ("agents", "AGENTS.md + skills" if agent_docs else "none"),
        ],
    )

    try:
        destination, context = generate_service(
            service_name,
            kind=kind,
            port=port,
            plugins=chosen,
            frontend=frontend,
            target=None,
            workspace=workspace,
            agent_docs=agent_docs,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    for name in extras_chosen:
        spec = capabilities.get(name)
        requirements = destination / "requirements.txt"
        if requirements.is_file():
            requirements.write_text(
                _add_extra(requirements.read_text(encoding="utf-8"), spec.extra),
                encoding="utf-8",
            )
            ui.created(str(requirements), f"+ {spec.extra}")
        if spec.system_packages:
            ui.warn(f"{name} needs system packages: {' '.join(spec.system_packages)}")

    _print_next_steps(destination, context, kind)

    if "tenancy" in chosen:
        ui.warn("Multi-tenancy needs two things set before it isolates anything.")
        typer.echo(
            "Set [plugin.tenancy] base_domain in jfast.toml,\n"
            "then, for a certificate per tenant subdomain:\n"
            "    jfast workspace caddy --hostname <your-domain> --production --wildcard-tenants\n"
            "\nThat needs a wildcard DNS record and an /internal/tenant-exists endpoint --\n"
            "docs/multitenancy.md explains why the second one is not optional."
        )

    if "storage" in chosen:
        ui.warn("Signed storage links do not work until a key is set.")
        typer.echo(
            "A public and a private local disk are configured. Private links are\n"
            "signed, so set a key or they will not work:\n"
            "    JFAST_STORAGE_SIGNING_KEY=$(openssl rand -hex 32)"
        )

    if workspace is not None and ui.confirm(
        "Deploying to Kubernetes?", default=False, hint="writes a kustomize tree under k8s/"
    ):
        from jfastframework.deploy.kubernetes import build as build_k8s

        host = typer.prompt("  Ingress hostname", default="example.com")
        files = build_k8s(workspace, host=host)
        for relative, contents in sorted(files.items()):
            destination_path = Path("k8s") / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            destination_path.write_text(contents, encoding="utf-8")
        typer.echo(
            f"\n  created           k8s/ ({len(files)} files)\n"
            f"\n    kubectl apply -k k8s/overlays/dev\n"
            f"\nRegenerate after adding a service:\n"
            f"    jfast workspace k8s --force\n"
            f"\nk8s/README.md explains what is not generated -- the database, on\n"
            f"purpose -- and what to change before production."
        )
    elif workspace is not None:
        typer.echo("\nIf you need Kubernetes later:  jfast workspace k8s")


# ---------------------------------------------------------------------------
# Reading a project back.
#
# `describe` answers "what is this service" by building the app. That answer is
# the true one and it is unavailable in the two moments you most want it: when
# a dependency is not installed, and when the code does not import. It also
# says nothing whatsoever about modules -- generate two and neither name
# appears in its output.
#
# These three read the filesystem instead. Slightly less authoritative, always
# available, and they know what a module is.
# ---------------------------------------------------------------------------


def _project(path: Path) -> project_model.Project:
    root = path.resolve()
    if not (root / DEFAULT_CONFIG_FILE).is_file():
        typer.echo(
            f"no {DEFAULT_CONFIG_FILE} in {root}\n"
            "Run this inside a service, or point at one with --path."
        )
        raise typer.Exit(Code.CONFIG)
    return project_model.load(root)


def _known_plugins(root: Path) -> frozenset[str]:
    """Every plugin name this installation can resolve, importable or not.

    A name that is installed but broken is `doctor`'s finding, not `analyze`'s.
    Only a name nothing provides at all is reported here -- which is why the
    project's own ``[plugins.paths]`` have to be discovered too: a plugin
    declared three lines above the allow-list that reads it is not missing.
    """
    from jfastframework.plugins import registry

    config_path = root / DEFAULT_CONFIG_FILE
    extra = _declared_paths(_load(str(config_path))) if config_path.is_file() else {}
    available = registry.discover(extra_paths=extra, search_path=root)
    broken: dict[str, str] = getattr(registry.discover, "broken", {})
    # A dotted path that does not import is a name nothing provides, however
    # confidently jfast.toml names it. Only an installed distribution earns the
    # "broken, not missing" reading, so the declarations are dropped here.
    installed_but_broken = frozenset(name for name in broken if name not in extra)
    return frozenset(available) | installed_but_broken


@app.command("inspect")
def inspect_project(
    resource: str | None = typer.Argument(
        None, help="`module <name>` for one module. Omit for the whole project."
    ),
    name: str | None = typer.Argument(None, help="Which module."),
    path: Path = typer.Option(Path("."), "--path", "-p", help="Project root."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """What is in this project, read from disk without importing it.

    The one command to run first in an unfamiliar service, and the one an agent
    should run before it edits anything: modules, how each is shaped, what it
    serves, whether it is actually wired into the app.
    """
    project = _project(path)

    if resource is None:
        findings = project_model.analyze(project)
        payload = {**project.describe(), "findings": [f.describe() for f in findings]}
        _echo(payload, json_out, insight.render_project(project, findings))
        return

    if resource != "module":
        typer.echo("inspect takes `module <name>`, or no argument at all.")
        raise typer.Exit(Code.USAGE)
    if not name:
        typer.echo(f"which module? {', '.join(project.module_names) or 'there are none yet'}")
        raise typer.Exit(Code.USAGE)

    module = project.module(name)
    if module is None:
        typer.echo(f"no module {name!r}. Found: {', '.join(project.module_names) or 'none'}")
        raise typer.Exit(Code.USAGE)
    _echo(module.describe(), json_out, insight.render_module(module))


@app.command("analyze")
def analyze_project(
    path: Path = typer.Option(Path("."), "--path", "-p", help="Project root."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
    fail_on: str = typer.Option(
        "high",
        "--fail-on",
        help="Exit non-zero at this severity or worse: critical, high, medium, low, never.",
    ),
) -> None:
    """What is structurally wrong with this project.

    Complementary to `contracts check`, not a replacement: the contract enforces
    the rules you declared *inside* a file, this reports on the shape of the
    project *between* files -- import cycles, a module main.py never registers,
    two routers claiming one prefix, shared/ importing a module.

    Every check is decidable from the source text. Nothing here guesses, on
    purpose: a checker that is right nine times in ten gets muted after the
    second false positive, and the true findings go with it.
    """
    levels = (*project_model.SEVERITY_ORDER, "never")
    if fail_on not in levels:
        raise typer.BadParameter(f"choose from: {', '.join(levels)}", param_hint="--fail-on")

    project = _project(path)
    findings = project_model.analyze(project, known_plugins=_known_plugins(project.root))
    payload = {
        "schema_version": "1",
        "project": project.name,
        "ok": not findings,
        "counts": insight.severity_counts(findings),
        "findings": [finding.describe() for finding in findings],
    }
    _echo(payload, json_out, insight.render_analysis(findings))

    if fail_on == "never":
        return
    threshold = project_model.SEVERITY_ORDER.index(fail_on)
    if any(project_model.SEVERITY_ORDER.index(f.severity) <= threshold for f in findings):
        raise typer.Exit(Code.VALIDATION)


@app.command("graph")
def module_graph(
    module: str | None = typer.Option(None, "--module", "-m", help="Only this module's edges."),
    output_format: str = typer.Option(
        "ascii", "--format", "-f", help=", ".join(insight.GRAPH_FORMATS)
    ),
    path: Path = typer.Option(Path("."), "--path", "-p", help="Project root."),
) -> None:
    """The module dependency graph.

    `jfast workspace graph` draws services. This draws the modules inside one,
    which is the graph that decides whether a module can ever be extracted:
    a module nothing imports is a service waiting to happen, and a cycle is two
    modules that will never be either.
    """
    if output_format not in insight.GRAPH_FORMATS:
        raise typer.BadParameter(
            f"choose from: {', '.join(insight.GRAPH_FORMATS)}", param_hint="--format"
        )
    project = _project(path)
    if module and project.module(module) is None:
        typer.echo(f"no module {module!r}. Found: {', '.join(project.module_names) or 'none'}")
        raise typer.Exit(Code.USAGE)
    typer.echo(insight.render_graph(project, output_format=output_format, root=module))


# ---------------------------------------------------------------------------
# The lifecycle commands.
#
# Each lives in its own module and attaches itself rather than being spelled out
# here: five were written in parallel, and a shared block in this file is the
# one thing more than one author cannot edit at once.
#
# `explain` goes last on purpose -- it attaches to the `contracts` group above
# when it finds one, and would otherwise become a top-level `jfast explain`.
# ---------------------------------------------------------------------------
migrations_cli.register(app)
check_cli.register(app)
ai_cli.register(app)
upgrade_cli.register(app)
explain_cli.register(app)


if __name__ == "__main__":  # pragma: no cover
    app()
