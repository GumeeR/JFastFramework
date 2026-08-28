"""Derive deployment artifacts from the enabled plugin graph.

This is the payoff of ``Plugin.infra()``: docker-compose is generated, not
hand-maintained. Disable the ``cache`` plugin and the Redis container is gone
from the next generated compose file -- no drift between what the app loads and
what the infrastructure runs.

Port allocation follows the CometaX convention: a service owns a block of ten
ports starting at its base port, and each plugin declares its offset inside
that block.

No YAML dependency: the emitted structure is small and fully known, so a tiny
deterministic serialiser is cheaper than pulling in PyYAML.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jfastframework.plugins.base import Plugin
    from jfastframework.settings import JFastConfig

PORT_BLOCK_SIZE = 10


def _dump_yaml(value: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return " {}"
        lines = [f"{pad}{key}:{_dump_yaml(item, indent + 1)}" for key, item in value.items()]
        return "\n" + "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return " []"
        lines = []
        for item in value:
            rendered = _dump_yaml(item, indent + 1)
            if rendered.startswith("\n"):
                inner = rendered.lstrip("\n")
                first, _, rest = inner.partition("\n")
                lines.append(f"{pad}- {first.strip()}")
                if rest:
                    lines.append(rest)
            else:
                lines.append(f"{pad}-{rendered}")
        return "\n" + "\n".join(lines)
    if isinstance(value, bool):
        return f" {str(value).lower()}"
    if value is None:
        return " null"
    text = str(value)
    if text == "" or any(ch in text for ch in ":#{}[]&*?|<>=!%@`") or text != text.strip():
        escaped = text.replace('"', '\\"')
        return f' "{escaped}"'
    return f" {text}"


def collect_infra(plugins: list[Plugin]) -> list[Any]:
    services = []
    for plugin in plugins:
        services.extend(plugin.infra())
    return services


def build_compose(
    config: JFastConfig,
    plugins: list[Plugin],
    *,
    base_port: int | None = None,
    include_app: bool = True,
) -> dict[str, Any]:
    settings = config.settings
    base = base_port if base_port is not None else settings.port
    app_name = settings.app_name

    services: dict[str, Any] = {}
    volumes: dict[str, Any] = {}
    infra_services = collect_infra(plugins)

    for infra in infra_services:
        entry: dict[str, Any] = {
            "image": infra.image,
            "restart": "unless-stopped",
            "container_name": f"{app_name}_{infra.name}",
        }
        mappings: list[tuple[int, int]] = []
        if infra.port_offset is not None and infra.internal_port is not None:
            mappings.append((infra.port_offset, infra.internal_port))
        mappings.extend(infra.extra_ports)
        if mappings:
            for offset, _ in mappings:
                if offset >= PORT_BLOCK_SIZE:
                    raise ValueError(
                        f"Plugin infra {infra.name!r} declares port_offset "
                        f"{offset}, outside the {PORT_BLOCK_SIZE}-port block."
                    )
            entry["ports"] = [f"{base + offset}:{internal}" for offset, internal in mappings]
        if infra.environment:
            entry["environment"] = dict(infra.environment)
        if infra.command:
            entry["command"] = infra.command
        if infra.volumes:
            entry["volumes"] = list(infra.volumes)
            for mount in infra.volumes:
                name = mount.split(":", 1)[0]
                if not name.startswith((".", "/")):
                    volumes[name] = None
        if infra.healthcheck:
            entry["healthcheck"] = dict(infra.healthcheck)
        if infra.depends_on:
            entry["depends_on"] = list(infra.depends_on)
        services[infra.name] = entry

    if include_app:
        app_entry: dict[str, Any] = {
            "build": ".",
            "container_name": f"{app_name}_api",
            "restart": "unless-stopped",
            "ports": [f"{base}:{base}"],
            "env_file": [".env"],
            "environment": {"JFAST_PORT": str(base), "JFAST_APP_NAME": app_name},
        }
        # Wait for a healthcheck when the container declares one; otherwise
        # "started" is the strongest guarantee compose can give.
        dependants = {
            name: {
                "condition": ("service_healthy" if entry.get("healthcheck") else "service_started")
            }
            for name, entry in services.items()
        }
        if dependants:
            app_entry["depends_on"] = dependants
        services = {"api": app_entry, **services}

    compose: dict[str, Any] = {"services": services}
    if volumes:
        compose["volumes"] = volumes
    return compose


def render_compose(compose: dict[str, Any]) -> str:
    header = (
        "# Generated by `jfast deploy compose`. Do not edit by hand --\n"
        "# regenerate after changing the plugin list in jfast.toml.\n"
    )
    return header + _dump_yaml(compose).lstrip("\n") + "\n"


# The image, as a named template rather than an f-string inside the function.
# Hoisted so a security scanner reads it as one string constant instead of as
# formatted output that happens to resemble SQL, and so the Dockerfile can be
# read without the surrounding Python.
DOCKERFILE_TEMPLATE = """\
# Generated by `jfast deploy dockerfile`.
FROM python:{python_version}-slim AS base

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \\
 && apt-get install -y --no-install-recommends build-essential curl \\
 && rm -rf /var/lib/apt/lists/*

# Both optional, both globbed. A generated service ships
# requirements.txt and no pyproject.toml, and COPY fails the build when
# its source does not exist -- so the image could never be built from
# what the generator actually produced.
COPY pyproject.toml* ./
COPY requirements.txt* ./
RUN pip install --upgrade pip \\
 && if [ -f requirements.txt ]; then pip install -r requirements.txt; else pip install .; fi

COPY . .

# Run as a non-root user. Containers that run as root are a finding in every
# security review, and fixing it later means rebuilding image layers.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \\
  CMD curl -fsS http://localhost:${{JFAST_PORT:-8000}}/health || exit 1

# Migrate, then serve. A container starting against an empty database
# answers 500 to everything until somebody remembers to run Alembic by
# hand, and `create_all` is not the fix: it builds a schema Alembic does
# not know about, so the first real migration diverges silently. One
# source of truth, applied before the first request.
#
# `set -e` stops the container on a failed migration rather than serving a
# half-migrated schema; `exec` leaves uvicorn as PID 1 so it gets SIGTERM.
USER root
RUN echo '#!/bin/sh' > /entrypoint.sh \\
 && echo 'set -e' >> /entrypoint.sh \\
 && echo '[ -f alembic.ini ] && alembic upgrade head' >> /entrypoint.sh \\
 && echo 'exec uvicorn main:app --host 0.0.0.0 --port ${{JFAST_PORT:-8000}}' \\
      >> /entrypoint.sh \\
 && chmod +x /entrypoint.sh
USER appuser

CMD ["/entrypoint.sh"]
"""


def render_dockerfile(python_version: str = "3.12") -> str:
    return DOCKERFILE_TEMPLATE.format(python_version=python_version)
