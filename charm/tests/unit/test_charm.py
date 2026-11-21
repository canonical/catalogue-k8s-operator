# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import json
import os
import socket
import unittest
from unittest.mock import MagicMock, Mock, PropertyMock, patch
from urllib.parse import urlparse

from charms.catalogue_k8s.v1.catalogue import DEFAULT_RELATION_NAME
from ops.charm import ActionEvent
from ops.model import ActiveStatus
from ops.testing import Harness

from charm import CatalogueCharm
from nginx_config import NGINX_CONFIG_PATH

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

        # THEN the unit is active
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
        patcher = patch.object(CatalogueCharm, "_tls_available", new_callable=PropertyMock)
        mock_tls_ready = patcher.start()
        self.addCleanup(patcher.stop)

        # WHEN TLS is available
        mock_tls_ready.return_value = True
        self.harness.charm._on_certificate_available(None)
        internal_url = urlparse(self.harness.charm._internal_url)

        # THEN the scheme is https
        self.assertEqual(internal_url.scheme, "https")
        self.assertEqual(internal_url.port, 443)
        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

        # WHEN TLS is not available
        mock_tls_ready.return_value = False
        self.harness.charm._on_certificate_available(None)

        # THEN the scheme is http
        internal_url = urlparse(self.harness.charm._internal_url)
        self.assertEqual(internal_url.scheme, "http")
        self.assertEqual(internal_url.port, 80)

    @patch("charm.logger")
    @patch("charm.CatalogueCharm._reconcile")
    def test_ingress(self, mock_reconcile: MagicMock, mock_logger: MagicMock) -> None:
        """Ingress events call the reconciler."""

        class MockInfo:
            @property
            def items(self) -> dict[str, str]:
                return {
                    "name": "test_value",
                }

        # GIVEN ingress ready
        self.harness.charm._info = MockInfo()  # pyright: ignore[reportAttributeAccessIssue]
        self.harness.charm._on_ingress_ready(Mock(url="https://testingress.com"))

        # THEN the reconciler is called with the items
        mock_logger.info.assert_called_with(
            "This app's ingress URL: %s", "https://testingress.com"
        )
        mock_reconcile.assert_called_with({"name": "test_value"})

        mock_logger.reset_mock()
        mock_reconcile.reset_mock()

        # GIVEN ingress revoked
        self.harness.charm._info = None  # pyright: ignore[reportAttributeAccessIssue]
        self.harness.charm._on_ingress_revoked(None)

        # THEN the reconciler is called with empty items
        mock_logger.info.assert_called_with("This app no longer has ingress")
        mock_reconcile.assert_called_with([])

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
