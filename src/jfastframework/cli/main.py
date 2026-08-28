"""The ``jfast`` command line.

Two audiences, one interface:

* humans get readable output;
* agents get ``--json`` on every inspection command, so an AI assistant reads
  machine state instead of grepping the source tree.
"""

from __future__ import annotations

import json as jsonlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import typer

from jfastframework import languages
from jfastframework.cli.patcher import (
    PatchError,
    ensure_import,
    ensure_named_import,
    insert_at_marker,
)
from jfastframework.cli.scaffold import (
    DATASTORE_PLUGINS,
    FRONTENDS,
    MODULE_LAYOUTS,
    MODULE_UIS,
    PLUGIN_CATALOG,
    SERVICE_KINDS,
    Scaffolder,
    detect_frontend,
    module_context,
    module_trees,
    service_context,
    service_trees,
    to_snake,
    view_context,
    view_trees,
)
from jfastframework.contracts import CONTRACTS_FILE, Contract, check, render, waivers
from jfastframework.graph import render_graph
from jfastframework.resources import RESOURCE_TYPES, Resource
from jfastframework.settings import DEFAULT_CONFIG_FILE, JFastConfig
from jfastframework.workspace import PORT_BLOCK_SIZE, WORKSPACE_FILE, ServiceEntry, Workspace

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
        available = registry.discover()
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


def _report(written: list[Any]) -> None:
    for item in written:
        marker = "created" if item.created else "skipped (exists)"
        typer.echo(f"  {marker:<18} {item.path}")


@new_app.command("module")
def new_module(
    name: str = typer.Argument(..., help="Module name, e.g. 'order' or 'BillingAccount'."),
    layout: str = typer.Option(
        "layered",
        "--layout",
        "-l",
        help=("layered = router/service/repository. screaming = domain + one file per use case."),
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
    if layout not in MODULE_LAYOUTS:
        raise typer.BadParameter(f"choose from: {', '.join(MODULE_LAYOUTS)}", param_hint="--layout")
    if ui not in MODULE_UIS:
        raise typer.BadParameter(f"choose from: {', '.join(MODULE_UIS)}", param_hint="--ui")

    scaffolder = Scaffolder()
    context = module_context(name, layout=layout, ui=ui, table=table, modules_dir=target.name)
    trees = module_trees(layout, ui, target, root)
    written = scaffolder.render_trees(trees, context, force=force, dry_run=dry_run)
    _report(written)

    module = context["module"]
    lines = [
        f"\nModule '{module}' scaffolded ({layout} layout, {ui} ui).",
        "\nMount it in main.py:",
        f"    from {target.name}.{module} import router as {module}_router",
    ]
    if ui == "htmx":
        lines += [
            f"    from {target.name}.{module}.web import router as {module}_web_router",
            f"    ROUTERS += [{module}_router, {module}_web_router]",
            "\nThe HTMX pages need the 'web' plugin:",
            '    [plugins] enabled = [..., "web"]',
        ]
    else:
        lines.append(f"    ROUTERS.append({module}_router)")
    lines += [
        "\nThen:",
        f"    pytest {target}/{module}/tests",
        f"    alembic revision --autogenerate -m 'add {context['table']}'",
    ]
    typer.echo("\n".join(lines))


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
    force: bool = False,
    dry_run: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Render a service and register it in the workspace, if there is one.

    Shared by `jfast new service`, `jfast init` and `jfast start` so every path
    produces exactly the same tree — a wizard that generates something slightly
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
        workspace_name=workspace.name if workspace else slug,
        api_base_url=workspace.api_base_url() if workspace else f"http://localhost:{resolved_port}",
    )
    destination = target or Path(slug)

    trees = service_trees(kind, frontend, destination, language=language, grpc=grpc)
    written = scaffolder.render_trees(trees, context, force=force, dry_run=dry_run)
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
        typer.echo(f"  registered        {workspace.file}")

    return destination, context


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
            "Comma-separated plugins: database,cache,mongo,qdrant,rag,web,sentry. "
            "Defaults to database for backend services."
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

    A frontend is a service like any other — it deploys, logs and reports
    health identically:

        jfast new service billing --with database,cache
        jfast new service storefront --kind web
        jfast new service admin --kind spa --frontend vue
    """
    if kind not in SERVICE_KINDS:
        raise typer.BadParameter(f"choose from: {', '.join(SERVICE_KINDS)}", param_hint="--kind")

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
    slug = context["service_slug"]
    port = context["port"]

    if context.get("language") == "go":
        typer.echo(
            f"\nService '{slug}' scaffolded (go, {kind}).\n"
            f"  datastores: {', '.join(context['datastores']) or 'none'}\n"
            f"\n    cd {destination}\n"
            f"    cp .env.example .env\n"
            f"    go test ./...\n"
            f"    go run .\n"
            + (
                "\nThe gRPC contract is in proto/. Generating stubs is a build step\n"
                "you own — see proto/README.md.\n"
                if context.get("grpc")
                else ""
            )
        )
        return

    if kind == "spa":
        typer.echo(
            f"\nFrontend '{slug}' scaffolded ({context['frontend']}).\n"
            f"\n    cd {destination}\n"
            f"    npm install\n"
            f"    npm run dev            # http://localhost:{port}\n"
            f"\nVITE_API_URL is already set to {context['api_base_url']}.\n"
            f"Add a module:\n"
            f"    jfast new view Facturas"
        )
        return

    if kind == "gateway":
        typer.echo(
            f"\nGateway '{slug}' scaffolded.\n"
            f"\n    cd {destination}\n"
            f'    pip install "jfastframework[{context["extras"]}]"\n'
            f"    uvicorn main:app --reload --port {port}"
        )
        return

    typer.echo(
        f"\nService '{slug}' scaffolded ({kind}).\n"
        f"  plugins: {', '.join(context['enabled_plugins'])}\n"
        f"\n    cd {destination}\n"
        f"    pip install -r requirements.txt\n"
        f"    cp .env.example .env\n"
        + (
            "    alembic revision --autogenerate -m 'initial'\n    alembic upgrade head\n"
            if context["has_database"]
            else ""
        )
        + f"    uvicorn main:app --reload --port {port}\n"
        f"\nThen add a module:\n"
        f"    jfast new module invoice" + (" --ui htmx" if kind == "web" else "")
    )


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
    """Generate a production Dockerfile."""
    from jfastframework.deploy import render_dockerfile

    rendered = render_dockerfile(python)
    if stdout:
        typer.echo(rendered)
        return
    output.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {output}")


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


@app.command()
def doctor(
    config: str = typer.Option(DEFAULT_CONFIG_FILE, "--config", "-c"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Check that the configuration resolves and every enabled plugin imports."""
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
        raise typer.Exit(1) from exc

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

    ok = not problems
    payload = {"ok": ok, "checks": checks, "problems": problems}
    human_lines = [f"{k:<14} {v}" for k, v in checks.items()]
    human_lines += [f"PROBLEM        {p}" for p in problems]
    human_lines.append("OK" if ok else f"{len(problems)} problem(s)")
    _echo(payload, json_out, "\n".join(human_lines))
    if not ok:
        raise typer.Exit(1)


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
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Scaffold a frontend module and register it.

    Creates ``src/Modulo<Name>/`` with Pages, Routes, Services and Components,
    then splices the route into the router and the entry into the sidebar at
    their marker comments.

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
        f"\nThe service calls {context['view_path']} on VITE_API_URL. Point it at a real\n"
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
    workspace = Workspace(name=to_snake(name), base_port=base_port, file=path)
    workspace.save()

    # `jfast workspace env` generates one password per resource into .env here.
    # Creating the ignore rule now means it can never be committed by accident.
    ignore = Path(".gitignore")
    rules = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    if ".env" not in rules.split():
        ignore.write_text(
            rules + ("" if rules.endswith("\n") or not rules else "\n") + ".env\n",
            encoding="utf-8",
        )

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
            f"and an outage surface for nothing — skipping. It is generated\n"
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

    Databases are deliberately not generated — the README it writes says why.
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


@workspace_app.command("env")
def workspace_env(
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Rewrite every frontend's .env from the workspace.

    The API base URL is the gateway when there is one and the single backend
    when there is not — which is exactly the value that goes stale by hand the
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
        variables = workspace.environment_for(backend)
        if not variables:
            continue
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
        f"contracts_{layout}",
        target,
        {"project": project, "layout": layout, "Project": project.replace("_", " ").title()},
        force=force,
    )
    _report(written)
    typer.echo(
        f"\nContract for '{project}' written ({layout} layout).\n"
        f"\nEdit it — the defaults are a floor, not the point. Then:\n"
        f"    jfast contracts check\n"
        f"    jfast contracts render      # CONTRACTS.md, for review and for agents"
    )


@contracts_app.command("check")
def contracts_check(
    path: Path | None = typer.Option(None, "--file", "-f", help="Path to contracts.toml."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Verify the code against its contract. Non-zero exit on a violation."""
    contract, root = _require_contract(path)
    violations = check(contract, root)

    payload = {
        "project": contract.project,
        "ok": not violations,
        "violations": [
            {"path": v.path, "line": v.line, "rule": v.rule, "message": v.message, "why": v.why}
            for v in violations
        ],
    }
    human = "\n".join(str(v) for v in violations) or f"OK  {contract.project}: no violations"
    if violations:
        human += f"\n\n{len(violations)} violation(s). Fix them, or waive one inline with"
        human += "\n    # contracts: allow <reason>"
    _echo(payload, json_out, human)

    if violations:
        raise typer.Exit(1)


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
    `jfast new service` promotes one.
    """
    if frontend not in FRONTENDS:
        raise typer.BadParameter(f"choose from: {', '.join(FRONTENDS)}", param_hint="--frontend")
    if queue_backend not in ("postgres", "redis"):
        raise typer.BadParameter("choose from: postgres, redis", param_hint="--queue")

    slug = to_snake(name)
    typer.echo(f"jfast start — {slug}\n")

    workspace = Workspace.load_or_none()
    if workspace is None:
        workspace = Workspace(
            name=slug, base_port=port - PORT_BLOCK_SIZE, file=Path(WORKSPACE_FILE)
        )
        workspace.save()
        typer.echo(f"  created           {workspace.file}")

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
    typer.echo(f"  created           {api_dir}/modules/item/")

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
    typer.echo("  created           docker-compose.yml\n  created           Caddyfile")

    typer.echo(
        f"\nReady. {slug} is a modular monolith: PostgreSQL + pgvector, Redis,\n"
        f"background jobs on {queue_backend}, and a {frontend} frontend behind Caddy.\n"
        f"\nBackend:\n"
        f"    cd {api_dir}\n"
        f"    pip install -r requirements.txt && cp .env.example .env\n"
        f"    alembic revision --autogenerate -m 'initial' && alembic upgrade head\n"
        f"    uvicorn main:app --reload --port {api_context['port']}\n"
        f"\nFrontend:\n"
        f"    cd {front_dir} && npm install && npm run dev\n"
        f"\nOr all of it at once:\n"
        f"    docker compose up --build\n"
        f"\nWhen a module outgrows the monolith:\n"
        f"    jfast new service billing --with database"
    )


@app.command()
def init(
    name: str | None = typer.Argument(None, help="Service name. Prompted if omitted."),
) -> None:
    """Interactive installer: pick a kind, a frontend and your datastores.

    The flag-driven `jfast new service` does the same thing without questions.
    This is the front door for the first service in a project.
    """
    typer.echo("JFastFramework installer\n")

    service_name = name or typer.prompt("Service name", default="app")

    typer.echo("\nWhat are you building?")
    typer.echo("  1) api      JSON API")
    typer.echo("  2) web      Server-rendered pages (Jinja2 + HTMX, no build step)")
    typer.echo("  3) spa      Frontend project (Vue or React + Tailwind)")
    typer.echo("  4) gateway  Reverse proxy in front of other services")
    kind_choice = typer.prompt("Choice", default="1")
    kind = {"1": "api", "2": "web", "3": "spa", "4": "gateway"}.get(kind_choice, kind_choice)
    if kind not in SERVICE_KINDS:
        raise typer.BadParameter(f"choose from: {', '.join(SERVICE_KINDS)}", param_hint="kind")

    frontend: str | None = None
    if kind == "spa":
        typer.echo(f"\nFrontend framework ({', '.join(FRONTENDS)}):")
        typer.echo("  Angular is not generated yet — see PLAN.md phase 3.")
        frontend = typer.prompt("Framework", default="vue")
        if frontend not in FRONTENDS:
            raise typer.BadParameter(f"choose from: {', '.join(FRONTENDS)}", param_hint="framework")

    chosen: list[str] = []
    if kind in ("api", "web"):
        typer.echo("\nDatastores (y/n each):")
        for plugin_name in DATASTORE_PLUGINS:
            spec = PLUGIN_CATALOG[plugin_name]
            default = plugin_name == "database"
            if typer.confirm(f"  {plugin_name:<10} {spec.label}", default=default):
                chosen.append(plugin_name)

        if {"database", "qdrant"} & set(chosen) and typer.confirm(
            "\n  rag        Semantic search over the store above", default=False
        ):
            chosen.append("rag")
        if typer.confirm("\n  queue      Background jobs (on the store above)", default=False):
            chosen.append("queue")
        if typer.confirm("  auth       JWT verification, scopes, revocation", default=False):
            chosen.append("auth")
        if typer.confirm("  storage    File storage (local disks, S3 or MinIO)", default=False):
            chosen.append("storage")
        if typer.confirm(
            "  tenancy    Multi-tenant (one deployment, many customers)", default=False
        ):
            chosen.append("tenancy")
        if typer.confirm("  notifications  Push via Firebase (FCM)", default=False):
            chosen.append("notifications")
        if typer.confirm("  sentry     Error reporting", default=False):
            chosen.append("sentry")
        if kind == "api" and typer.confirm(
            "  web        Server-rendered pages alongside the API", default=False
        ):
            chosen.append("web")

    workspace = Workspace.load_or_none()
    if workspace is None and typer.confirm(
        f"\nNo {WORKSPACE_FILE} here. Create one? "
        f"(gives every service a free port block and generates a gateway later)",
        default=True,
    ):
        workspace = Workspace(name=to_snake(service_name), file=Path(WORKSPACE_FILE))
        workspace.save()
        typer.echo(f"created           {workspace.file}")

    default_port = workspace.next_port() if workspace else 8000
    port = typer.prompt("\nBase port (a block of 10)", default=default_port, type=int)

    typer.echo("")
    try:
        destination, context = generate_service(
            service_name,
            kind=kind,
            port=port,
            plugins=chosen,
            frontend=frontend,
            target=None,
            workspace=workspace,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    _print_next_steps(destination, context, kind)

    if "tenancy" in chosen:
        typer.echo(
            "\nMulti-tenancy is on. Set [plugin.tenancy] base_domain in jfast.toml,\n"
            "then, for a certificate per tenant subdomain:\n"
            "    jfast workspace caddy --hostname <your-domain> --production --wildcard-tenants\n"
            "\nThat needs a wildcard DNS record and an /internal/tenant-exists endpoint --\n"
            "docs/multitenancy.md explains why the second one is not optional."
        )

    if "storage" in chosen:
        typer.echo(
            "\nStorage is on, with a public and a private local disk. Private links are\n"
            "signed, so set a key or they will not work:\n"
            "    JFAST_STORAGE_SIGNING_KEY=$(openssl rand -hex 32)"
        )

    if workspace is not None and typer.confirm(
        "\nDeploying to Kubernetes? (writes a kustomize tree under k8s/)",
        default=False,
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
            f"\nk8s/README.md explains what is not generated — the database, on\n"
            f"purpose — and what to change before production."
        )
    elif workspace is not None:
        typer.echo("\nIf you need Kubernetes later:  jfast workspace k8s")


if __name__ == "__main__":  # pragma: no cover
    app()
