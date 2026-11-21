#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Pure tests for catalogue_state.py — zero mocks, no ops.testing."""

from collections.abc import Mapping

from catalogue_state import (
    CatalogueIntent,
    ContainerNotReady,
    ContainerReady,
    EnsureLayer,
    Noop,
    PushCatalogueConfig,
    PushCerts,
    PushNginxConfig,
    RemoveCerts,
    RestartNginx,
    StatusActive,
    StatusWaiting,
    TlsMaterial,
    compute,
    fetch,
    intent_from,
    status_without_intent,
)

# --- Shared test data ---

TLS = TlsMaterial(
    server_cert="server-cert-data",
    ca_cert="ca-cert-data",
    private_key="private-key-data",
)

CHARM_CONFIG: Mapping[str, object] = {
    "title": "Test Catalogue",
    "tagline": "A test catalogue",
    "description": "Test description",
    "links": [{"name": "docs", "url": "https://docs.example.com"}],
}

ITEMS: list[Mapping[str, object]] = [
    {
        "name": "app-one",
        "url": "https://app-one.example.com",
        "icon": "icon-one",
        "description": "First app",
        "api_docs": "",
        "api_endpoints": {},
    },
]

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


class FakeFacts:
    """A fake Facts implementation for testing fetch()."""

    def __init__(
        self,
        can_connect: bool = True,
        nginx_config: str = "",
        catalogue_config: Mapping[str, object] | None = None,
        layer_services: Mapping[str, Mapping[str, str]] | None = None,
        certs: bool = False,
    ) -> None:
        """Initialise with configurable fake facts."""
        self._can_connect = can_connect
        self._nginx_config = nginx_config
        self._catalogue_config: Mapping[str, object] = (
            catalogue_config if catalogue_config is not None else {}
        )
        self._layer_services: Mapping[str, Mapping[str, str]] = (
            layer_services if layer_services is not None else {}
        )
        self._certs = certs

    def can_connect(self) -> bool:
        return self._can_connect

    def nginx_config(self) -> str:
        return self._nginx_config

    def catalogue_config(self) -> Mapping[str, object]:
        return self._catalogue_config

    def layer_services(self) -> Mapping[str, Mapping[str, str]]:
        return self._layer_services

    def certs(self) -> bool:
        return self._certs


# --- fetch tests ---


def test_fetch_container_not_ready() -> None:
    """Fetch returns ContainerNotReady when can_connect is False."""
    # GIVEN a container whose Pebble API is not reachable
    facts = FakeFacts(can_connect=False)

    # WHEN state is fetched
    state = fetch(facts)

    # THEN the state reflects the unreachable container
    assert isinstance(state, ContainerNotReady)


def test_fetch_container_ready() -> None:
    """Fetch returns ContainerReady with all facts when connected."""
    # GIVEN a reachable container with known state
    facts = FakeFacts(
        can_connect=True,
        nginx_config="some-nginx-config",
        catalogue_config={"title": "Test"},
        layer_services={"catalogue": {"override": "replace"}},
        certs=True,
    )

    # WHEN state is fetched
    state = fetch(facts)

    # THEN every fact is captured
    assert isinstance(state, ContainerReady)
    assert state.running_nginx_config == "some-nginx-config"
    assert state.running_catalogue_config == {"title": "Test"}
    assert state.running_layer_services == {"catalogue": {"override": "replace"}}
    assert state.running_certs is True


# --- intent_from tests ---


def test_intent_from_http_no_tls() -> None:
    """intent_from builds HTTP intent when TLS is absent."""
    # GIVEN no TLS material
    # WHEN intent is built
    intent = intent_from(items=ITEMS, charm_config=CHARM_CONFIG, tls=None, override_hostname=None)

    # THEN the nginx config is HTTP, catalogue config includes apps,
    # layer is constant
    assert intent.nginx_config == EXPECTED_NGINX_HTTP
    assert intent.tls is None
    assert intent.catalogue_config["title"] == "Test Catalogue"
    assert intent.catalogue_config["apps"] == ITEMS
    assert "catalogue" in intent.layer_services


def test_intent_from_https_with_tls() -> None:
    """intent_from builds HTTPS intent when TLS is present."""
    # GIVEN TLS material
    # WHEN intent is built
    intent = intent_from(items=ITEMS, charm_config=CHARM_CONFIG, tls=TLS, override_hostname=None)

    # THEN the nginx config includes TLS directives
    assert intent.tls is TLS
    assert "ssl_certificate" in intent.nginx_config
    assert "listen               443 ssl" in intent.nginx_config


def test_intent_from_override_hostname() -> None:
    """intent_from rewrites item URLs when override_hostname is set."""
    # GIVEN an override hostname
    # WHEN intent is built
    intent = intent_from(
        items=ITEMS,
        charm_config=CHARM_CONFIG,
        tls=None,
        override_hostname="custom.host",
    )

    # THEN the item URL netloc is replaced
    apps = intent.catalogue_config["apps"]
    assert isinstance(apps, list)
    assert apps[0]["url"] == "https://custom.host"


def test_intent_from_override_hostname_empty_skipped() -> None:
    """intent_from skips empty override_hostname."""
    # GIVEN an empty override hostname
    # WHEN intent is built
    intent = intent_from(items=ITEMS, charm_config=CHARM_CONFIG, tls=None, override_hostname="")

    # THEN the item URL is unchanged
    apps = intent.catalogue_config["apps"]
    assert isinstance(apps, list)
    assert apps[0]["url"] == "https://app-one.example.com"


# --- compute tests ---


def _intent(
    nginx_config: str = EXPECTED_NGINX_HTTP,
    catalogue_config: Mapping[str, object] | None = None,
    layer_services: Mapping[str, Mapping[str, str]] | None = None,
    tls: TlsMaterial | None = None,
) -> CatalogueIntent:
    """Build an intent with defaults for compute tests."""
    if catalogue_config is None:
        catalogue_config = {**CHARM_CONFIG, "apps": ITEMS}
    if layer_services is None:
        layer_services = {
            "catalogue": {
                "override": "replace",
                "summary": "catalogue",
                "command": "nginx",
                "startup": "enabled",
            }
        }
    return CatalogueIntent(
        nginx_config=nginx_config,
        catalogue_config=catalogue_config,
        layer_services=layer_services,
        tls=tls,
    )


def _ready(
    running_nginx_config: str = EXPECTED_NGINX_HTTP,
    running_catalogue_config: Mapping[str, object] | None = None,
    running_layer_services: Mapping[str, Mapping[str, str]] | None = None,
    running_certs: bool = False,
) -> ContainerReady:
    """Build a ContainerReady with defaults for compute tests."""
    if running_catalogue_config is None:
        running_catalogue_config = {**CHARM_CONFIG, "apps": ITEMS}
    if running_layer_services is None:
        running_layer_services = {
            "catalogue": {
                "override": "replace",
                "summary": "catalogue",
                "command": "nginx",
                "startup": "enabled",
            }
        }
    return ContainerReady(
        running_nginx_config=running_nginx_config,
        running_catalogue_config=running_catalogue_config,
        running_layer_services=running_layer_services,
        running_certs=running_certs,
    )


def test_compute_container_not_ready_yields_noop() -> None:
    """Compute returns only Noop when the container is not ready."""
    # GIVEN a container whose Pebble API is not reachable
    state: ContainerNotReady = ContainerNotReady()
    intent = _intent()

    # WHEN outcomes are computed
    outcomes = compute(state, intent)

    # THEN nothing is applied — the status handler reports Waiting
    assert outcomes == (Noop(),)


def test_compute_no_delta_yields_noop() -> None:
    """Compute returns only Noop when state matches intent."""
    # GIVEN a running container whose state matches intent exactly
    intent = _intent()
    state = _ready()

    # WHEN outcomes are computed
    outcomes = compute(state, intent)

    # THEN nothing happens — this is the "safe to run twice" proof
    assert outcomes == (Noop(),)


def test_compute_nginx_delta_yields_push_and_restart() -> None:
    """Compute returns PushNginxConfig + RestartNginx on delta."""
    # GIVEN a running container with a different nginx config
    intent = _intent(nginx_config="new-nginx-config")
    state = _ready(running_nginx_config="old-nginx-config")

    # WHEN outcomes are computed
    outcomes = compute(state, intent)

    # THEN the nginx config is pushed and nginx is restarted
    assert len(outcomes) == 2
    assert isinstance(outcomes[0], PushNginxConfig)
    assert outcomes[0].body == "new-nginx-config"
    assert isinstance(outcomes[1], RestartNginx)
    assert "nginx config changed" in outcomes[1].reason


def test_compute_catalogue_delta_yields_push_and_restart() -> None:
    """Compute returns PushCatalogueConfig + RestartNginx on delta."""
    # GIVEN a running container with a different catalogue config
    new_config: dict[str, object] = {
        **CHARM_CONFIG,
        "apps": [
            {
                "name": "new-app",
                "url": "https://new.example.com",
                "icon": "icon",
                "description": "",
                "api_docs": "",
                "api_endpoints": {},
            }
        ],
    }
    intent = _intent(catalogue_config=new_config)
    state = _ready()

    # WHEN outcomes are computed
    outcomes = compute(state, intent)

    # THEN the catalogue config is pushed and nginx is restarted
    assert len(outcomes) == 2
    assert isinstance(outcomes[0], PushCatalogueConfig)
    assert outcomes[0].body == new_config
    assert isinstance(outcomes[1], RestartNginx)
    assert "catalogue config changed" in outcomes[1].reason


def test_compute_layer_delta_yields_ensure_and_restart() -> None:
    """Compute returns EnsureLayer + RestartNginx on layer delta."""
    # GIVEN a running container with different layer services
    new_services: Mapping[str, Mapping[str, str]] = {
        "catalogue": {
            "override": "replace",
            "summary": "catalogue",
            "command": "new-command",
            "startup": "enabled",
        }
    }
    intent = _intent(layer_services=new_services)
    state = _ready()

    # WHEN outcomes are computed
    outcomes = compute(state, intent)

    # THEN the layer is ensured and nginx is restarted
    assert len(outcomes) == 2
    assert isinstance(outcomes[0], EnsureLayer)
    assert outcomes[0].services == new_services
    assert isinstance(outcomes[1], RestartNginx)
    assert "pebble layer changed" in outcomes[1].reason


def test_compute_certs_present_when_tls_set() -> None:
    """Compute returns PushCerts when TLS set but certs absent."""
    # GIVEN TLS material in intent but no certs on disk
    intent = _intent(tls=TLS)
    state = _ready(running_certs=False)

    # WHEN outcomes are computed
    outcomes = compute(state, intent)

    # THEN certs are pushed (before nginx config — ordering matters)
    assert isinstance(outcomes[0], PushCerts)
    assert outcomes[0].material is TLS


def test_compute_certs_removed_when_tls_absent() -> None:
    """Compute returns RemoveCerts when TLS absent but certs exist."""
    # GIVEN no TLS material in intent but certs on disk
    intent = _intent(tls=None)
    state = _ready(running_certs=True)

    # WHEN outcomes are computed
    outcomes = compute(state, intent)

    # THEN certs are removed
    assert isinstance(outcomes[0], RemoveCerts)


def test_compute_certs_before_nginx_push() -> None:
    """Compute orders PushCerts before PushNginxConfig."""
    # GIVEN TLS material in intent, certs absent, and nginx config
    # differs
    intent = _intent(tls=TLS, nginx_config="new-nginx-with-tls")
    state = _ready(running_certs=False, running_nginx_config="old-nginx")

    # WHEN outcomes are computed
    outcomes = compute(state, intent)

    # THEN certs come before the nginx config push
    assert isinstance(outcomes[0], PushCerts)
    assert isinstance(outcomes[1], PushNginxConfig)
    assert isinstance(outcomes[2], RestartNginx)


def test_compute_all_three_deltas_yields_all_outcomes() -> None:
    """Compute returns all outcomes when all three configs differ."""
    # GIVEN a running container where everything differs from intent
    new_nginx = "new-nginx"
    new_config: dict[str, object] = {**CHARM_CONFIG, "apps": []}
    new_services: Mapping[str, Mapping[str, str]] = {
        "catalogue": {
            "override": "replace",
            "summary": "catalogue",
            "command": "new-cmd",
            "startup": "enabled",
        }
    }
    intent = _intent(
        nginx_config=new_nginx, catalogue_config=new_config, layer_services=new_services
    )
    state = _ready(
        running_nginx_config="old-nginx",
        running_catalogue_config={**CHARM_CONFIG, "apps": ITEMS},
        running_layer_services={
            "catalogue": {
                "override": "replace",
                "summary": "catalogue",
                "command": "old-cmd",
                "startup": "enabled",
            }
        },
    )

    # WHEN outcomes are computed
    outcomes = compute(state, intent)

    # THEN all three pushes plus a restart are produced
    outcome_types = tuple(type(o) for o in outcomes)
    assert outcome_types == (PushNginxConfig, PushCatalogueConfig, EnsureLayer, RestartNginx)


# --- status tests ---


def test_status_without_intent_not_ready() -> None:
    """status_without_intent returns Waiting when not ready."""
    # GIVEN a container that is not ready
    state: ContainerNotReady = ContainerNotReady()

    # WHEN status is determined without intent
    status = status_without_intent(state)

    # THEN the unit reports Waiting
    assert isinstance(status, StatusWaiting)


def test_status_without_intent_ready() -> None:
    """status_without_intent returns Active when ready."""
    # GIVEN a ready container
    state = _ready()

    # WHEN status is determined without intent
    status = status_without_intent(state)

    # THEN the unit reports Active
    assert isinstance(status, StatusActive)
