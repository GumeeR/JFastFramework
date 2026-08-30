"""Kubernetes manifests: the probes, the defaults, and what is left out."""

from __future__ import annotations

import pytest

from jfastframework.deploy.kubernetes import build, deployment, ingress
from jfastframework.resources import Resource
from jfastframework.workspace import ServiceEntry, Workspace


def ws(*services: ServiceEntry) -> Workspace:
    workspace = Workspace(name="cometax")
    for service in services:
        workspace.add(service)
    return workspace


def api(name: str, port: int, **kwargs: object) -> ServiceEntry:
    return ServiceEntry(name=name, kind="api", port=port, path=name, **kwargs)  # type: ignore[arg-type]


def parsed(files: dict[str, str], name: str) -> list[dict]:  # type: ignore[type-arg]
    yaml = pytest.importorskip("yaml")
    return [d for d in yaml.safe_load_all(files[name]) if d]


def kind_of(documents: list[dict], kind: str) -> dict:  # type: ignore[type-arg]
    return next(d for d in documents if d["kind"] == kind)


# -- probes -------------------------------------------------------------


def test_liveness_probes_health_and_readiness_probes_ready() -> None:
    container = deployment(api("billing", 8010), namespace="cometax")["spec"]["template"]["spec"][
        "containers"
    ][0]

    # Pointing liveness at /ready turns a database blip into a restart storm
    # that finishes off the database.
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"
    assert container["readinessProbe"]["httpGet"]["path"] == "/ready"


def test_a_startup_probe_allows_a_slow_first_boot() -> None:
    container = deployment(api("billing", 8010), namespace="cometax")["spec"]["template"]["spec"][
        "containers"
    ][0]
    startup = container["startupProbe"]
    # Migrations or a cold cache must not read as a crash.
    assert startup["periodSeconds"] * startup["failureThreshold"] >= 120


# -- hardening ----------------------------------------------------------


def test_pods_run_as_non_root_with_a_read_only_filesystem() -> None:
    spec = deployment(api("billing", 8010), namespace="cometax")["spec"]["template"]["spec"]
    container = spec["containers"][0]

    assert spec["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_a_writable_tmp_exists_because_the_root_filesystem_is_not() -> None:
    spec = deployment(api("billing", 8010), namespace="cometax")["spec"]["template"]["spec"]
    assert spec["volumes"][0]["name"] == "tmp"
    assert spec["containers"][0]["volumeMounts"][0]["mountPath"] == "/tmp"


def test_a_rollout_never_drops_below_the_replica_count() -> None:
    strategy = deployment(api("billing", 8010), namespace="cometax")["spec"]["strategy"]
    assert strategy["rollingUpdate"]["maxUnavailable"] == 0


# -- configuration ------------------------------------------------------


def test_datastore_dsns_come_from_a_secret_not_a_configmap() -> None:
    container = deployment(
        api("billing", 8010, datastores=["database", "cache"]), namespace="cometax"
    )["spec"]["template"]["spec"]["containers"][0]
    names = {entry["name"]: entry for entry in container["env"]}

    assert "secretKeyRef" in names["JFAST_DB_DSN"]["valueFrom"]
    assert "secretKeyRef" in names["JFAST_CACHE_URL"]["valueFrom"]


def test_a_second_database_reaches_the_pod_as_its_own_secret() -> None:
    """The legacy `datastores` list could name one PostgreSQL. Bindings name two."""
    workspace = ws(api("billing", 8010))
    workspace.add_resource(Resource(name="core-db", type="postgres", port=8900))
    workspace.add_resource(Resource(name="core-db-replica", type="postgres", port=8901))
    workspace.link("billing", "core-db")
    workspace.link("billing", "core-db-replica", env="JFAST_DB_REPLICA_DSN")

    files = build(workspace)
    container = kind_of(parsed(files, "base/billing.yaml"), "Deployment")["spec"]["template"][
        "spec"
    ]["containers"][0]
    names = {entry["name"]: entry for entry in container["env"]}

    assert names["JFAST_DB_DSN"]["valueFrom"]["secretKeyRef"]["key"] == "db-dsn"
    assert names["JFAST_DB_REPLICA_DSN"]["valueFrom"]["secretKeyRef"]["key"] == "db-replica-dsn"


def test_the_secret_template_covers_every_bound_resource() -> None:
    workspace = ws(api("billing", 8010))
    workspace.add_resource(Resource(name="core-db", type="postgres", port=8900))
    workspace.add_resource(Resource(name="core-db-replica", type="postgres", port=8901))
    workspace.link("billing", "core-db")
    workspace.link("billing", "core-db-replica", env="JFAST_DB_REPLICA_DSN")

    secret = kind_of(parsed(build(workspace), "base/billing-secrets.example.yaml"), "Secret")

    assert set(secret["stringData"]) == {"db-dsn", "db-replica-dsn"}


def test_grpc_gets_its_own_port() -> None:
    manifest = deployment(api("edge", 8010, grpc=True), namespace="cometax")
    ports = manifest["spec"]["template"]["spec"]["containers"][0]["ports"]
    assert {"name": "grpc", "containerPort": 8019} in ports


# -- the tree -----------------------------------------------------------


def test_the_tree_is_a_kustomize_base_with_overlays() -> None:
    files = build(ws(api("billing", 8010, datastores=["database"])))

    assert "base/kustomization.yaml" in files
    assert "overlays/dev/kustomization.yaml" in files
    assert "overlays/prod/kustomization.yaml" in files
    assert "base/billing.yaml" in files


def test_a_frontend_is_not_deployed() -> None:
    files = build(
        ws(
            api("billing", 8010),
            ServiceEntry(name="admin", kind="spa", port=8020, path="admin", frontend="vue"),
        )
    )
    # A built SPA is static files served by the ingress or a CDN, not a pod.
    assert "base/admin.yaml" not in files


def test_no_database_manifest_is_generated() -> None:
    files = build(ws(api("billing", 8010, datastores=["database"])))
    # Manifests only: the README mentions StatefulSets in the prose explaining
    # why there is not one.
    manifests = "\n".join(v for k, v in files.items() if k.endswith(".yaml"))

    # A StatefulSet for PostgreSQL from a scaffolder is how people lose data.
    assert "StatefulSet" not in manifests
    assert "postgres:" not in manifests
    assert "Not generated: databases" in files["README.md"]


def test_the_secret_template_holds_placeholders_only() -> None:
    files = build(ws(api("billing", 8010, datastores=["database"])))
    documents = parsed(files, "base/billing-secrets.example.yaml")
    secret = kind_of(documents, "Secret")

    assert set(secret["stringData"].values()) == {"REPLACE_ME"}


def test_the_ingress_serves_the_api_prefix() -> None:
    manifest = ingress(ws(api("billing", 8010)), namespace="cometax", host="app.example.com")
    path = manifest["spec"]["rules"][0]["http"]["paths"][0]

    # Same public shape as the Caddyfile, so the frontend build is identical
    # in both places.
    assert path["path"] == "/api"
    assert path["backend"]["service"]["name"] == "billing"


def test_the_ingress_prefers_the_gateway_when_there_is_one() -> None:
    workspace = ws(
        api("billing", 8010),
        api("catalog", 8020),
        ServiceEntry(name="gateway", kind="gateway", port=8030, path="gateway"),
    )
    manifest = ingress(workspace, namespace="cometax", host="app.example.com")
    backend = manifest["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]

    assert backend["name"] == "gateway"


def test_every_manifest_is_valid_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    files = build(
        ws(api("billing", 8010, datastores=["database", "cache"]), api("edge", 8020, grpc=True))
    )

    for name, contents in files.items():
        if not name.endswith(".yaml"):
            continue
        documents = [d for d in yaml.safe_load_all(contents) if d]
        assert documents, f"{name} parsed to nothing"
        for document in documents:
            assert "apiVersion" in document and "kind" in document, name


def test_a_disruption_budget_keeps_one_replica_through_a_drain() -> None:
    files = build(ws(api("billing", 8010)))
    budget = kind_of(parsed(files, "base/billing.yaml"), "PodDisruptionBudget")
    # Without it a node drain can take every replica at once, and the
    # zero-downtime rollout above buys nothing.
    assert budget["spec"]["minAvailable"] == 1


def test_the_readme_warns_about_the_latest_tag() -> None:
    files = build(ws(api("billing", 8010)))
    # `latest` makes a rollback meaningless: there is nothing to roll back to.
    assert "latest" in files["README.md"]
    assert "rollback" in files["README.md"]
