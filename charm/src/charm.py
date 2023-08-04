#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charmed operator for creating service catalogues on Kubernetes."""

import json
import logging
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
from nginx_config import CA_CERT_PATH, CERT_PATH, KEY_PATH, NGINX_CONFIG_PATH, NginxConfigBuilder
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus
from ops.pebble import Layer

logger = logging.getLogger(__name__)

ROOT_PATH = "/web"
CONFIG_PATH = ROOT_PATH + "/config.json"


class CatalogueCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.name = "catalogue"

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

    def _on_catalogue_pebble_ready(self, _):
        """Event handler for the pebble ready event."""
        self._configure(self.items)

    def _update_status(self, status):
        if self.unit.is_leader():
            self.app.status = status
        self.unit.status = status

    def _on_upgrade(self, _):
        self._configure(self.items)

    def _on_config_changed(self, _):
        self._configure(self.items)

    def _on_items_changed(self, event: CatalogueItemsChangedEvent):
        self._configure(event.items)

    def _on_server_cert_changed(self, _):
        self._push_certs()
        self._configure(self.items)

    def _push_certs(self):
        for path in [KEY_PATH, CERT_PATH, CA_CERT_PATH]:
            self.workload.remove_path(path, recursive=True)

        if self.server_cert.ca:
            self.workload.push(CA_CERT_PATH, self.server_cert.ca, make_dirs=True)

        if self.server_cert.cert:
            self.workload.push(CERT_PATH, self.server_cert.cert, make_dirs=True)

        if self.server_cert.key:
            self.workload.push(KEY_PATH, self.server_cert.key, make_dirs=True)

    def _configure(self, items):
        if not self.workload.can_connect():
            return

        self._configure_web_server()
        self.workload.add_layer(self.name, self._pebble_layer, combine=True)

        # TODO: Reload the workload only configs files change
        self.workload.restart(self.name)

        try:
            if self.workload.exists(CONFIG_PATH):
                self.workload.remove_path(CONFIG_PATH)

            logger.info("Configuring %s application entries", len(items))

            self.workload.push(
                CONFIG_PATH,
                json.dumps({**self.charm_config, "apps": items}),
                make_dirs=True,
            )
        except:  # noqa TODO: catch a specific exception
            self._update_status(BlockedStatus("Failed to write configuration"))

        if self.unit.is_leader():
            self._update_status(ActiveStatus())

    def _configure_web_server(self):
        config = NginxConfigBuilder(self._tls_enabled).build()
        self.workload.push(NGINX_CONFIG_PATH, config, make_dirs=True)
        logger.info("Configuring nginx web server.")

    @property
    def _pebble_layer(self) -> Layer:
        return Layer(
            {
                "summary": "catalogue layer",
                "description": "pebble config layer for the catalogue",
                "services": {
                    self.name: {
                        "override": "replace",
                        "summary": "catalogue",
                        "command": f"nginx -c {NGINX_CONFIG_PATH}",
                        "startup": "enabled",
                    }
                },
            }
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
        return self.unit.get_container(self.name)

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

    @property
    def _tls_enabled(self) -> bool:
        return bool(self.server_cert.cert)


if __name__ == "__main__":
    main(CatalogueCharm)
