#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charmed operator for creating service catalogues on Kubernetes."""

import json
import logging
import socket
from collections.abc import Mapping, Sequence
from typing import Any, assert_never, cast
from urllib.parse import urlparse

import ops_tracing
from charms.catalogue_k8s.v1.catalogue import (
    CatalogueConsumer,
    CatalogueItem,
    CatalogueItemsChangedEvent,
    CatalogueProvider,
)
from charms.istio_beacon_k8s.v0.service_mesh import ServiceMeshConsumer
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.tls_certificates_interface.v4.tls_certificates import (
    CertificateRequestAttributes,
    TLSCertificatesRequiresV4,
)
from charms.traefik_k8s.v2.ingress import IngressPerAppReadyEvent, IngressPerAppRequirer
from ops.charm import ActionEvent, CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.pebble import Error

import catalogue_state
from catalogue_state import TlsMaterial
from nginx_config import CA_CERT_PATH, CERT_PATH, KEY_PATH, NGINX_CONFIG_PATH
from workload import Workload, WorkloadError

logger = logging.getLogger(__name__)

ROOT_PATH = "/web"
CONFIG_PATH = ROOT_PATH + "/config.json"


class CatalogueCharm(CharmBase):
    """Catalogue charm class."""

    def __init__(self, *args: Any) -> None:  # noqa: ANN401
        """Initialize the charm and register event observers."""
        super().__init__(*args)
        self.name = "catalogue"  # container, layer, service
        self._fqdn = socket.getfqdn()

        self.unit.set_ports(80)

        self._workload = Workload(self.unit.get_container(self.name))

        self.charm_tracing = ops_tracing.Tracing(
            self,
            tracing_relation_name="tracing",
            ca_relation_name="receive-ca-cert",
        )

        self._info = CatalogueProvider(charm=self)

        self._csr_attributes = CertificateRequestAttributes(
            # `common_name` is required but limited to 64 chars.
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

        self._log_forwarder = LogForwarder(self, relation_name="logging")

        self.framework.observe(
            self.on.catalogue_pebble_ready,
            self._on_catalogue_pebble_ready,
        )
        self.framework.observe(
            self._info.on.items_changed,
            self._on_items_changed,
        )
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self._ingress.on.ready, self._on_ingress_ready)
        self.framework.observe(
            self._ingress.on.revoked,
            self._on_ingress_revoked,
        )
        self.framework.observe(
            self._cert_requirer.on.certificate_available,
            self._on_certificate_available,
        )
        self.framework.observe(self.on.get_url_action, self._get_url)

    # --- Action ---

    def _get_url(self, event: ActionEvent) -> None:
        """Return the external hostname to pass to ingress via relation.

        If we do not have an ingress, use the pod dns name as hostname.
        Relying on cluster DNS, those dns names are routable virtually
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

    # --- Event handlers ---

    def _on_ingress_ready(self, event: IngressPerAppReadyEvent) -> None:
        logger.info("This app's ingress URL: %s", event.url)
        self._reconcile(self.items)

    def _on_ingress_revoked(self, _: object) -> None:
        logger.info("This app no longer has ingress")
        self._reconcile(self.items)

    def _on_catalogue_pebble_ready(self, _: object) -> None:
        self._reconcile(self.items)

    def _on_upgrade(self, _: object) -> None:
        self._reconcile(self.items)

    def _on_config_changed(self, _: object) -> None:
        self._reconcile(self.items)

    def _on_items_changed(self, event: CatalogueItemsChangedEvent) -> None:
        self._reconcile(cast(Sequence[Mapping[str, object]], event.items))

    def _on_certificate_available(self, _: object) -> None:
        self._reconcile(self.items)

        # When server cert changes we need to update the scheme we
        # inform traefik.
        parsed = urlparse(self._internal_url)
        port = parsed.port or 80 if parsed.scheme == "http" else 443
        self._ingress.provide_ingress_requirements(scheme=parsed.scheme, port=port)

    # --- Reconciler ---

    def _reconcile(self, items: Sequence[Mapping[str, object]]) -> None:
        """Converge the container toward the desired state.

        Builds intent from charm config and relation data, fetches
        the running state, computes outcomes, and applies them.
        """
        tls_material = self._tls_config
        override_hostname_raw = self.config.get("override_hostname")
        override_hostname = str(override_hostname_raw) if override_hostname_raw else None
        intent = catalogue_state.intent_from(
            items=items,
            charm_config=self.charm_config,
            tls=tls_material,
            override_hostname=override_hostname,
        )

        try:
            state = catalogue_state.fetch(self._workload)
            outcomes = catalogue_state.compute(state, intent)
            for outcome in outcomes:
                self._apply(outcome)
            status = catalogue_state.status_plan(state, intent, outcomes)
        except (Error, WorkloadError) as e:
            self._apply_status(
                catalogue_state.StatusBlocked(
                    remedy=(
                        f"Workload operation failed: {e}; "
                        f"check the container with "
                        f"'juju exec --unit {self.unit.name} -- pebble services'"
                    )
                )
            )
            logger.error("Workload operation failed: %s", e)
            return

        self._apply_status(status)

    def _apply(self, outcome: catalogue_state.CatalogueOutcome) -> None:
        """Perform one decided outcome. Exhaustive match."""
        match outcome:
            case catalogue_state.PushCerts(material=material):
                self._workload.push(CERT_PATH, material.server_cert, make_dirs=True)
                self._workload.push(KEY_PATH, material.private_key, make_dirs=True)
                self._workload.push(CA_CERT_PATH, material.ca_cert, make_dirs=True)
            case catalogue_state.RemoveCerts():
                for path in (KEY_PATH, CERT_PATH, CA_CERT_PATH):
                    self._workload.remove_path(path, recursive=True)
            case catalogue_state.PushNginxConfig(body=body):
                self._workload.push(NGINX_CONFIG_PATH, body, make_dirs=True)
                logger.info("Configuring NGINX web server.")
            case catalogue_state.PushCatalogueConfig(body=body):
                self._workload.push(CONFIG_PATH, json.dumps(body), make_dirs=True)
                apps = body.get("apps", [])
                app_count = len(apps) if isinstance(apps, Sequence) else 0  # pyright: ignore[reportUnknownArgumentType]
                logger.info("Configuring %s application entries", app_count)
            case catalogue_state.EnsureLayer(services=services):
                self._workload.ensure_layer(self.name, services)
            case catalogue_state.RestartNginx(reason=reason):
                logger.info("Restarting nginx: %s", reason)
                self._workload.restart(self.name)
            case catalogue_state.Noop():
                logger.debug("converged: nothing to do.")
            case _ as unreachable:
                assert_never(unreachable)

    def _apply_status(self, status: catalogue_state.CatalogueStatus) -> None:
        """Map a CatalogueStatus ADT to ops statuses and set them.

        Sets both app status (leader-only) and unit status, matching
        the existing semantics.
        """
        match status:
            case catalogue_state.StatusActive(message=message):
                ops_status = ActiveStatus(message) if message else ActiveStatus()
            case catalogue_state.StatusWaiting(reason=reason):
                ops_status = WaitingStatus(reason)
            case catalogue_state.StatusBlocked(remedy=remedy):
                ops_status = BlockedStatus(remedy)
            case _ as unreachable:
                assert_never(unreachable)

        if self.unit.is_leader():
            self.app.status = ops_status
        self.unit.status = ops_status

    # --- Properties ---

    @property
    def items(self) -> Sequence[Mapping[str, object]]:
        """Applications to display in the catalogue."""
        if not self._info:
            return []
        return self._info.items

    @property
    def charm_config(self) -> dict[str, object]:
        """The part of the charm config set through ``juju config``."""
        return {
            "title": self.model.config["title"],
            "tagline": self.model.config["tagline"],
            "description": self.model.config.get("description", ""),
            "links": json.loads(cast(str, self.model.config["links"])),
        }

    @property
    def _internal_url(self) -> str:
        """Return the fqdn dns-based in-cluster (private) address."""
        scheme = "https" if self._tls_available else "http"
        port = 80 if scheme == "http" else 443
        return f"{scheme}://{self._fqdn}:{port}"

    @property
    def _internal_port(self) -> int:
        """Return the port extracted from the internal URL."""
        parsed_url = urlparse(self._internal_url)
        return int(parsed_url.port or 80)

    @property
    def _tls_config(self) -> TlsMaterial | None:
        """TLS material from the certificates relation, if available."""
        certificates, key = self._cert_requirer.get_assigned_certificate(
            certificate_request=self._csr_attributes
        )
        if not (key and certificates):
            return None
        return TlsMaterial(
            server_cert=certificates.certificate.raw,
            ca_cert=certificates.ca.raw,
            private_key=key.raw,
        )

    @property
    def _tls_available(self) -> bool:
        """Whether TLS material is available from the relation."""
        return bool(self._tls_config)


if __name__ == "__main__":
    main(CatalogueCharm)
