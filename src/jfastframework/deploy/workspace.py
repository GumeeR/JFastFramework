"""One compose file and one Caddyfile for a whole workspace.

Per-service compose files are right while you are working on one service. The
moment there are three plus a gateway, you want a single `docker compose up`
and one hostname — that is what these two generators produce.

Both derive from ``jfast.workspace.toml``. Regenerate after adding a service
rather than editing the output; a hand-edited generated file is a merge
conflict waiting to happen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jfastframework.deploy.compose import _dump_yaml

if TYPE_CHECKING:
    from jfastframework.workspace import ServiceEntry, Workspace

# Datastore containers a non-Python service can declare in jfast.service.toml.
# Same images and offsets the Python plugins use, so a Go service and a Python
# service get identical infrastructure.
DATASTORE_IMAGES: dict[str, tuple[str, int, int]] = {
    "database": ("pgvector/pgvector:pg16", 1, 5432),
    "cache": ("redis:7-alpine", 3, 6379),
    "mongo": ("mongo:7", 4, 27017),
    "qdrant": ("qdrant/qdrant:v1.12.4", 7, 6333),
}

CADDY_HTTP_PORT = 80
CADDY_HTTPS_PORT = 443


def _datastore_services(service: ServiceEntry) -> dict[str, Any]:
    """Containers this service's datastores need, namespaced by service."""
    out: dict[str, Any] = {}
    for name in service.datastores:
        spec = DATASTORE_IMAGES.get(name)
        if spec is None:
            continue
        image, offset, internal = spec
        # Namespaced per service: two services declaring `database` get two
        # PostgreSQL instances, which is the point of splitting them up. Share
        # one deliberately by pointing both DSNs at the same host instead.
        container = f"{service.name}-{name}"
        entry: dict[str, Any] = {
            "image": image,
            "restart": "unless-stopped",
            "container_name": container,
            "ports": [f"{service.port + offset}:{internal}"],
            "volumes": [f"{service.name}_{name}_data:/data"],
        }
        if name == "database":
            entry["environment"] = {
                "POSTGRES_DB": "app",
                "POSTGRES_USER": "app",
                "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}",
            }
            entry["volumes"] = [f"{service.name}_database_data:/var/lib/postgresql/data"]
            entry["healthcheck"] = {
                "test": ["CMD-SHELL", "pg_isready -U app"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 10,
            }
        out[container] = entry
    return out


def build_workspace_compose(workspace: Workspace, *, with_caddy: bool = True) -> dict[str, Any]:
    services: dict[str, Any] = {}
    volumes: dict[str, Any] = {}

    for service in workspace.services:
        if service.is_frontend:
            # A built SPA is static files. Caddy serves them; there is no
            # container to run in production. `npm run dev` is the dev story.
            continue

        entry: dict[str, Any] = {
            "build": {"context": f"./{service.path}"},
            "container_name": service.name,
            "restart": "unless-stopped",
            "env_file": [f"./{service.path}/.env"],
            "environment": {
                "JFAST_APP_NAME": service.name,
                "JFAST_PORT": str(service.port),
            },
            "ports": [f"{service.port}:{service.port}"],
        }

        stores = _datastore_services(service)
        if stores:
            entry["depends_on"] = {
                name: {
                    "condition": (
                        "service_healthy" if store.get("healthcheck") else "service_started"
                    )
                }
                for name, store in stores.items()
            }
            for name, store in stores.items():
                services[name] = store
                for mount in store.get("volumes", []):
                    volume = mount.split(":", 1)[0]
                    if not volume.startswith((".", "/")):
                        volumes[volume] = None

        if service.grpc:
            entry["ports"].append(f"{service.grpc_port}:{service.grpc_port}")

        services[service.name] = entry

    if with_caddy:
        # Caddy last so it depends on everything already collected.
        services["caddy"] = {
            "image": "caddy:2-alpine",
            "container_name": f"{workspace.name}-caddy",
            "restart": "unless-stopped",
            "ports": [f"{CADDY_HTTP_PORT}:80", f"{CADDY_HTTPS_PORT}:443"],
            "volumes": [
                "./Caddyfile:/etc/caddy/Caddyfile:ro",
                "./dist:/srv:ro",
                "caddy_data:/data",
                "caddy_config:/config",
            ],
            "depends_on": [s.name for s in workspace.services if not s.is_frontend],
        }
        volumes["caddy_data"] = None
        volumes["caddy_config"] = None

    compose: dict[str, Any] = {"services": services}
    if volumes:
        compose["volumes"] = volumes
    return compose


def render_workspace_compose(workspace: Workspace, *, with_caddy: bool = True) -> str:
    header = (
        f"# {workspace.name} — generated by `jfast workspace compose`.\n"
        "# Do not edit by hand: regenerate after adding a service.\n"
        "#\n"
        "# Frontends are absent on purpose. A built SPA is static files, served\n"
        "# by Caddy from ./dist — there is no container to run.\n"
    )
    compose = build_workspace_compose(workspace, with_caddy=with_caddy)
    return header + _dump_yaml(compose).lstrip("\n") + "\n"


def render_caddyfile(
    workspace: Workspace,
    *,
    hostname: str = "localhost",
    local_dev: bool = True,
    wildcard_tenants: bool = False,
) -> str:
    """Caddyfile routing the whole workspace behind one hostname.

    Caddy is the *edge*: TLS, HTTP/3, compression, static assets. The JFast
    gateway, when there is one, is the *application* proxy: auth, per-tenant
    limits, request-id minting.

    Running both is only worth it when you need that application layer. If you
    do not, this file routes straight to the services and the gateway is one
    hop you can delete.
    """
    lines: list[str] = [
        f"# {workspace.name} — generated by `jfast workspace caddy`.",
        "# Regenerate after adding a service; do not edit by hand.",
        "",
    ]

    if local_dev:
        lines += [
            "{",
            "\t# Local development: no ACME, no certificate on first boot.",
            "\tauto_https off",
            "}",
            "",
        ]

    # A wildcard site block serves every tenant subdomain from one config.
    # Caddy cannot get a certificate per tenant from a wildcard match, so
    # on-demand TLS issues one the first time each hostname is seen -- which
    # is why the `ask` endpoint below is not optional: without it, anyone
    # pointing a DNS record at you can make you request certificates for it.
    if wildcard_tenants and not local_dev:
        lines += [
            "{",
            "\ton_demand_tls {",
            f"\t\task http://{workspace.backends[0].name if workspace.backends else 'api'}"
            f":{workspace.backends[0].port if workspace.backends else 8000}/internal/tenant-exists",
            "\t\tinterval 2m",
            "\t\tburst 5",
            "\t}",
            "}",
            "",
        ]

    site = f"{'http://' if local_dev else ''}{hostname}"
    if wildcard_tenants:
        site = f"{site}, {'http://' if local_dev else ''}*.{hostname}"

    lines += [f"{site} {{", "\tencode zstd gzip", ""]

    if wildcard_tenants and not local_dev:
        lines += ["\ttls {", "\t\ton_demand", "\t}", ""]

    if wildcard_tenants:
        lines += [
            "\t# The tenant is the subdomain. The application reads it from the",
            "\t# Host header; this header is a convenience for logs and must not",
            "\t# be trusted on its own -- see [plugin.tenancy] sources.",
            "\theader_up X-Forwarded-Host {host}",
            "",
        ]

    gateway = workspace.gateway
    backends = [s for s in workspace.backends]
    frontends = workspace.frontends

    # Everything server-side lives under /api, with or without a gateway.
    # Keeping the public shape identical is what lets the frontend ship one
    # production build (VITE_API_URL=/api) that keeps working the day a
    # gateway appears in front of the backends.
    if gateway is not None:
        lines += [
            "\t# One application proxy in front of every backend. It owns auth",
            "\t# and request-id minting; Caddy owns TLS, HTTP/3 and compression.",
            "\thandle /api/* {",
            "\t\turi strip_prefix /api",
            f"\t\treverse_proxy {gateway.name}:{gateway.port}",
            "\t}",
            "",
        ]
    elif len(backends) == 1:
        service = backends[0]
        lines += [
            "\t# One backend, so no gateway: a second proxy would buy a hop and",
            "\t# an outage surface for nothing. The path stays /api either way.",
            "\thandle /api/* {",
            "\t\turi strip_prefix /api",
            f"\t\treverse_proxy {service.name}:{service.port}",
            "\t}",
            "",
        ]
    else:
        for service in backends:
            lines += [
                f"\thandle /api{service.prefix}/* {{",
                f"\t\turi strip_prefix /api{service.prefix}",
                f"\t\treverse_proxy {service.name}:{service.port}",
                "\t}",
                "",
            ]

    if frontends:
        primary = frontends[0]
        lines += [
            f"\t# {primary.name}: the built SPA, served as static files.",
            "\t# try_files sends unknown paths to index.html so client-side",
            "\t# routing survives a hard refresh.",
            "\thandle {",
            "\t\troot * /srv",
            "\t\ttry_files {path} /index.html",
            "\t\tfile_server",
            "\t}",
            "",
        ]
    elif not backends:
        lines += ['\trespond "no services in this workspace" 503', ""]

    lines += [
        "\tlog {",
        "\t\toutput stdout",
        "\t\tformat json",
        "\t}",
        "}",
    ]
    return "\n".join(lines) + "\n"
