#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charmed operator for creating service catalogues on Kubernetes."""

import json
import logging
import os
import socket
from typing import cast
from urllib.parse import urlparse

from charms.catalogue_k8s.v0.catalogue import (
    CatalogueItemsChangedEvent,
    CatalogueProvider,
)
from charms.observability_libs.v0.cert_handler import CertHandler
from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from charms.traefik_k8s.v1.ingress import (
    IngressPerAppReadyEvent,
    IngressPerAppRequirer,
    IngressPerAppRevokedEvent,
)
from lightkube.models.core_v1 import ServicePort
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus

logger = logging.getLogger(__name__)

ROOT_PATH = "/web"
CONFIG_PATH = ROOT_PATH + "/config.json"

CATALOGUE_CERTS_DIR = "/etc/catalogue/certs"
CERT_PATH = os.path.join(CATALOGUE_CERTS_DIR, "catalogue.cert.pem")
KEY_PATH = os.path.join(CATALOGUE_CERTS_DIR, "catalogue.key.pem")
CA_CERT_PATH = os.path.join(CATALOGUE_CERTS_DIR, "ca.cert")


class CatalogueCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self._container = self.unit.get_container("catalogue")
        self.name = "catalogue-k8s"

        port = ServicePort(80, name=f"{self.app.name}")
        self.service_patcher = KubernetesServicePatch(self, [port])

        self._info = CatalogueProvider(charm=self)
        self._ingress = IngressPerAppRequirer(charm=self, port=80, strip_prefix=True)

        url = self.hostname
        extra_sans_dns = [cast(str, urlparse(url).hostname)] if url else None
        self.server_cert = CertHandler(
            self,
            key="catalogue-server-cert",
            peer_relation_name="replicas",
            extra_sans_dns=extra_sans_dns,
        )

        self.framework.observe(
            self.on.catalogue_pebble_ready, self._on_catalogue_pebble_ready  # pyright: ignore
        )
        self.framework.observe(
            self._info.on.items_changed, self._on_items_changed  # pyright: ignore
        )
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self._ingress.on.ready, self._on_ingress_ready)  # pyright: ignore
        self.framework.observe(
            self._ingress.on.revoked, self._on_ingress_revoked  # pyright: ignore
        )
        self.framework.observe(
            self.server_cert.on.cert_changed,  # pyright: ignore
            self._on_server_cert_changed,
        )

    def _on_ingress_ready(self, event: IngressPerAppReadyEvent):
        logger.info("This app's ingress URL: %s", event.url)

    def _on_ingress_revoked(self, event: IngressPerAppRevokedEvent):
        logger.info("This app no longer has ingress")

    def _on_catalogue_pebble_ready(self, event):
        """Event handler for the pebble ready event."""
        container = event.workload
        pebble_layer = {
            "summary": "catalogue layer",
            "description": "pebble config layer for the catalogue",
            "services": {
                "catalogue": {
                    "override": "replace",
                    "summary": "catalogue",
                    "command": "nginx",
                    "startup": "enabled",
                }
            },
        }
        container.add_layer("catalogue", pebble_layer, combine=True)
        container.autostart()

        try:
            self.configure(self.items)
        except:  # noqa
            self._update_status(BlockedStatus("Failed to write configuration"))

        if self.unit.is_leader():
            self._update_status(ActiveStatus())

    def _update_status(self, status):
        if self.unit.is_leader():
            self.app.status = status
        self.unit.status = status

    def _on_upgrade(self, _):
        self.configure(self.items)

    def _on_config_changed(self, _):
        self.configure(self.items)

    def _on_items_changed(self, event: CatalogueItemsChangedEvent):
        self.configure(event.items)

    def _on_server_cert_changed(self, _):
        self._push_certs()
        self.configure(self.items)

    def _push_certs(self):
        for path in [KEY_PATH, CERT_PATH, CA_CERT_PATH]:
            self._container.remove_path(path, recursive=True)

        if self.server_cert.ca:
            self._container.push(CA_CERT_PATH, self.server_cert.ca, make_dirs=True)

        if self.server_cert.cert:
            self._container.push(CERT_PATH, self.server_cert.cert, make_dirs=True)

        if self.server_cert.key:
            self._container.push(KEY_PATH, self.server_cert.key, make_dirs=True)

    def configure(self, items):
        """Reconfigures the catalogue, writing a new config file to the workload."""
        if not self.workload.can_connect():
            return
        if self.workload.exists(CONFIG_PATH):
            self.workload.remove_path(CONFIG_PATH)

        logger.info("Configuring %s application entries", len(items))

        self.workload.push(
            CONFIG_PATH,
            json.dumps({**self.charm_config, "apps": items}),
            make_dirs=True,
        )

    @property
    def items(self):
        """Applications to display in the catalogue."""
        if not self._info:
            return []
        return self._info.items

    @property
    def workload(self):
        """The main workload of the charm."""
        return self.unit.get_container("catalogue")

    @property
    def charm_config(self):
        """The part of the charm config that is set through `juju config`."""
        return {
            "title": self.model.config["title"],
            "tagline": self.model.config["tagline"],
            "description": self.model.config.get("description", ""),
            "links": json.loads(self.model.config["links"]),
        }

    @property
    def hostname(self) -> str:
        """Unit's hostname."""
        return socket.getfqdn()


if __name__ == "__main__":
    main(CatalogueCharm)
