"""Load secrets from a cloud secret manager into the environment.

Environment variables are the right interface — every plugin's settings already
read them, and so does every language in the workspace. What is wrong is how
they usually get there: a `.env` file copied onto a server, or a value pasted
into a CI variable that nobody can rotate.

This closes that gap without changing the interface. Call it before
``create_app()`` and the rest of the framework is unchanged:

    from jfastframework.secrets import load_secrets

    load_secrets()          # reads JFAST_SECRETS_* and populates os.environ
    app = create_app()

Two rules it enforces:

* **A value already in the environment wins.** A developer exporting
  ``JFAST_DB_DSN`` locally must not be silently overridden by a production
  secret, and a container's own configuration must beat a stale stored copy.
* **Nothing is logged.** Only the *names* loaded, never the values.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("jfast.secrets")

PROVIDERS = ("aws", "gcp", "env")


class SecretsError(RuntimeError):
    """The secret could not be fetched or parsed."""


@dataclass
class SecretsConfig:
    provider: str = "env"
    # AWS: the secret's name or ARN. GCP: the secret id.
    name: str = ""
    region: str = ""
    project: str = ""
    version: str = "latest"
    # Only load keys with this prefix, so one shared secret can hold several
    # services' configuration without them reading each other's.
    prefix: str = ""

    @classmethod
    def from_env(cls) -> SecretsConfig:
        return cls(
            provider=os.getenv("JFAST_SECRETS_PROVIDER", "env"),
            name=os.getenv("JFAST_SECRETS_NAME", ""),
            region=os.getenv("JFAST_SECRETS_REGION", ""),
            project=os.getenv("JFAST_SECRETS_PROJECT", ""),
            version=os.getenv("JFAST_SECRETS_VERSION", "latest"),
            prefix=os.getenv("JFAST_SECRETS_PREFIX", ""),
        )


def _fetch_aws(config: SecretsConfig) -> str:
    """AWS Secrets Manager.

    No credentials are passed: boto3's default chain finds the instance or
    IRSA role. That is the point — a task role means no long-lived key exists
    to leak in the first place.
    """
    import boto3

    client = boto3.client("secretsmanager", region_name=config.region or None)
    try:
        response = client.get_secret_value(SecretId=config.name)
    except Exception as exc:
        raise SecretsError(f"cannot read AWS secret {config.name!r}: {exc}") from exc
    payload: str = response.get("SecretString") or ""
    if not payload:
        raise SecretsError(f"AWS secret {config.name!r} is binary; expected JSON text")
    return payload


def _fetch_gcp(config: SecretsConfig) -> str:
    """Google Secret Manager."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{config.project}/secrets/{config.name}/versions/{config.version}"
    try:
        response = client.access_secret_version(request={"name": path})
    except Exception as exc:
        raise SecretsError(f"cannot read GCP secret {config.name!r}: {exc}") from exc
    return str(response.payload.data.decode("utf-8"))


def parse(payload: str) -> dict[str, str]:
    """A secret's body as a flat mapping.

    JSON object, or `KEY=value` lines. Nested JSON is rejected rather than
    flattened: `{"db": {"host": ...}}` has no obvious environment-variable
    spelling, and guessing one produces a name nobody can predict.
    """
    payload = payload.strip()
    if payload.startswith("["):
        # Caught explicitly: falling through to the KEY=value parser would
        # return an empty mapping and load nothing, silently.
        raise SecretsError("secret JSON must be an object")
    if payload.startswith("{"):
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise SecretsError(f"secret is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SecretsError("secret JSON must be an object")
        flat: dict[str, str] = {}
        for key, value in data.items():
            if isinstance(value, dict | list):
                raise SecretsError(
                    f"secret key {key!r} is nested; environment variables are flat. "
                    f"Store it as a JSON string if the value really is structured."
                )
            flat[str(key)] = "" if value is None else str(value)
        return flat

    values: dict[str, str] = {}
    for line in payload.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_secrets(config: SecretsConfig | None = None, *, override: bool = False) -> list[str]:
    """Populate ``os.environ`` from the configured secret store.

    Returns the names loaded, so a caller can log *what* arrived without
    logging what it was.
    """
    resolved = config or SecretsConfig.from_env()

    if resolved.provider == "env" or not resolved.name:
        return []
    if resolved.provider not in PROVIDERS:
        raise SecretsError(
            f"unknown secrets provider {resolved.provider!r}; choose from {', '.join(PROVIDERS)}"
        )
    if resolved.provider == "gcp" and not resolved.project:
        raise SecretsError("the gcp provider needs JFAST_SECRETS_PROJECT")

    payload = _fetch_aws(resolved) if resolved.provider == "aws" else _fetch_gcp(resolved)
    values = parse(payload)

    loaded: list[str] = []
    for key, value in values.items():
        if resolved.prefix and not key.startswith(resolved.prefix):
            continue
        name = key[len(resolved.prefix) :] if resolved.prefix else key
        # An existing value wins unless explicitly overridden: the container's
        # own configuration beats a stale stored copy, and a developer's export
        # beats a production secret leaking into a local run.
        if name in os.environ and not override:
            continue
        os.environ[name] = value
        loaded.append(name)

    logger.info(
        "loaded %d secret(s) from %s", len(loaded), resolved.provider, extra={"keys": loaded}
    )
    return loaded
