# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import dataclasses
import json
import os
import socket
import unittest
from unittest.mock import Mock, PropertyMock, patch
from urllib.parse import urlparse

from charms.catalogue_k8s.v1.catalogue import DEFAULT_RELATION_NAME
from charms.tls_certificates_interface.v4.tls_certificates import (
    Certificate,
    CertificateSigningRequest,
)
from ops.charm import ActionEvent
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.pebble import ConnectionError as PebbleConnectionError
from ops.testing import Container, Context, Harness, State

from catalogue_state import WAITING_FOR_PEBBLE_MESSAGE, TlsMaterial
from charm import CatalogueCharm
from nginx_config import NGINX_CONFIG_PATH
from workload import ServiceNotRunningError

CONTAINER_NAME = "catalogue"
ROOT_PATH = "/web"
CONFIG_PATH = ROOT_PATH + "/config.json"

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

EXPECTED_LAYER_SERVICES = {
    "catalogue": {
        "override": "replace",
        "summary": "catalogue",
        "command": f"nginx -g 'daemon off;' -c {NGINX_CONFIG_PATH}",
        "startup": "enabled",
    }
}


class TestCharm(unittest.TestCase):
    """Unit tests for CatalogueCharm with mocked Workload."""

    def setUp(self) -> None:
        os.environ["JUJU_VERSION"] = "3.0.3"

        # Patch Workload at module level so the charm gets a mock.
        self.workload_patcher = patch("charm.Workload", autospec=True)
        self.mock_workload_cls = self.workload_patcher.start()
        self.mock_workload = self.mock_workload_cls.return_value

        # Default: container ready, nothing on disk — so the initial
        # hooks will push everything (layer, configs) and converge.
        self.mock_workload.can_connect.return_value = True
        self.mock_workload.nginx_config.return_value = ""
        self.mock_workload.catalogue_config.return_value = {}
        self.mock_workload.layer_services.return_value = {}
        self.mock_workload.certs.return_value = False

        self.harness = Harness(CatalogueCharm)
        self.harness.set_model_name("test-model")
        self.addCleanup(self.harness.cleanup)
        self.addCleanup(self.workload_patcher.stop)
        self.harness.set_leader(True)
        self.harness.begin_with_initial_hooks()

    # --- Helpers ---

    def _push_call_for(self, path: str) -> Mock | None:
        """Find the most recent push call for a given path."""
        for call in reversed(self.mock_workload.push.call_args_list):
            if call.args[0] == path:
                return call
        return None

    # --- Tests ---

    def test_catalogue_pebble_ready(self) -> None:
        """pebble_ready pushes layer, configs, and restarts nginx."""
        # GIVEN a fresh container (mock returns empty state)
        # WHEN pebble_ready fires (via begin_with_initial_hooks)

        # THEN the layer was ensured
        self.mock_workload.ensure_layer.assert_called()
        layer_call = self.mock_workload.ensure_layer.call_args
        assert layer_call is not None
        assert layer_call.args[0] == "catalogue"
        assert layer_call.args[1]["catalogue"]["startup"] == "enabled"

        # THEN nginx was restarted
        self.mock_workload.restart.assert_called_with("catalogue")

        # THEN the unit is active (via collect_unit_status)
        self.harness.evaluate_status()
        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

    def test_reconfigure_applications(self) -> None:
        """Adding a catalogue relation pushes the app to config.json."""
        # GIVEN the catalogue and a remote charm
        # WHEN a relation is established
        rel_id = self.harness.add_relation(DEFAULT_RELATION_NAME, "rc")
        self.harness.add_relation_unit(rel_id, "rc/0")
        self.harness.update_relation_data(
            rel_id,
            "rc",
            {
                "name": "remote-charm",
                "url": "https://localhost",
                "icon": "some-cool-icon",
            },
        )

        # THEN the catalogue config was pushed with the app entry
        push_call = self._push_call_for(CONFIG_PATH)
        assert push_call is not None, "Expected push to CONFIG_PATH"
        body = json.loads(push_call.args[1])
        apps = body.get("apps", [])
        assert len(apps) == 1
        assert apps[0]["name"] == "remote-charm"
        assert apps[0]["url"] == "https://localhost"
        assert apps[0]["icon"] == "some-cool-icon"

    def test_api_type(self) -> None:
        """Catalogue config includes api_docs and api_endpoints."""
        # GIVEN a remote charm with api_docs and api_endpoints
        apidocs = "some_url_to_upstream_docs"
        apiendpoint = {"endpoint_1": "some_endpoint_1", "endpoint_2": "some_endpoint_2"}
        rel_id = self.harness.add_relation(DEFAULT_RELATION_NAME, "rc")
        self.harness.add_relation_unit(rel_id, "rc/0")
        self.harness.update_relation_data(
            rel_id,
            "rc",
            {
                "name": "remote-charm",
                "url": "https://localhost",
                "icon": "some-cool-icon",
                "api_docs": apidocs,
                "api_endpoints": json.dumps(apiendpoint),
            },
        )

        # THEN the pushed config includes the api fields
        push_call = self._push_call_for(CONFIG_PATH)
        assert push_call is not None, "Expected push to CONFIG_PATH"
        body = json.loads(push_call.args[1])
        apps = body.get("apps", [])
        assert len(apps) == 1
        assert apps[0]["api_docs"] == apidocs
        assert apps[0]["api_endpoints"] == apiendpoint

    def test_server_cert(self) -> None:
        """_internal_url scheme reflects TLS availability."""
        # GIVEN TLS available
        patcher = patch.object(CatalogueCharm, "_tls_config", new_callable=PropertyMock)
        mock_tls_config = patcher.start()
        self.addCleanup(patcher.stop)

        # WHEN TLS is available
        mock_tls_config.return_value = TlsMaterial(
            server_cert="cert", ca_cert="ca", private_key="key"
        )
        self.harness.charm.on.config_changed.emit()
        self.harness.evaluate_status()
        internal_url = urlparse(self.harness.charm._internal_url)

        # THEN the scheme is https
        self.assertEqual(internal_url.scheme, "https")
        self.assertEqual(internal_url.port, 443)
        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

        # WHEN TLS is not available
        mock_tls_config.return_value = None
        self.harness.charm.on.config_changed.emit()
        self.harness.evaluate_status()

        # THEN the scheme is http
        internal_url = urlparse(self.harness.charm._internal_url)
        self.assertEqual(internal_url.scheme, "http")
        self.assertEqual(internal_url.port, 80)

    def test_funnel_all_deferrable_events_to_reconcile(self) -> None:
        """Every deferrable event routes to _reconcile exactly once."""
        # GIVEN a charm with _reconcile mocked
        with patch.object(CatalogueCharm, "_reconcile") as mock_reconcile:
            container = self.harness.model.unit.get_container("catalogue")

            # Add an ingress relation so ingress events have a real
            # relation.
            ingress_rel_id = self.harness.add_relation("ingress", "traefik")
            self.harness.add_relation_unit(ingress_rel_id, "traefik/0")
            ingress_relation = self.harness.charm.model.get_relation("ingress", ingress_rel_id)
            assert ingress_relation is not None

            # WHEN each deferrable event fires
            self.harness.charm.on.catalogue_pebble_ready.emit(workload=container)
            assert mock_reconcile.call_count == 1
            mock_reconcile.reset_mock()

            self.harness.charm.on.config_changed.emit()
            assert mock_reconcile.call_count == 1
            mock_reconcile.reset_mock()

            self.harness.charm.on.upgrade_charm.emit()
            assert mock_reconcile.call_count == 1
            mock_reconcile.reset_mock()

            self.harness.charm.on.update_status.emit()
            assert mock_reconcile.call_count == 1
            mock_reconcile.reset_mock()

            self.harness.charm.on.leader_elected.emit()
            assert mock_reconcile.call_count == 1
            mock_reconcile.reset_mock()

            self.harness.charm._info.on.items_changed.emit(items=[])
            assert mock_reconcile.call_count == 1
            mock_reconcile.reset_mock()

            self.harness.charm._ingress.on.ready.emit(ingress_relation, "https://testingress.com")
            assert mock_reconcile.call_count == 1
            mock_reconcile.reset_mock()

            self.harness.charm._ingress.on.revoked.emit(ingress_relation)
            assert mock_reconcile.call_count == 1
            mock_reconcile.reset_mock()

            # certificate_available requires real Certificate objects
            # (the event snapshot/restore round-trips through PEM).
            test_cert_pem = (
                "-----BEGIN CERTIFICATE-----\n"
                "MIICqjCCAZKgAwIBAgIUDjC3aJdOM1vHwNAm7ERFxdgdaZ8wDQYJKoZIhvcNAQEL\n"
                "BQAwDzENMAsGA1UEAwwEdGVzdDAeFw0yNjExMjEwMjAyMTJaFw0yNjExMjIwMjAy\n"
                "MTJaMA8xDTALBgNVBAMMBHRlc3QwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEK\n"
                "AoIBAQC6PQnIInno2ACyeJySZ+26+xlnA//sxan8VVZ44FgbnSmiODJ33qAiUIGJ\n"
                "4laubprqk5OURpzQkfyTU+0cleSy7iuwzBbBxPfUnC6uwIcd8ob2Jt0QYq1WFVUf\n"
                "XQoRMRFckEgIbP9fhoZws3SXwx8rc2MvhehCUeF7X79UgUSNSl1GhoOyXuE9bO4l\n"
                "0fLtr82119axfqpIAQzmc0/8b1cOoaUVTUlj74YABcP+F89jlk//yu+7MBhCTprj\n"
                "eJKUm5oOF/8HyKhRvqdWkFkSdjXPmjRAcA8igQmCR/fQJZbdujkjhSf2TB8Cy5cG\n"
                "lRoG61V9v5cQIXcPZ/qvBwhWwypDAgMBAAEwDQYJKoZIhvcNAQELBQADggEBAF5a\n"
                "wO91j/ch42XGmGCOrjwCLIy1qhdCXJMPRNsC6edLEhuwe+pZL90VKAdlbQC6g/mK\n"
                "dP6824g1yvLfJ7iYWYN8fIrIWbQdg62Vdlby1jJARgS/2R5FDmtRgs7M1dL6Uxuo\n"
                "crNiASzYvt1FBJAU1hN5KQzJDVEVrJFx/PmnAfseob4FmcxIXZkJQjDTyIVfZawY\n"
                "LXu61w2z39JUH1bOQuNHR/xIxx8+D0Ba/Jjh5icBrsG05CDRz2RZiv0wijb10IgT\n"
                "yXNPOMyDsgExh0XtPKy4EMjcHueVM8SJvm68NrPRjCTpUmxt/3Jdr1fa+5rAMpkX\n"
                "TxTGJtp+dRK+8BK4C/U=\n"
                "-----END CERTIFICATE-----"
            )
            test_csr_pem = (
                "-----BEGIN CERTIFICATE REQUEST-----\n"
                "MIICVDCCATwCAQAwDzENMAsGA1UEAwwEdGVzdDCCASIwDQYJKoZIhvcNAQEBBQAD\n"
                "ggEPADCCAQoCggEBAJKiQF2A3/+NNMvZa2lo3+yAFheyP6epZXnEmlM2SVkLaz6W\n"
                "3aLWMm3XLWZ1M/rLt8hZZ/8toNmjSxCI/K33NuGwWYdDK0QO0cKY6xAzn07lSyCU\n"
                "anq1Tv+xqoqam0GFGuJ7of1zEcbDMBtsWLfZuGN/ALnGgJW4y4VcyXPnlaQ1KJsw\n"
                "MN3f9Ly78D0UCuln1oiIDHeFOl2t32zSBV4wYGKgVqo7VsYz7+zhCA7iJOEDrahz\n"
                "PYTf/E1+sDfYA+NaF9BDZ9aQ4KjALBy9/hW4kF3epnvwk6TlEYFrevypN0yMspd0\n"
                "pjpE2HEV3QfXUDZ+E2xjfsiSM5Plg8+iPPpbH8UCAwEAAaAAMA0GCSqGSIb3DQEB\n"
                "CwUAA4IBAQBN5jHsEkpslc/YJiPHGJrjtHtsX++7aHvTJmzeeUQcTuPKV8Rp4Vt5\n"
                "aesqj1GqQykI/HTAenGeks1Y+d4dBmdZ7wSSHkOF0UxSuRKd0mRCp6IaetFW8UK6\n"
                "46wtGQ2GQA7NUJLDsStifrhyyGpCMNKAU3VWlic3nVvTVnw9XO1J67P2LBo4nmUq\n"
                "ysormhzGH6f+dZ+U3/Xizjpzrsim9M82Ug/RxB/v21P0qqen17RNddiUelBu7Wk8\n"
                "0H0BzkbkCHvt2v0nFN5BW3S6+yO/N7Sso7raZQXC8LoMnXZ3WaOLEySIWFUzthw/\n"
                "guiV7jilnvgSBlDYFxfuGIT18iGje++E\n"
                "-----END CERTIFICATE REQUEST-----"
            )
            test_cert = Certificate.from_string(test_cert_pem)
            test_csr = CertificateSigningRequest.from_string(test_csr_pem)
            self.harness.charm._cert_requirer.on.certificate_available.emit(
                certificate=test_cert,
                certificate_signing_request=test_csr,
                ca=test_cert,
                chain=[],
            )
            assert mock_reconcile.call_count == 1

    def test_ingress(self) -> None:
        """Ingress events route through the reconciler."""
        # GIVEN a catalogue relation with items
        rel_id = self.harness.add_relation(DEFAULT_RELATION_NAME, "rc")
        self.harness.add_relation_unit(rel_id, "rc/0")
        self.harness.update_relation_data(
            rel_id,
            "rc",
            {
                "name": "remote-charm",
                "url": "https://localhost",
                "icon": "some-cool-icon",
            },
        )

        # Reset mock to clear initial-hooks calls
        self.mock_workload.push.reset_mock()

        # Add an ingress relation so the ingress event has a real
        # relation.
        ingress_rel_id = self.harness.add_relation("ingress", "traefik")
        self.harness.add_relation_unit(ingress_rel_id, "traefik/0")
        ingress_relation = self.harness.charm.model.get_relation("ingress", ingress_rel_id)
        assert ingress_relation is not None

        # WHEN ingress ready fires
        self.harness.charm._ingress.on.ready.emit(ingress_relation, "https://testingress.com")

        # THEN the catalogue config was pushed with the app entry
        push_call = self._push_call_for(CONFIG_PATH)
        assert push_call is not None, "Expected push to CONFIG_PATH"
        body = json.loads(push_call.args[1])
        apps = body.get("apps", [])
        assert len(apps) == 1
        assert apps[0]["name"] == "remote-charm"

        # Reset and test ingress revoked
        self.mock_workload.push.reset_mock()
        self.harness.charm._ingress.on.revoked.emit(ingress_relation)

        # THEN the reconciler still runs (items still pushed)
        push_call = self._push_call_for(CONFIG_PATH)
        assert push_call is not None, "Expected push to CONFIG_PATH after revoke"

    def test_get_url_action_no_ingress(self) -> None:
        """get-url action returns the internal URL when no ingress."""
        action_event = Mock(spec=ActionEvent)
        self.harness.charm._get_url(action_event)
        action_event.set_results.assert_called_once_with({"url": f"http://{socket.getfqdn()}:80"})

    @patch("ops.model.Model.get_binding", lambda *_: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    def test_get_url_action_ingress(self) -> None:
        """get-url action returns the ingress URL when available."""
        rel_id = self.harness.add_relation("ingress", "remote")
        self.harness.add_relation_unit(rel_id, "remote/0")
        self.harness.update_relation_data(
            rel_id,
            "remote",
            {"ingress": json.dumps({"url": "https://endpoint/subpath"})},
        )

        action_event = Mock(spec=ActionEvent)
        self.harness.charm._get_url(action_event)
        action_event.set_results.assert_called_once_with({"url": "https://endpoint/subpath"})


# --- Scenario-based tests ---


def test_workload_failure_yields_blocked() -> None:
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

    ctx = Context(CatalogueCharm)

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


def test_container_not_connectable_yields_waiting() -> None:
    """When the container is not connectable, status is Waiting."""
    # GIVEN a container whose Pebble API is not reachable
    container = Container(name="catalogue", can_connect=False)
    state = State(
        leader=True,
        containers=[container],
    )

    # WHEN config_changed fires
    context = Context(CatalogueCharm)
    state_out = context.run(context.on.config_changed(), state)

    # THEN the unit reports Waiting for Pebble ready
    assert state_out.unit_status == WaitingStatus(WAITING_FOR_PEBBLE_MESSAGE)


def test_collector_pebble_drop_yields_waiting() -> None:
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
    ctx = Context(CatalogueCharm)

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
