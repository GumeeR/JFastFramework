"""The ``jfast`` command line.

Two audiences, one interface:

* humans get readable output;
* agents get ``--json`` on every inspection command, so an AI assistant reads
  machine state instead of grepping the source tree.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from typing import Any

import typer

from jfastframework.cli.scaffold import (
    MODULE_LAYOUTS,
    MODULE_UIS,
    SERVICE_KINDS,
    Scaffolder,
    module_context,
    module_trees,
    service_context,
)
from jfastframework.settings import DEFAULT_CONFIG_FILE, JFastConfig

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


@new_app.command("service")
def new_service(
    name: str = typer.Argument(..., help="Service name, e.g. 'billing'."),
    kind: str = typer.Option(
        "api",
        "--kind",
        "-k",
        help="api = JSON service. web = server-rendered frontend service (Jinja + HTMX).",
    ),
    port: int = typer.Option(8000, "--port", "-p", help="Base port of the service's port block."),
    target: Path | None = typer.Option(
        None, "--target", "-t", help="Destination directory. Defaults to ./<name>."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Scaffold a whole service.

    A frontend is a service like any other -- it just renders HTML instead of
    JSON, and both kinds deploy, log and report health identically:

        jfast new service billing
        jfast new service storefront --kind web --port 8020
    """
    if kind not in SERVICE_KINDS:
        raise typer.BadParameter(f"choose from: {', '.join(SERVICE_KINDS)}", param_hint="--kind")

    scaffolder = Scaffolder()
    context = service_context(name, kind=kind, port=port)
    destination = target or Path(context["service"])

    trees = [("service_base", destination)]
    if kind == "web":
        trees.append(("service_web", destination))

    written = scaffolder.render_trees(trees, context, force=force, dry_run=dry_run)
    _report(written)

    extras = "server,db,metrics,web" if kind == "web" else "server,db,metrics"
    typer.echo(
        f"\nService '{context['service_slug']}' scaffolded ({kind}).\n"
        f"\n    cd {destination}\n"
        f'    pip install "jfastframework[{extras}]"\n'
        f"    cp .env.example .env\n"
        f"    uvicorn main:app --reload --port {port}\n"
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


if __name__ == "__main__":  # pragma: no cover
    app()
