"""Languages a JFast service can be written in.

Adding Go is not "a second generator". It only works because every JFast
service, whatever it is written in, satisfies the same contract:

* ``GET /health``   liveness, cheap, no dependency probing
* ``GET /ready``    readiness, 503 when a critical dependency is down
* ``X-Request-ID``  read from the request or minted, echoed, propagated
* errors as ``application/problem+json``
* configuration from ``JFAST_*`` environment variables
* one ten-port block, allocated by the workspace

That contract is what lets the gateway route to a Go service without knowing it
is Go, the workspace allocate its ports, and Caddy put it behind one hostname.
See docs/service-contract.md -- it is the thing to keep stable, not the
templates.

A language is deliberately *not* a plugin: plugins extend a running Python
app, and a Go service has no Python app to extend.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LanguageSpec:
    name: str
    label: str
    # Binary that must be on PATH to build or run a service in this language.
    toolchain: str
    version_args: tuple[str, ...]
    # Template tree for a service, and the file that identifies one on disk.
    template: str
    marker_file: str
    # Service kinds this language can produce.
    kinds: tuple[str, ...]
    # How the generated Dockerfile builds it, for the workspace compose file.
    dockerfile: str = "Dockerfile"
    notes: str = ""
    extra_templates: dict[str, str] = field(default_factory=dict)

    def installed(self) -> bool:
        return shutil.which(self.toolchain) is not None

    def version(self) -> str | None:
        """Installed toolchain version, or None when it is missing."""
        binary = shutil.which(self.toolchain)
        if binary is None:
            return None
        try:
            result = subprocess.run(
                [binary, *self.version_args],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return (
            (result.stdout or result.stderr).strip().splitlines()[0]
            if result.returncode == 0
            else None
        )


LANGUAGES: dict[str, LanguageSpec] = {
    "python": LanguageSpec(
        name="python",
        label="Python (FastAPI) — the full plugin system",
        toolchain="python3",
        version_args=("--version",),
        template="service_base",
        marker_file="jfast.toml",
        kinds=("api", "web", "gateway"),
        notes="Everything the framework offers: plugins, modules, migrations, HTMX.",
    ),
    "go": LanguageSpec(
        name="go",
        label="Go (stdlib net/http) — small, fast, one binary",
        toolchain="go",
        version_args=("version",),
        template="service_go",
        marker_file="go.mod",
        kinds=("api",),
        notes=(
            "Satisfies the service contract with zero third-party dependencies. "
            "No plugin system: a Go service wires its own datastores."
        ),
    ),
}

DEFAULT_LANGUAGE = "python"


def get(name: str) -> LanguageSpec:
    try:
        return LANGUAGES[name]
    except KeyError:
        raise ValueError(
            f"Unknown language {name!r}. Available: {', '.join(sorted(LANGUAGES))}"
        ) from None


def available() -> list[LanguageSpec]:
    """Languages whose toolchain is actually installed on this machine.

    The point of the language registry is that you only need the toolchain for
    the languages you use -- a Python-only team never installs Go.
    """
    return [spec for spec in LANGUAGES.values() if spec.installed()]


def detect(marker_files: set[str]) -> str | None:
    """Identify a service's language from the files in its directory."""
    for spec in LANGUAGES.values():
        if spec.marker_file in marker_files:
            return spec.name
    return None
