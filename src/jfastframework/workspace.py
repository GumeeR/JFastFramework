"""Workspaces: several services that know about each other.

A single service needs no workspace. The moment there are two, three questions
appear that a per-service config cannot answer:

* which ports are already taken;
* what the frontend should call;
* whether anything sits in front of them.

``jfast.workspace.toml`` answers all three. It is the only file that knows the
whole system, and it is what makes the gateway and the frontend `.env` generate
themselves instead of being hand-maintained.

    [workspace]
    name = "cometax"
    base_port = 8000

    [[workspace.services]]
    name = "billing"
    kind = "api"
    port = 8010
    path = "billing"
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

WORKSPACE_FILE = "jfast.workspace.toml"
PORT_BLOCK_SIZE = 10
DEFAULT_BASE_PORT = 8000

# Kinds that expose an HTTP API a gateway can route to.
BACKEND_KINDS = frozenset({"api", "web"})
FRONTEND_KINDS = frozenset({"spa"})
GATEWAY_KIND = "gateway"


@dataclass
class ServiceEntry:
    name: str
    kind: str
    port: int
    path: str
    # Only for kind == "spa": vue | react.
    frontend: str | None = None
    # python | go. A service's language changes how it is built and tested,
    # and nothing else -- the contract it satisfies is identical.
    language: str = "python"
    grpc: bool = False
    # Datastores the service needs. Python services derive this from their
    # plugin list; other languages declare it in jfast.service.toml.
    datastores: list[str] = field(default_factory=list)

    @property
    def grpc_port(self) -> int:
        """Offset +9, at the end of the block so it never meets a datastore."""
        return self.port + 9

    @property
    def is_backend(self) -> bool:
        return self.kind in BACKEND_KINDS

    @property
    def is_frontend(self) -> bool:
        return self.kind in FRONTEND_KINDS

    @property
    def is_gateway(self) -> bool:
        return self.kind == GATEWAY_KIND

    @property
    def prefix(self) -> str:
        """URL prefix the gateway routes to this service under."""
        return f"/{self.name.replace('_', '-')}"

    @property
    def internal_url(self) -> str:
        """How sibling containers reach it on the compose network."""
        return f"http://{self.name}:{self.port}"

    @property
    def local_url(self) -> str:
        """How a developer reaches it from the host."""
        return f"http://localhost:{self.port}"


@dataclass
class Workspace:
    name: str
    base_port: int = DEFAULT_BASE_PORT
    services: list[ServiceEntry] = field(default_factory=list)
    file: Path | None = None

    # -- discovery -----------------------------------------------------

    @staticmethod
    def find(start: Path | None = None) -> Path | None:
        """Nearest ``jfast.workspace.toml`` at or above ``start``."""
        current = (start or Path.cwd()).resolve()
        for candidate in (current, *current.parents):
            path = candidate / WORKSPACE_FILE
            if path.is_file():
                return path
        return None

    @classmethod
    def load(cls, path: Path) -> Workspace:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        section: dict[str, Any] = raw.get("workspace", {})
        services = [
            ServiceEntry(
                name=entry["name"],
                kind=entry.get("kind", "api"),
                port=int(entry["port"]),
                path=entry.get("path", entry["name"]),
                frontend=entry.get("frontend"),
                language=entry.get("language", "python"),
                grpc=bool(entry.get("grpc", False)),
                datastores=list(entry.get("datastores", [])),
            )
            for entry in section.get("services", [])
        ]
        return cls(
            name=section.get("name", path.parent.name),
            base_port=int(section.get("base_port", DEFAULT_BASE_PORT)),
            services=services,
            file=path,
        )

    @classmethod
    def load_or_none(cls, start: Path | None = None) -> Workspace | None:
        path = cls.find(start)
        return cls.load(path) if path else None

    # -- mutation ------------------------------------------------------

    def get(self, name: str) -> ServiceEntry | None:
        return next((s for s in self.services if s.name == name), None)

    def next_port(self) -> int:
        """First free port block.

        Blocks are ten wide because each service's plugins claim offsets
        inside them (PostgreSQL at +1, Redis at +3, Qdrant at +7...). Handing
        out consecutive ports would make two services fight over the same
        database container port.
        """
        if not self.services:
            return self.base_port + PORT_BLOCK_SIZE
        return max(s.port for s in self.services) + PORT_BLOCK_SIZE

    def add(self, entry: ServiceEntry, *, replace: bool = False) -> ServiceEntry:
        existing = self.get(entry.name)
        if existing is not None and not replace:
            raise ValueError(
                f"Service {entry.name!r} is already in the workspace on port {existing.port}."
            )
        if existing is not None:
            self.services.remove(existing)

        clash = next((s for s in self.services if s.port == entry.port), None)
        if clash is not None:
            raise ValueError(
                f"Port {entry.port} is already taken by {clash.name!r}. "
                f"Next free block starts at {self.next_port()}."
            )
        self.services.append(entry)
        self.services.sort(key=lambda s: s.port)
        return entry

    # -- queries -------------------------------------------------------

    @property
    def backends(self) -> list[ServiceEntry]:
        return [s for s in self.services if s.is_backend]

    @property
    def frontends(self) -> list[ServiceEntry]:
        return [s for s in self.services if s.is_frontend]

    @property
    def gateway(self) -> ServiceEntry | None:
        return next((s for s in self.services if s.is_gateway), None)

    def needs_gateway(self) -> bool:
        """True once more than one backend exists and nothing fronts them.

        One service does not need a gateway -- adding one buys a hop and an
        outage surface for nothing. Two is where clients start needing to know
        too many hostnames.
        """
        return len(self.backends) > 1 and self.gateway is None

    def api_base_url(self, *, internal: bool = False) -> str:
        """What a frontend should call.

        The gateway when there is one, the single backend when there is not,
        and the workspace's own base port as a last resort.
        """
        target = self.gateway or (self.backends[0] if self.backends else None)
        if target is None:
            return f"http://localhost:{self.base_port}"
        return target.internal_url if internal else target.local_url

    # -- persistence ---------------------------------------------------

    def render(self) -> str:
        lines = [
            "# JFast workspace. Generated and updated by the `jfast` CLI.",
            "# Ports are allocated in blocks of ten; see docs/deploy.md.",
            "",
            "[workspace]",
            f'name = "{self.name}"',
            f"base_port = {self.base_port}",
        ]
        for service in sorted(self.services, key=lambda s: s.port):
            lines += [
                "",
                "[[workspace.services]]",
                f'name = "{service.name}"',
                f'kind = "{service.kind}"',
                f"port = {service.port}",
                f'path = "{service.path}"',
            ]
            if service.language != "python":
                lines.append(f'language = "{service.language}"')
            if service.frontend:
                lines.append(f'frontend = "{service.frontend}"')
            if service.grpc:
                lines.append("grpc = true")
            if service.datastores:
                stores = ", ".join(f'"{name}"' for name in service.datastores)
                lines.append(f"datastores = [{stores}]")
        return "\n".join(lines) + "\n"

    def save(self, path: Path | None = None) -> Path:
        destination = path or self.file
        if destination is None:
            raise ValueError("Workspace has no file to save to")
        destination.write_text(self.render(), encoding="utf-8")
        self.file = destination
        return destination

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_port": self.base_port,
            "services": [asdict(s) for s in self.services],
            "api_base_url": self.api_base_url(),
            "needs_gateway": self.needs_gateway(),
        }
