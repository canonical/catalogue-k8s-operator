# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import json
import os
import socket
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlparse

from charms.catalogue_k8s.v1.catalogue import DEFAULT_RELATION_NAME
from ops.charm import ActionEvent
from ops.model import ActiveStatus
from ops.testing import Harness

from charm import CatalogueCharm

CONTAINER_NAME = "catalogue"


class TestCharm(unittest.TestCase):
    def setUp(self):
        os.environ["JUJU_VERSION"] = "3.0.3"
        self.harness = Harness(CatalogueCharm)
        self.harness.set_model_name("test-model")
        self.addCleanup(self.harness.cleanup)
        self.harness.set_leader(True)
        self.harness.begin_with_initial_hooks()

    def test_catalogue_pebble_ready(self):
        expected_plan = {
            "services": {
                "catalogue": {
                    "override": "replace",
                    "summary": "catalogue",
                    "command": "nginx -g 'daemon off;' -c /etc/nginx/nginx.conf",
                    "startup": "enabled",
                }
            },
        }

        initial_plan = self._plan.to_dict()
        self.assertEqual(expected_plan, initial_plan)

        service = self._container.get_service("catalogue")
        self.assertTrue(service.is_running())
        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

    def test_reconfigure_applications(self):
        # Given the catalogue and a remote charm
        # When a relation is established
        # Then the remote charm should expose an application entry
        # And the catalogue should write the entry to its config

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

        data = self._container.pull("/web/config.json")
        self.assertEqual(
            [
                {
                    "name": "remote-charm",
                    "url": "https://localhost",
                    "icon": "some-cool-icon",
                    "description": "",
                }
            ],
            json.loads(data.read())["apps"],
        )

    @patch.multiple(
        "charm.CatalogueCharm",
        _push_certs=lambda *_: None,
    )
    def test_server_cert(self):
        patcher = patch.object(CatalogueCharm, "_is_tls_ready")
        self.mock_tls_ready = patcher.start()
        self.addCleanup(patcher.stop)
        # Test with TLS
        self.harness.charm.server_cert = Mock(
            ca_cert="mock_ca", server_cert="mock_cert", private_key="mock_key"
        )

        self.mock_tls_ready.return_value = True
        self.harness.charm._on_server_cert_changed(None)
        internal_url = urlparse(self.harness.charm._internal_url)

        self.assertEqual(internal_url.scheme, "https")
        self.assertEqual(internal_url.port, 443)
        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

        # Test with HTTP
        self.mock_tls_ready.return_value = False
        self.harness.charm.server_cert = Mock()
        self.harness.charm._on_server_cert_changed(None)

        internal_url = urlparse(self.harness.charm._internal_url)
        self.assertEqual(internal_url.scheme, "http")
        self.assertEqual(internal_url.port, 80)

    @patch("charm.logger")
    @patch("charm.CatalogueCharm._configure")
    def test_ingress(self, mock_configure, mock_logger):
        class MockInfo:
            @property
            def items(self):
                return {
                    "name": "test_value",
                }

        # Test with ingress ready
        self.harness.charm._info = MockInfo()  # pyright: ignore
        self.harness.charm._on_ingress_ready(Mock(url="https://testingress.com"))

        mock_logger.info.assert_called_with(
            "This app's ingress URL: %s", "https://testingress.com"
        )
        mock_configure.assert_called_with({"name": "test_value"}, push_certs=True)

        mock_logger.reset_mock()
        mock_configure.reset_mock()

        # Test with ingress revoked
        self.harness.charm._info = None  # pyright: ignore
        self.harness.charm._on_ingress_revoked(None)

        mock_logger.info.assert_called_with("This app no longer has ingress")
        mock_configure.assert_called_with([], push_certs=True)

    def test_get_url_action_no_ingress(self):
        action_event = Mock(spec=ActionEvent)
        self.harness.charm._get_url(action_event)
        action_event.set_results.assert_called_once_with({"url": f"http://{socket.getfqdn()}:80"})

    @patch("ops.model.Model.get_binding", lambda *_, **__: None)
    def test_get_url_action_ingress(self):
        rel_id = self.harness.add_relation("ingress", "remote")
        self.harness.add_relation_unit(rel_id, "remote/0")
        self.harness.update_relation_data(
            rel_id, "remote", {"ingress": json.dumps({"url": "https://endpoint/subpath"})}
        )

        action_event = Mock(spec=ActionEvent)
        self.harness.charm._get_url(action_event)
        action_event.set_results.assert_called_once_with({"url": "https://endpoint/subpath"})

    @property
    def _container(self):
        return self.harness.model.unit.get_container(CONTAINER_NAME)

    @property
    def _plan(self):
        return self.harness.get_container_pebble_plan(CONTAINER_NAME)
