"""Session state that is per process, in a deployment that is not.

The generated Dockerfile ends in `uvicorn --workers $JFAST_WORKERS`, and
`JFAST_WORKERS` defaults to one per CPU. So more than one process is the shape
production has, and anything the framework keeps in memory is kept once per
worker.

For `auth` that is not a degraded mode, it is a broken one, and the symptom
arrives weeks later as "users get logged out at random":

  * a logout revokes on the worker that served it and nowhere else, so the
    token keeps working everywhere else;
  * a refresh reaching any worker but the issuing one finds no family for it
    and is answered 401 "this session has been revoked" -- a revocation that
    never happened, on three requests in four with four cores.

Both are reproduced below against two applications, which is what two workers
are: one process each, one store each.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jfastframework import create_app
from jfastframework.errors import PluginError

pytest.importorskip("jwt")

CONFIG = """\
[app]
name = "probe"
version = "0.1.0"
env = "{env}"

[plugins]
enabled = ["observability", "auth"]
disabled = []

[plugin.auth]
mode = "secret"
issue_tokens = {issue}
issuer = "https://probe.example"
audience = "probe"
algorithms = ["HS256"]
"""


@pytest.fixture(autouse=True)
def signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JFAST_AUTH_SECRET", "s" * 40)


def config(tmp_path: Path, *, env: str, issue: bool = True) -> Path:
    path = tmp_path / f"{env}-{issue}.toml"
    path.write_text(CONFIG.format(env=env, issue=str(issue).lower()), encoding="utf-8")
    return path


def test_two_workers_do_not_share_a_logout(tmp_path: Path) -> None:
    """The failure the guard exists to prevent, shown before it is prevented.

    Outside production the in-memory store is still what a developer gets, so
    this stays reproducible -- and reproducible is the point: it is the
    evidence that the refusal below is not a precaution.
    """
    path = config(tmp_path, env="local")
    worker_a, worker_b = create_app(config_path=str(path)), create_app(config_path=str(path))

    with TestClient(worker_a) as a, TestClient(worker_b) as b:
        issuer = worker_a.state.jfast.require("auth")._issuer
        pair = asyncio.run(issuer.issue_pair(subject="u1", scopes=["invoices:write"]))
        header = {"Authorization": f"Bearer {pair.access_token}"}

        assert a.get("/auth/me", headers=header).status_code == 200
        assert b.get("/auth/me", headers=header).status_code == 200
        assert a.post("/auth/logout", headers=header).status_code == 204

        assert a.get("/auth/me", headers=header).status_code == 401
        # The whole finding in one line: revoked here, still valid there.
        assert b.get("/auth/me", headers=header).status_code == 200


def test_a_refresh_on_another_worker_is_refused_as_revoked(tmp_path: Path) -> None:
    """And the half that breaks logins rather than security.

    Fail-closed is the right direction for an unknown family, which makes this
    worse to diagnose, not better: the 401 names a revocation, so the person
    reading it goes looking for the logout that never happened.
    """
    path = config(tmp_path, env="local")
    worker_a, worker_b = create_app(config_path=str(path)), create_app(config_path=str(path))

    with TestClient(worker_a) as a, TestClient(worker_b) as b:
        issuer = worker_a.state.jfast.require("auth")._issuer
        pair = asyncio.run(issuer.issue_pair(subject="u1", scopes=[]))

        assert (
            a.post("/auth/refresh", json={"refresh_token": pair.refresh_token}).status_code == 200
        )

        second = asyncio.run(issuer.issue_pair(subject="u2", scopes=[]))
        refused = b.post("/auth/refresh", json={"refresh_token": second.refresh_token})
        assert refused.status_code == 401
        assert "revoked" in refused.text


def test_production_refuses_to_start_rather_than_serve_that(tmp_path: Path) -> None:
    """A warning in a JSON log at boot is not read. This is."""
    with pytest.raises(PluginError) as raised:
        create_app(config_path=str(config(tmp_path, env="prod")))

    message = str(raised.value)
    assert "shared token store" in message
    # The remedy, not just the diagnosis.
    assert "cache" in message
    assert "issue_tokens = false" in message


def test_a_service_that_only_verifies_tokens_still_starts(tmp_path: Path) -> None:
    """It mints nothing, so it has no session of its own to lose.

    Refusing here would fail the ordinary case -- a service behind somebody
    else's identity provider -- for a risk it does not carry.
    """
    app = create_app(config_path=str(config(tmp_path, env="prod", issue=False)))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_development_keeps_working_without_redis(tmp_path: Path) -> None:
    """The guard is about production. Requiring Redis to run tests locally is
    how a rule gets switched off instead of followed."""
    app = create_app(config_path=str(config(tmp_path, env="local")))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
