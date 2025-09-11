#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charmed operator for creating service catalogues on Kubernetes."""

import json
import logging
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast
from urllib.parse import urlparse

from charms.catalogue_k8s.v1.catalogue import (
    CatalogueConsumer,
    CatalogueItem,
    CatalogueItemsChangedEvent,
    CatalogueProvider,
)
from charms.istio_beacon_k8s.v0.service_mesh import ServiceMeshConsumer
from charms.tempo_coordinator_k8s.v0.charm_tracing import trace_charm
from charms.tempo_coordinator_k8s.v0.tracing import TracingEndpointRequirer, charm_tracing_config
from charms.tls_certificates_interface.v4.tls_certificates import (
    CertificateRequestAttributes,
    TLSCertificatesRequiresV4,
)
from charms.traefik_k8s.v2.ingress import IngressPerAppReadyEvent, IngressPerAppRequirer
from ops.charm import ActionEvent, CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.pebble import ChangeError, Error, Layer, PathError, ProtocolError

from nginx_config import CA_CERT_PATH, CERT_PATH, KEY_PATH, NGINX_CONFIG_PATH, NginxConfigBuilder

logger = logging.getLogger(__name__)

ROOT_PATH = "/web"
CONFIG_PATH = ROOT_PATH + "/config.json"

@dataclass
class TLSConfig:
    """TLS configuration received by the charm over the `certificates` relation."""

    server_cert: str
    ca_cert: str
    private_key: str

@trace_charm(
    tracing_endpoint="tracing_endpoint",
    server_cert="server_ca_cert_path",
    extra_types=(
        CatalogueProvider,
        TLSCertificatesRequiresV4,
        IngressPerAppRequirer,
    ),
)
class CatalogueCharm(CharmBase):
    """Catalogue charm class."""

    _ca_path = "/usr/local/share/ca-certificates/ca.crt"

    def __init__(self, *args):
        super().__init__(*args)
        self.name = "catalogue"  # container, layer, service
        self._fqdn = socket.getfqdn()

        self.unit.set_ports(80)

        self._tracing = TracingEndpointRequirer(self, protocols=["otlp_http"])

        self.tracing_endpoint, self.server_ca_cert_path = charm_tracing_config(
            self._tracing, self._ca_path
        )
        self._info = CatalogueProvider(charm=self)


        self._csr_attributes = CertificateRequestAttributes(
            # the `common_name` field is required but limited to 64 characters.
            # since it's overridden by sans, we can use a short,
            # constrained value like app name.
            common_name=self.app.name,
            sans_dns=frozenset((self._fqdn,)),
        )
        self._cert_requirer = TLSCertificatesRequiresV4(
            charm=self,
            relationship_name="certificates",
            certificate_requests=[self._csr_attributes],
        )

        desc = f"A service catalogue containing {len(self._info.items)} items."

        self._ingress = IngressPerAppRequirer(
            charm=self,
            port=self._internal_port,
            strip_prefix=True,
            redirect_https=True,
            scheme=lambda: urlparse(self._internal_url).scheme,
        )

        self._catalogue_consumer = CatalogueConsumer(
            charm=self,
            relation_name="catalogue-item",
            item=CatalogueItem(
                name=f"{self.model.config['title']}",
                icon="book-open-blank-variant-outline",
                url=self._ingress.url or "about:blank",
                description=desc,
            ),
        )

        self._mesh = ServiceMeshConsumer(self)

        self.framework.observe(
            self.on.catalogue_pebble_ready,
            self._on_catalogue_pebble_ready,  # pyright: ignore
        )
        self.framework.observe(
            self._info.on.items_changed,
            self._on_items_changed,  # pyright: ignore
        )
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self._ingress.on.ready, self._on_ingress_ready)  # pyright: ignore
        self.framework.observe(
            self._ingress.on.revoked,
            self._on_ingress_revoked,  # pyright: ignore
        )
        self.framework.observe(
            self._cert_requirer.on.certificate_available,  # pyright: ignore
            self._on_certificate_available,
        )
        self.framework.observe(self.on.get_url_action, self._get_url)

    def _get_url(self, event: ActionEvent):
        """Return the external hostname to be passed to ingress via the relation.

        If we do not have an ingress, then use the pod dns name as hostname.
        Relying on cluster's DNS service, those dns names are routable virtually
        exclusively inside the cluster.
        """
        output = self._internal_url
        if ingress_url := self._ingress.url:
            output = ingress_url
        event.set_results(
            {
                "url": output,
            }
        )

    def _on_ingress_ready(self, event: IngressPerAppReadyEvent):
        logger.info("This app's ingress URL: %s", event.url)
        self._configure(self.items, push_certs=True)

    def _on_ingress_revoked(self, _):
        logger.info("This app no longer has ingress")
        self._configure(self.items, push_certs=True)

    def _on_catalogue_pebble_ready(self, _):
        # We set push_certs to True here to cover the upgrade sequence. When upgrade-charm fires,
        # the container may not yet be ready, and the certs are written to non-persistent storage
        # (which is a good thing).
        self._configure(self.items, push_certs=True)

    def _update_status(self, status):
        if self.unit.is_leader():
            self.app.status = status
        self.unit.status = status

    def _on_upgrade(self, _):
        # Ideally we would want to push certs on upgrade, but at this point we can't know for sure
        # if pebble-ready (can_connect guard).
        self._configure(self.items)

    def _on_config_changed(self, _):
        self._configure(self.items)

    def _on_items_changed(self, event: CatalogueItemsChangedEvent):
        self._configure(event.items)

    def _on_certificate_available(self, _):
        self._configure(self.items, push_certs=True)

        # When server cert changes we need to update the scheme we inform traefik.
        parsed = urlparse(self._internal_url)
        port = parsed.port or 80 if parsed.scheme == "http" else 443
        self._ingress.provide_ingress_requirements(scheme=parsed.scheme, port=port)

    def _push_certs(self):
        ca_cert_path = Path(self._ca_path)
        if tls_config := self._tls_config:
            self.workload.push(CERT_PATH, tls_config.server_cert, make_dirs=True)
            self.workload.push(KEY_PATH, tls_config.private_key, make_dirs=True)
            self.workload.push(CA_CERT_PATH, tls_config.ca_cert, make_dirs=True)
            # write CA certificate to the charm container for charm tracing
            ca_cert_path.parent.mkdir(exist_ok=True, parents=True)
            ca_cert_path.write_text(tls_config.ca_cert)
            subprocess.check_output(["update-ca-certificates", "--fresh"])
        else:
            for path in [KEY_PATH, CERT_PATH, CA_CERT_PATH]:
                self.workload.remove_path(path, recursive=True)
            ca_cert_path.unlink(missing_ok=True)

    def _configure(self, items, push_certs: bool = False):
        if not self.workload.can_connect():
            self._update_status(WaitingStatus("Waiting for Pebble ready"))
            return

        if push_certs:
            try:
                self._push_certs()
            except (ProtocolError, PathError, Exception) as e:
                self._update_status(BlockedStatus(str(e)))
                logger.error(str(e))
                return

        if base_hostname := self.config.get("base_hostname"):
            # TODO: replace url hostname with base_hostname for all urls

        nginx_config_changed = self._update_web_server_config()
        catalogue_config_changed = self._update_catalogue_config(items)
        pebble_layer_changed = self._update_pebble_layer()
        restart = any([nginx_config_changed, catalogue_config_changed, pebble_layer_changed])

        if restart:
            try:
                self.workload.restart(self.name)
            except ChangeError as e:
                msg = f"Failed to restart Catalogue: {e}"
                self._update_status(BlockedStatus(msg))
                logger.error(msg)
                return

        self._update_status(ActiveStatus())

    def _update_pebble_layer(self) -> bool:
        current_layer = self.workload.get_plan()

        if current_layer.services == self._pebble_layer.services:
            return False

        self.workload.add_layer(self.name, self._pebble_layer, combine=True)
        self.workload.autostart()
        return True

    def _update_catalogue_config(self, items) -> bool:
        config = {**self.charm_config, "apps": items}

        if self._running_catalogue_config == config:
            return False

        self.workload.push(
            CONFIG_PATH,
            json.dumps({**self.charm_config, "apps": items}),
            make_dirs=True,
        )
        logger.info("Configuring %s application entries", len(items))
        return True

    def _update_web_server_config(self) -> bool:
        config = NginxConfigBuilder(self._tls_available).build()

        if self._running_nginx_config == config:
            return False

        self.workload.push(NGINX_CONFIG_PATH, config, make_dirs=True)
        logger.info("Configuring NGINX web server.")
        return True

    @property
    def _running_nginx_config(self) -> str:
        """Get the on-disk Nginx config."""
        if not self.workload.can_connect():
            return ""

        try:
            return str(self.workload.pull(NGINX_CONFIG_PATH, encoding="utf-8").read())
        except (FileNotFoundError, Error) as e:
            logger.error("Failed to retrieve Nginx config %s", e)
            return ""

    @property
    def _running_catalogue_config(self) -> dict:
        """Get the on-disk Catalogue config."""
        if not self.workload.can_connect():
            return {}

        try:
            return json.loads(self.workload.pull(CONFIG_PATH, encoding="utf-8").read())
        except (FileNotFoundError, Error) as e:
            logger.error("Failed to retrieve Catalogue config %s", e)
            return {}

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
                        "command": f"nginx -g 'daemon off;' -c {NGINX_CONFIG_PATH}",
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
            "links": json.loads(cast(str, self.model.config["links"])),
        }

    @property
    def _internal_url(self) -> str:
        """Return the fqdn dns-based in-cluster (private) address of the catalogue server."""
        scheme = "https" if self._tls_available else "http"
        port = 80 if scheme == "http" else 443
        return f"{scheme}://{self._fqdn}:{port}"

    @property
    def _internal_port(self) -> int:
        """Return the port extracted from the internal URL."""
        parsed_url = urlparse(self._internal_url)
        return int(parsed_url.port or 80)

    @property
    def _tls_config(self) -> Optional[TLSConfig]:
        certificates, key = self._cert_requirer.get_assigned_certificate(
            certificate_request=self._csr_attributes
        )
        if not (key and certificates):
            return None
        return TLSConfig(certificates.certificate.raw, certificates.ca.raw, key.raw)

    @property
    def _tls_available(self) -> bool:
        return bool(self._tls_config)

if __name__ == "__main__":
    main(CatalogueCharm)
