#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Pure functional core for the Catalogue charm.

Decides what to do without performing any effects. Imports only stdlib
and nginx_config — never ops or workload.

The status union is pull-only: the pure core derives Active and
Waiting from container state. Blocked is a push status built by the
shell from exception context — the core never produces it.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, assert_never
from urllib.parse import urlparse, urlunparse

from nginx_config import NGINX_CONFIG_PATH, NginxConfigBuilder

logger = logging.getLogger(__name__)

# --- Constants ---

SERVICE_NAME = "catalogue"

LAYER_SERVICES: Mapping[str, Mapping[str, str]] = {
    SERVICE_NAME: {
        "override": "replace",
        "summary": "catalogue",
        "command": f"nginx -g 'daemon off;' -c {NGINX_CONFIG_PATH}",
        "startup": "enabled",
    }
}

WAITING_FOR_PEBBLE_MESSAGE = "Waiting for Pebble ready"


# --- Data types ---


@dataclass(frozen=True)
class TlsMaterial:
    """TLS material received over the `certificates` relation."""

    server_cert: str
    ca_cert: str
    private_key: str


@dataclass(frozen=True)
class CatalogueIntent:
    """The desired state of the Catalogue workload.

    Every field is a complete value, not a delta — the pure core
    compares state against intent to decide what to do.
    """

    nginx_config: str
    """Rendered nginx config (HTTP or HTTPS per TLS availability)."""

    catalogue_config: Mapping[str, object]
    """Complete desired config.json body, including ``apps``."""

    layer_services: Mapping[str, Mapping[str, str]]
    """Pebble layer services dict (constant)."""

    tls: TlsMaterial | None
    """TLS material when available, None otherwise."""


# --- State ---


@dataclass(frozen=True)
class ContainerNotReady:
    """The Pebble API is not reachable yet."""


@dataclass(frozen=True)
class ContainerReady:
    """Every fact read from the running container."""

    running_nginx_config: str
    """The nginx config currently on disk (empty string if absent)."""

    running_catalogue_config: Mapping[str, object]
    """The config.json currently on disk (empty dict if absent)."""

    running_layer_services: Mapping[str, Mapping[str, str]]
    """The services dict from the running Pebble plan."""

    running_certs: bool
    """Whether all three cert files (cert, key, CA) exist on disk."""


type CatalogueState = ContainerNotReady | ContainerReady


# --- Facts protocol ---


class Facts(Protocol):
    """The reads the decision depends on.

    Implemented by the workload module. Each method reads one fact
    from the container; none of them mutate.
    """

    def can_connect(self) -> bool:
        """Whether the Pebble API is reachable (point-in-time)."""
        ...

    def nginx_config(self) -> str:
        """Read the on-disk nginx config. Returns "" on failure."""
        ...

    def catalogue_config(self) -> Mapping[str, object]:
        """Read the on-disk catalogue config. Returns {} on failure."""
        ...

    def layer_services(self) -> Mapping[str, Mapping[str, str]]:
        """Read the running Pebble layer services dict."""
        ...

    def certs(self) -> bool:
        """Whether all three cert files exist on disk."""
        ...


# --- Outcomes ---


@dataclass(frozen=True)
class PushCerts:
    """Push TLS material to the container.

    Container storage is ephemeral, so this outcome recurs whenever
    the state does not show the certs.
    """

    material: TlsMaterial


@dataclass(frozen=True)
class RemoveCerts:
    """Remove TLS material from the container."""


@dataclass(frozen=True)
class PushNginxConfig:
    """Push the rendered nginx config to NGINX_CONFIG_PATH."""

    body: str


@dataclass(frozen=True)
class PushCatalogueConfig:
    """Push the config.json body (charm config plus catalogue items)."""

    body: Mapping[str, object]


@dataclass(frozen=True)
class EnsureLayer:
    """Ensure the Pebble layer matches intent."""

    services: Mapping[str, Mapping[str, str]]


@dataclass(frozen=True)
class RestartNginx:
    """Restart the nginx service."""

    reason: str


@dataclass(frozen=True)
class Noop:
    """Nothing to do — state matches intent."""


type CatalogueOutcome = (
    PushCerts
    | RemoveCerts
    | PushNginxConfig
    | PushCatalogueConfig
    | EnsureLayer
    | RestartNginx
    | Noop
)


# --- Status ---


@dataclass(frozen=True)
class StatusActive:
    """The unit is correctly offering its services."""

    message: str = ""


@dataclass(frozen=True)
class StatusWaiting:
    """Waiting on another application or on the workload to be ready."""

    reason: str


type CatalogueStatus = StatusActive | StatusWaiting


# --- Fetch ---


def fetch(facts: Facts) -> CatalogueState:
    """Read every fact the decision depends on.

    The only impure read path — nothing downstream touches the
    container or relation data.
    """
    if not facts.can_connect():
        return ContainerNotReady()
    return ContainerReady(
        running_nginx_config=facts.nginx_config(),
        running_catalogue_config=facts.catalogue_config(),
        running_layer_services=facts.layer_services(),
        running_certs=facts.certs(),
    )


# --- Intent ---


def intent_from(
    items: Sequence[Mapping[str, object]],
    charm_config: Mapping[str, object],
    tls: TlsMaterial | None,
    override_hostname: str | None,
) -> CatalogueIntent:
    """Build the desired state from charm config and relation data.

    Pure — no IO, no exceptions for control flow.

    Args:
        items: Catalogue items from the ``catalogue`` relation.
        charm_config: Charm config mapping with ``links`` already
            parsed from JSON.
        tls: TLS material when available, None otherwise.
        override_hostname: Optional hostname override from config.
    """
    tls_available = tls is not None
    nginx_config = NginxConfigBuilder(tls_available).build()

    # Apply override_hostname to items — same urlparse netloc-replace
    # logic (and the same KeyError on a missing "url" key) as the
    # original _override_hostname method.
    if override_hostname:
        processed_items: list[Mapping[str, object]] = []
        for item in items:
            mutable = dict(item)
            parsed = urlparse(str(mutable["url"]))
            mutable["url"] = urlunparse(parsed._replace(netloc=override_hostname))
            processed_items.append(mutable)
        items = processed_items

    catalogue_config: dict[str, object] = {**charm_config, "apps": list(items)}
    return CatalogueIntent(
        nginx_config=nginx_config,
        catalogue_config=catalogue_config,
        layer_services=LAYER_SERVICES,
        tls=tls,
    )


# --- Compute ---


def compute(
    state: CatalogueState,
    intent: CatalogueIntent,
) -> tuple[CatalogueOutcome, ...]:
    """Decide what to do.

    Pure — no IO, no exceptions for control flow. Exhaustive match
    on the state union; pyright fails the build if a case is missing.
    """
    match state:
        case ContainerNotReady():
            return (Noop(),)
        case ContainerReady():
            return _compute_ready(state, intent)
        case _ as unreachable:  # pragma: no cover — unreachable by
            # construction: the union is closed and every member has an
            # arm; pyright fails the build if that ever changes.
            assert_never(unreachable)


def _compute_ready(
    state: ContainerReady,
    intent: CatalogueIntent,
) -> tuple[CatalogueOutcome, ...]:
    """Compute outcomes when the container is reachable."""
    outcomes: list[CatalogueOutcome] = []

    # Certs first — the TLS nginx config references the cert files,
    # so certs must be on disk before the config is pushed.
    if intent.tls is not None and not state.running_certs:
        outcomes.append(PushCerts(material=intent.tls))
    elif intent.tls is None and state.running_certs:
        outcomes.append(RemoveCerts())

    nginx_changed = state.running_nginx_config != intent.nginx_config
    catalogue_changed = state.running_catalogue_config != intent.catalogue_config
    layer_changed = state.running_layer_services != intent.layer_services

    if nginx_changed:
        outcomes.append(PushNginxConfig(body=intent.nginx_config))
    if catalogue_changed:
        outcomes.append(PushCatalogueConfig(body=intent.catalogue_config))
    if layer_changed:
        outcomes.append(EnsureLayer(services=intent.layer_services))

    if nginx_changed or catalogue_changed or layer_changed:
        reasons: list[str] = []
        if nginx_changed:
            reasons.append("nginx config changed")
        if catalogue_changed:
            reasons.append("catalogue config changed")
        if layer_changed:
            reasons.append("pebble layer changed")
        outcomes.append(RestartNginx(reason="; ".join(reasons)))

    if not outcomes:
        outcomes.append(Noop())

    return tuple(outcomes)


# --- Status ---


def status_without_intent(state: CatalogueState) -> CatalogueStatus:
    """Determine status from state alone (no intent available).

    Args:
        state: The fetched container state.
    """
    match state:
        case ContainerNotReady():
            return StatusWaiting(reason=WAITING_FOR_PEBBLE_MESSAGE)
        case ContainerReady():
            return StatusActive()
        case _ as unreachable:  # pragma: no cover — unreachable by
            # construction: the union is closed and every member has an
            # arm; pyright fails the build if that ever changes.
            assert_never(unreachable)
