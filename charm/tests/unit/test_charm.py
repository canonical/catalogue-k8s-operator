# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for CatalogueCharm — Scenario-based (no Harness)."""

import dataclasses
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import PropertyMock, patch

from charms.catalogue_k8s.v1.catalogue import DEFAULT_RELATION_NAME
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.pebble import ConnectionError as PebbleConnectionError
from ops.testing import Container, Context, Relation, State
from scenario.state import Mount

from catalogue_state import WAITING_FOR_PEBBLE_MESSAGE, TlsMaterial
from charm import CatalogueCharm
from workload import ServiceNotRunningError

CONTAINER_NAME = "catalogue"

# The HTTP nginx config that intent_from produces when TLS is absent.
EXPECTED_NGINX_HTTP = (
    "worker_processes  1;\n"
    "        events {\n"
    "            worker_connections  1024;\n"
    "        }\n"
    "\n"
    "\n"
    "http {\n"
    "    include            mime.types;\n"
    "    default_type       application/octet-stream;\n"
    "    sendfile           on;\n"
    "    keepalive_timeout  65;\n"
    "    access_log         /dev/stdout;\n"
    "    error_log          /dev/stderr;\n"
    "\n"
    "    upstream self {\n"
    "      server localhost:80;\n"
    "    }\n"
    "\n"
    "    server {\n"
    "        listen               80;\n"
    "        server_name          localhost;\n"
    "        root                 /web;\n"
    "\n"
    "        error_page           500 502 503 504  /50x.html;\n"
    "        location = /50x.html {\n"
    "            root             /usr/share/nginx/html;\n"
    "        }\n"
    "    }\n"
    "}\n"
    "\n"
)


# --- Helpers ---


def _certificate_for_csr(csr_pem: str) -> tuple[str, str]:
    """Build a leaf certificate for the CSR, signed by a fresh CA.

    The tls library generates its own key and CSR during the run, so
    the test cannot pre-populate them — it reads the library's CSR
    from the output relation data and certifies that.

    Returns:
        (certificate_pem, ca_pem)
    """
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2027, 1, 1, tzinfo=UTC))
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_name)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2026, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2027, 1, 1, tzinfo=UTC))
        .sign(ca_key, hashes.SHA256())
    )
    return (
        leaf.public_bytes(serialization.Encoding.PEM).decode(),
        ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
    )


def _relate_certificate(
    ctx: Context[CatalogueCharm],
    base: State,
) -> tuple[State, str]:
    """Run the two-phase tls flow; return (state, cert_pem).

    Phase 1: relation_changed with an empty provider databag — the
    tls library generates its own key and CSR and publishes the
    request to the unit databag. Phase 2: the provider publishes a
    certificate for that CSR; the library matches it, stores it as a
    secret, and emits certificate_available, which routes through the
    reconciler and pushes the cert to the container.
    """
    cert_relation = Relation(endpoint="certificates", remote_app_name="vault")
    state_phase1 = dataclasses.replace(base, relations=[cert_relation])
    state_out = ctx.run(ctx.on.relation_changed(cert_relation), state_phase1)

    out_rel = next(r for r in state_out.relations if r.endpoint == "certificates")
    requests = json.loads(out_rel.local_unit_data["certificate_signing_requests"])
    csr_pem = requests[0]["certificate_signing_request"]
    cert_pem, ca_pem = _certificate_for_csr(csr_pem)
    provider_rel = Relation(
        endpoint="certificates",
        remote_app_name="vault",
        remote_app_data={
            "certificates": json.dumps(
                [
                    {
                        "ca": ca_pem,
                        "certificate_signing_request": csr_pem,
                        "certificate": cert_pem,
                        "chain": [],
                        "revoked": False,
                    }
                ]
            )
        },
        local_unit_data=dict(out_rel.local_unit_data),
    )
    state_phase2 = dataclasses.replace(state_out, relations=[provider_rel])
    state_out = ctx.run(ctx.on.relation_changed(provider_rel), state_phase2)
    return state_out, cert_pem


# --- Funnel test ---


def _reconcile_artifact(ctx: Context[CatalogueCharm], state_out: State) -> str:
    """Assert and return the config.json only a reconcile can push.

    The status collector reports Active for any connectable
    container even when no reconcile ran, so status assertions
    prove nothing about routing here; the pushed config does.
    """
    container_fs = state_out.get_container(CONTAINER_NAME).get_filesystem(ctx)
    cfg_file = container_fs / "web" / "config.json"
    assert cfg_file.exists(), "no reconcile artifact: config.json missing"
    return cfg_file.read_text()


def test_funnel_all_deferrable_events_to_reconcile(ctx: Context[CatalogueCharm]) -> None:
    """Every deferrable event routes to the reconciler.

    Each event fires against a fresh (empty) container, so the pushed
    config.json is proof the reconcile ran: the collector cannot fake
    it. Mocking `_reconcile` is not an option — ops checks
    isinstance(observer, types.MethodType) at observe time.
    """
    container = Container(name="catalogue", can_connect=True)
    base = State(leader=True, containers=[container])

    # -- config_changed --
    state_out = ctx.run(ctx.on.config_changed(), base)
    assert '"title"' in _reconcile_artifact(ctx, state_out)

    # -- pebble_ready --
    state_out = ctx.run(ctx.on.pebble_ready(container), base)
    _reconcile_artifact(ctx, state_out)

    # -- upgrade_charm --
    state_out = ctx.run(ctx.on.upgrade_charm(), base)
    _reconcile_artifact(ctx, state_out)

    # -- update_status --
    state_out = ctx.run(ctx.on.update_status(), base)
    _reconcile_artifact(ctx, state_out)

    # -- leader_elected --
    state_out = ctx.run(ctx.on.leader_elected(), base)
    _reconcile_artifact(ctx, state_out)

    # -- items_changed (catalogue relation): item reaches config --
    cat_relation = Relation(
        endpoint=DEFAULT_RELATION_NAME,
        remote_app_name="rc",
        remote_app_data={
            "name": "remote-charm",
            "url": "https://localhost",
            "icon": "some-cool-icon",
        },
    )
    state_cat = dataclasses.replace(base, relations=[cat_relation])
    state_out = ctx.run(ctx.on.relation_changed(cat_relation), state_cat)
    assert "remote-charm" in _reconcile_artifact(ctx, state_out)

    # -- ingress ready --
    ingress_relation = Relation(
        endpoint="ingress",
        remote_app_name="traefik",
        remote_app_data={"ingress": json.dumps({"url": "https://testingress.com/sub"})},
    )
    state_ingress = dataclasses.replace(base, relations=[ingress_relation])
    state_out = ctx.run(ctx.on.relation_changed(ingress_relation), state_ingress)
    _reconcile_artifact(ctx, state_out)

    # -- ingress revoked (relation from output state: same identity) --
    out_ingress_rel = next(r for r in state_out.relations if r.endpoint == "ingress")
    state_out = ctx.run(ctx.on.relation_broken(out_ingress_rel), state_out)
    _reconcile_artifact(ctx, state_out)

    # -- certificate_available: the cert lands on disk --
    state_out, cert_pem = _relate_certificate(ctx, base)
    container_fs = state_out.get_container(CONTAINER_NAME).get_filesystem(ctx)
    cert_file = container_fs / "etc" / "catalogue" / "certs" / "catalogue.cert.pem"
    assert cert_file.exists(), "no reconcile artifact: cert missing"
    # The library normalises the PEM's trailing newline when storing.
    assert cert_file.read_text().strip() == cert_pem.strip()


def test_certificate_relation_removed_cleans_certs(
    ctx: Context[CatalogueCharm],
    tmp_path: Path,
) -> None:
    """Certs on disk with the TLS relation gone yields RemoveCerts.

    Scenario does not persist the container filesystem between runs,
    so the certs directory is a host-backed mount: the files the
    reconciler pushes in one run are visible to the next, and their
    removal is observable on the host.
    """
    # GIVEN a unit whose certificate was pushed by the reconciler
    mount = Mount(location="/etc/catalogue/certs", source=tmp_path)
    container = Container(name="catalogue", can_connect=True, mounts={"certs": mount})
    base = State(leader=True, containers=[container])
    state_out, _ = _relate_certificate(ctx, base)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "ca.cert",
        "catalogue.cert.pem",
        "catalogue.key.pem",
    ]

    # WHEN the certificates relation is gone and a reconcile fires
    state_no_tls = dataclasses.replace(state_out, relations=[])
    state_out = ctx.run(ctx.on.config_changed(), state_no_tls)

    # THEN the stale certs are removed from the container
    assert list(tmp_path.iterdir()) == []


# --- Action tests ---


def test_get_url_action_no_ingress(ctx: Context[CatalogueCharm], base_state: State) -> None:
    """get-url action returns the internal URL when no ingress."""
    # GIVEN a running unit with no ingress relation
    # WHEN the action runs
    ctx.run(ctx.on.action("get-url"), base_state)

    # THEN the result is the internal fqdn URL
    assert ctx.action_results is not None
    assert ctx.action_results["url"] == f"http://{socket.getfqdn()}:80"


def test_get_url_action_ingress(ctx: Context[CatalogueCharm], base_state: State) -> None:
    """get-url action returns the ingress URL when available."""
    # GIVEN a running unit with an ingress relation providing a URL
    ingress_relation = Relation(
        endpoint="ingress",
        remote_app_name="traefik",
        remote_app_data={"ingress": json.dumps({"url": "https://endpoint/subpath"})},
    )
    state = dataclasses.replace(base_state, relations=[ingress_relation])

    # WHEN the action runs
    ctx.run(ctx.on.action("get-url"), state)

    # THEN the result is the ingress URL
    assert ctx.action_results is not None
    assert ctx.action_results["url"] == "https://endpoint/subpath"


# --- Config-push content tests ---


def test_reconfigure_applications(ctx: Context[CatalogueCharm], base_state: State) -> None:
    """Adding a catalogue relation pushes the app to config.json."""
    # GIVEN a catalogue relation with a remote charm
    cat_relation = Relation(
        endpoint=DEFAULT_RELATION_NAME,
        remote_app_name="rc",
        remote_app_data={
            "name": "remote-charm",
            "url": "https://localhost",
            "icon": "some-cool-icon",
        },
    )
    state = dataclasses.replace(base_state, relations=[cat_relation])

    # WHEN relation-changed fires
    state_out = ctx.run(ctx.on.relation_changed(cat_relation), state)

    # THEN the catalogue config was pushed with the app entry
    container_fs = state_out.get_container(CONTAINER_NAME).get_filesystem(ctx)
    cfg_file = container_fs / "web" / "config.json"
    body = json.loads(cfg_file.read_text())
    apps = body.get("apps", [])
    assert len(apps) == 1
    assert apps[0]["name"] == "remote-charm"
    assert apps[0]["url"] == "https://localhost"
    assert apps[0]["icon"] == "some-cool-icon"


def test_api_type(ctx: Context[CatalogueCharm], base_state: State) -> None:
    """Catalogue config includes api_docs and api_endpoints."""
    # GIVEN a remote charm with api_docs and api_endpoints
    apidocs = "some_url_to_upstream_docs"
    apiendpoint = {"endpoint_1": "some_endpoint_1", "endpoint_2": "some_endpoint_2"}
    cat_relation = Relation(
        endpoint=DEFAULT_RELATION_NAME,
        remote_app_name="rc",
        remote_app_data={
            "name": "remote-charm",
            "url": "https://localhost",
            "icon": "some-cool-icon",
            "api_docs": apidocs,
            "api_endpoints": json.dumps(apiendpoint),
        },
    )
    state = dataclasses.replace(base_state, relations=[cat_relation])

    # WHEN relation-changed fires
    state_out = ctx.run(ctx.on.relation_changed(cat_relation), state)

    # THEN the pushed config includes the api fields
    container_fs = state_out.get_container(CONTAINER_NAME).get_filesystem(ctx)
    cfg_file = container_fs / "web" / "config.json"
    body = json.loads(cfg_file.read_text())
    apps = body.get("apps", [])
    assert len(apps) == 1
    assert apps[0]["api_docs"] == apidocs
    assert apps[0]["api_endpoints"] == apiendpoint


# --- TLS / server cert test ---


def test_server_cert(ctx: Context[CatalogueCharm], base_state: State) -> None:
    """_internal_url scheme reflects TLS availability."""
    # GIVEN TLS available
    with patch.object(CatalogueCharm, "_tls_config", new_callable=PropertyMock) as mock_tls:
        mock_tls.return_value = TlsMaterial(server_cert="cert", ca_cert="ca", private_key="key")

        # WHEN config_changed fires
        state_out = ctx.run(ctx.on.config_changed(), base_state)

        # THEN the unit is active and the nginx config is HTTPS
        assert state_out.unit_status == ActiveStatus()
        container_fs = state_out.get_container(CONTAINER_NAME).get_filesystem(ctx)
        nginx_conf = container_fs / "etc" / "nginx" / "nginx.conf"
        content = nginx_conf.read_text()
        assert "listen               443 ssl" in content
        assert "ssl_certificate" in content

        # WHEN TLS is not available
        mock_tls.return_value = None
        state_out2 = ctx.run(ctx.on.config_changed(), state_out)

        # THEN the nginx config is HTTP
        container_fs2 = state_out2.get_container(CONTAINER_NAME).get_filesystem(ctx)
        nginx_conf2 = container_fs2 / "etc" / "nginx" / "nginx.conf"
        content2 = nginx_conf2.read_text()
        assert "listen               80" in content2
        assert "ssl_certificate" not in content2


# --- Existing Scenario tests (kept, adapted to fixtures) ---


def test_workload_failure_yields_blocked(ctx: Context[CatalogueCharm]) -> None:
    """A workload failure during reconcile yields BlockedStatus."""
    # GIVEN a container with a stale config so compute yields
    # RestartNginx, and a workload whose restart raises
    # ServiceNotRunningError.
    container = Container(name="catalogue", can_connect=True)
    state = State(
        leader=True,
        containers=[container],
        config={"title": "Test"},
    )

    # Push initial config so the running state exists.
    state_out = ctx.run(ctx.on.config_changed(), state)
    assert state_out.unit_status == ActiveStatus()

    # Now patch Workload.restart to fail
    with patch("charm.Workload.restart") as mock_restart:
        mock_restart.side_effect = ServiceNotRunningError(service="catalogue", current="inactive")

        # WHEN config_changed fires with a change that triggers restart
        state_out = ctx.run(
            ctx.on.config_changed(),
            dataclasses.replace(state_out, config={"title": "New Title"}),
        )

        # THEN the unit reports Blocked with the remedy text
        assert isinstance(state_out.unit_status, BlockedStatus)
        assert "pebble services" in state_out.unit_status.message


def test_container_not_connectable_yields_waiting(ctx: Context[CatalogueCharm]) -> None:
    """When the container is not connectable, status is Waiting."""
    # GIVEN a container whose Pebble API is not reachable
    container = Container(name="catalogue", can_connect=False)
    state = State(
        leader=True,
        containers=[container],
    )

    # WHEN config_changed fires
    state_out = ctx.run(ctx.on.config_changed(), state)

    # THEN the unit reports Waiting for Pebble ready
    assert state_out.unit_status == WaitingStatus(WAITING_FOR_PEBBLE_MESSAGE)


def test_collector_pebble_drop_yields_waiting(ctx: Context[CatalogueCharm]) -> None:
    """A Pebble drop between reconcile and collect is Waiting.

    can_connect() is point-in-time: the socket can drop between the
    check and the read. The collector runs at the end of every hook
    and must never crash the hook into error state.
    """
    # GIVEN a connectable container whose second read drops the socket
    container = Container(name="catalogue", can_connect=True)
    state = State(
        leader=True,
        containers=[container],
        config={"title": "Test"},
    )

    with patch("charm.Workload.nginx_config") as mock_nginx:
        # First fetch (inside _reconcile) answers with the expected
        # config; every later fetch drops the socket. On a leader both
        # collect events fire (unit then app), so the handler runs
        # twice after the reconcile — three calls in total.
        mock_nginx.side_effect = [
            EXPECTED_NGINX_HTTP,
            PebbleConnectionError("pebble socket dropped"),
            PebbleConnectionError("pebble socket dropped"),
        ]

        # WHEN config_changed fires
        state_out = ctx.run(ctx.on.config_changed(), state)

    # THEN the unit reports Waiting, not an error state
    assert state_out.unit_status == WaitingStatus(WAITING_FOR_PEBBLE_MESSAGE)


def test_ingress(ctx: Context[CatalogueCharm], base_state: State) -> None:
    """Ingress events route through the reconciler."""
    # GIVEN a catalogue relation with items
    cat_relation = Relation(
        endpoint=DEFAULT_RELATION_NAME,
        remote_app_name="rc",
        remote_app_data={
            "name": "remote-charm",
            "url": "https://localhost",
            "icon": "some-cool-icon",
        },
    )
    ingress_relation = Relation(
        endpoint="ingress",
        remote_app_name="traefik",
        remote_app_data={"ingress": json.dumps({"url": "https://testingress.com/sub"})},
    )
    state = dataclasses.replace(base_state, relations=[cat_relation, ingress_relation])

    # WHEN ingress relation-changed fires (triggers ready)
    state_out = ctx.run(ctx.on.relation_changed(ingress_relation), state)

    # THEN the catalogue config was pushed with the app entry
    container_fs = state_out.get_container(CONTAINER_NAME).get_filesystem(ctx)
    cfg_file = container_fs / "web" / "config.json"
    body = json.loads(cfg_file.read_text())
    apps = body.get("apps", [])
    assert len(apps) == 1
    assert apps[0]["name"] == "remote-charm"

    # WHEN ingress relation-broken fires (triggers revoked)
    # Must use the relation from the output state (same identity).
    out_ingress_rel = next(r for r in state_out.relations if r.endpoint == "ingress")
    state_out2 = ctx.run(ctx.on.relation_broken(out_ingress_rel), state_out)

    # THEN the reconciler still runs (items still pushed)
    container_fs2 = state_out2.get_container(CONTAINER_NAME).get_filesystem(ctx)
    cfg_file2 = container_fs2 / "web" / "config.json"
    body2 = json.loads(cfg_file2.read_text())
    apps2 = body2.get("apps", [])
    assert len(apps2) == 1
