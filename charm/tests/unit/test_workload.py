#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for workload.py with a FakeContainer at constructor seam."""

import json
from collections.abc import Mapping
from typing import Any, cast

import pytest
from ops.model import Container
from ops.pebble import PathError, ServiceInfo, ServiceStatus

from nginx_config import CA_CERT_PATH, CERT_PATH, KEY_PATH, NGINX_CONFIG_PATH
from workload import ServiceNotRunningError, Workload

CONTAINER_NAME = "catalogue"
ROOT_PATH = "/web"
CONFIG_PATH = ROOT_PATH + "/config.json"


def _make_service_info(name: str, status: ServiceStatus) -> ServiceInfo:
    """Build a ServiceInfo-like object with the given status."""

    class _FakeServiceInfo:
        def is_running(self) -> bool:
            return status == ServiceStatus.ACTIVE

        @property
        def current(self) -> ServiceStatus:
            return status

        @property
        def name(self) -> str:
            return name

    # The cast is the annotated seam: the fake models ServiceInfo's
    # behaviour without subclassing the ops class.
    return cast("ServiceInfo", _FakeServiceInfo())


def _workload(fake: FakeContainer) -> Workload:
    """Build a Workload over the fake container.

    The cast is the one annotated seam: FakeContainer models the
    container's behaviour without being an ops.model.Container.
    """
    return Workload(cast("Container", fake))


# --- FakeContainer ---


class FakeContainer:
    """A fake ops.model.Container that lies the way Pebble lies.

    Maintains a files dict, a services dict, and a plan dict so that
    read-back verification (non-negotiable #6) is tested directly:
    restart() returning is not evidence the service runs; a pull that
    returns yesterday's config after a push is a real failure mode.
    """

    def __init__(
        self,
        *,
        can_connect: bool = True,
        files: dict[str, str] | None = None,
        services: Mapping[str, ServiceStatus] | None = None,
    ) -> None:
        """Initialise with configurable state.

        Args:
            can_connect: Whether the Pebble API is reachable.
            files: Initial filesystem contents (path -> content).
            services: Initial service statuses (name -> ServiceStatus).
        """
        self._can_connect = can_connect
        self.name = CONTAINER_NAME
        self.files: dict[str, str] = dict(files) if files else {}
        self._services: dict[str, ServiceStatus] = dict(services) if services else {}
        self._layers: dict[str, dict[str, Any]] = {}
        self._plan_services: dict[str, dict[str, Any]] = {}

        # Call records for assertions.
        self.restart_calls: list[str] = []
        self.add_layer_calls: list[tuple[str, dict[str, Any], bool]] = []
        self.autostart_calls: list[tuple[()]] = []
        self.remove_path_calls: list[tuple[str, bool]] = []

    # -- Pebble API --

    def can_connect(self) -> bool:
        """Whether the Pebble API is reachable (point-in-time)."""
        return self._can_connect

    def push(self, path: str, source: str, *, make_dirs: bool = False) -> None:
        """Push a file to the container."""
        self.files[path] = source

    def pull(self, path: str, *, encoding: str = "utf-8") -> Any:  # noqa: ANN401
        """Pull a file from the container.

        Raises PathError("not-found") when the file does not exist,
        matching the real Pebble API.
        """
        if path not in self.files:
            raise PathError("not-found", f"stat {path}: no such file or directory")

        class _File:
            def __init__(self, content: str) -> None:
                self._content = content

            def read(self) -> str:
                return self._content

            def __enter__(self) -> _File:
                return self

            def __exit__(self, *args: object) -> None:
                pass

        return _File(self.files[path])

    def exists(self, path: str) -> bool:
        """Whether a path exists in the container."""
        return path in self.files

    def remove_path(self, path: str, *, recursive: bool = False) -> None:
        """Remove a path from the container."""
        self.remove_path_calls.append((path, recursive))
        self.files.pop(path, None)

    def get_services(self, *names: str) -> dict[str, ServiceInfo]:
        """Get service statuses.

        Returns ServiceInfo-like objects whose is_running() reflects
        the configured status.
        """
        result: dict[str, ServiceInfo] = {}
        targets = set(names) if names else set(self._services.keys())
        for name in targets:
            svc_status = self._services.get(name, ServiceStatus.INACTIVE)
            result[name] = _make_service_info(name, svc_status)
        return result

    def restart(self, service: str) -> None:
        """Record a restart call."""
        self.restart_calls.append(service)

    def add_layer(self, label: str, layer: dict[str, Any], *, combine: bool = False) -> None:
        """Add a Pebble layer."""
        self.add_layer_calls.append((label, layer, combine))
        self._layers[label] = layer
        # Merge services into the plan.
        if "services" in layer:
            self._plan_services.update(layer["services"])

    def autostart(self) -> None:
        """Record an autostart call."""
        self.autostart_calls.append(())

    def get_plan(self) -> Any:  # noqa: ANN401
        """Return a plan-like object with the current services."""

        class _FakePlan:
            def __init__(self, services: dict[str, Any]) -> None:
                self.services: dict[str, Any] = {}
                for svc_name, svc_data in services.items():

                    class _FakeService:
                        def to_dict(self, _data: dict[str, Any] = svc_data) -> dict[str, Any]:
                            return dict(_data)

                    self.services[svc_name] = _FakeService()

        return _FakePlan(self._plan_services)


# --- Facts tests ---


def test_can_connect_true() -> None:
    """can_connect returns True when the container is reachable."""
    # GIVEN a connected container
    fake = FakeContainer(can_connect=True)
    wl = _workload(fake)

    # WHEN can_connect is called
    result = wl.can_connect()

    # THEN it returns True
    assert result is True


def test_can_connect_false() -> None:
    """can_connect returns False when the container is not reachable."""
    # GIVEN a disconnected container
    fake = FakeContainer(can_connect=False)
    wl = _workload(fake)

    # WHEN can_connect is called
    result = wl.can_connect()

    # THEN it returns False
    assert result is False


def test_nginx_config_returns_content() -> None:
    """nginx_config reads the on-disk nginx config."""
    # GIVEN a container with a pushed nginx config
    fake = FakeContainer(files={NGINX_CONFIG_PATH: "test-nginx-config"})
    wl = _workload(fake)

    # WHEN the config is read
    result = wl.nginx_config()

    # THEN the content matches
    assert result == "test-nginx-config"


def test_nginx_config_returns_empty_on_missing() -> None:
    """nginx_config returns '' when the file does not exist."""
    # GIVEN a container with no nginx config
    fake = FakeContainer()
    wl = _workload(fake)

    # WHEN the config is read
    result = wl.nginx_config()

    # THEN an empty string is returned
    assert result == ""


def test_catalogue_config_returns_content() -> None:
    """catalogue_config reads and parses the on-disk config.json."""
    # GIVEN a container with a pushed catalogue config
    expected: dict[str, object] = {"title": "Test", "apps": []}
    fake = FakeContainer(files={CONFIG_PATH: json.dumps(expected)})
    wl = _workload(fake)

    # WHEN the config is read
    result = wl.catalogue_config()

    # THEN the parsed content matches
    assert result == expected


def test_catalogue_config_returns_empty_on_missing() -> None:
    """catalogue_config returns {} when the file does not exist."""
    # GIVEN a container with no catalogue config
    fake = FakeContainer()
    wl = _workload(fake)

    # WHEN the config is read
    result = wl.catalogue_config()

    # THEN an empty dict is returned
    assert result == {}


def test_catalogue_config_returns_empty_on_corrupt_json() -> None:
    """catalogue_config returns {} for a corrupt config file.

    A corrupt cached file self-heals: {} compares unequal to intent,
    so the reconcile re-pushes it.
    """
    # GIVEN a container with a corrupt catalogue config
    fake = FakeContainer(files={CONFIG_PATH: "not-json{"})
    wl = _workload(fake)

    # WHEN the config is read
    result = wl.catalogue_config()

    # THEN an empty dict is returned, not a raised error
    assert result == {}


def test_catalogue_config_returns_empty_on_non_mapping_json() -> None:
    """catalogue_config returns {} for valid JSON that is not an object.

    A JSON list in config.json is as corrupt as invalid JSON: {}
    compares unequal to intent, so the reconcile re-pushes it.
    """
    # GIVEN a container whose config.json is a JSON list
    fake = FakeContainer(files={CONFIG_PATH: "[1, 2]"})
    wl = _workload(fake)

    # WHEN the config is read
    result = wl.catalogue_config()

    # THEN an empty dict is returned
    assert result == {}


def test_plan_services_returns_plan() -> None:
    """plan_services reads the running Pebble plan services."""
    # GIVEN a container with a plan
    fake = FakeContainer()
    # Pre-populate the plan via add_layer so get_plan returns it.
    fake.add_layer(
        "catalogue",
        {
            "services": {
                "catalogue": {
                    "override": "replace",
                    "summary": "catalogue",
                    "command": "nginx",
                    "startup": "enabled",
                }
            }
        },
    )
    wl = _workload(fake)

    # WHEN the layer services are read
    result = wl.plan_services()

    # THEN the services dict matches
    assert "catalogue" in result
    assert result["catalogue"]["command"] == "nginx"


def test_certs_true_when_all_exist() -> None:
    """Certs returns True when all three cert files exist."""
    # GIVEN a container with all three cert files
    fake = FakeContainer(
        files={
            CERT_PATH: "cert",
            KEY_PATH: "key",
            CA_CERT_PATH: "ca",
        }
    )
    wl = _workload(fake)

    # WHEN certs are checked
    result = wl.certs()

    # THEN all three are reported present
    assert result is True


def test_certs_false_when_one_missing() -> None:
    """Certs returns False when any cert file is missing."""
    # GIVEN a container with only two of three cert files
    fake = FakeContainer(
        files={
            CERT_PATH: "cert",
            KEY_PATH: "key",
            # CA_CERT_PATH is missing
        }
    )
    wl = _workload(fake)

    # WHEN certs are checked
    result = wl.certs()

    # THEN certs are reported absent
    assert result is False


# --- Effect tests ---


def test_push_writes_file() -> None:
    """Push writes a file to the container."""
    # GIVEN a container
    fake = FakeContainer()
    wl = _workload(fake)

    # WHEN a file is pushed
    wl.push("/tmp/test.txt", "hello world", make_dirs=True)

    # THEN the file is in the fake filesystem
    assert fake.files["/tmp/test.txt"] == "hello world"


def test_remove_path() -> None:
    """remove_path calls container.remove_path."""
    # GIVEN a container with a file
    fake = FakeContainer(files={"/tmp/to-remove.txt": "content"})
    wl = _workload(fake)

    # WHEN a path is removed
    wl.remove_path("/tmp/to-remove.txt", recursive=True)

    # THEN the file is gone
    assert "/tmp/to-remove.txt" not in fake.files


def test_ensure_layer_adds_and_autostarts() -> None:
    """ensure_layer adds a Pebble layer and autostarts services."""
    # GIVEN a container
    fake = FakeContainer()
    wl = _workload(fake)

    services: Mapping[str, Mapping[str, str]] = {
        "catalogue": {
            "override": "replace",
            "summary": "catalogue",
            "command": "nginx",
            "startup": "enabled",
        }
    }

    # WHEN the layer is ensured
    wl.ensure_layer("catalogue", services)

    # THEN add_layer and autostart were called
    assert len(fake.add_layer_calls) == 1
    assert fake.add_layer_calls[0][0] == "catalogue"
    assert len(fake.autostart_calls) == 1
    # AND the plan now contains the service
    assert "catalogue" in fake._plan_services
    assert fake._plan_services["catalogue"]["command"] == "nginx"


def test_restart_verifies_service_running() -> None:
    """Restart succeeds when the service comes up after restart."""
    # GIVEN a container with a running service
    fake = FakeContainer(services={"catalogue": ServiceStatus.ACTIVE})
    wl = _workload(fake)

    # WHEN the service is restarted
    # THEN no exception is raised (service is active)
    wl.restart("catalogue")
    assert fake.restart_calls == ["catalogue"]


def test_restart_raises_when_service_stays_down() -> None:
    """Restart raises ServiceNotRunningError when service stays down."""
    # GIVEN a container whose service is inactive after restart
    fake = FakeContainer(services={"catalogue": ServiceStatus.INACTIVE})
    wl = _workload(fake)

    # WHEN the service is restarted
    # THEN the charm refuses to believe the return value
    with pytest.raises(ServiceNotRunningError, match="catalogue"):
        wl.restart("catalogue")


# --- Exception guard test ---


def test_service_not_running_error_survives_raise() -> None:
    """ServiceNotRunningError survives raise/catch.

    This is the guard test required by AGENTS.md for every exception
    module — ops assigns __traceback__ as the event context unwinds,
    and a frozen exception would die with FrozenInstanceError.
    """
    # GIVEN a ServiceNotRunningError
    err = ServiceNotRunningError(service="test-svc", current="inactive")

    # WHEN it is raised and caught
    try:
        raise err
    except ServiceNotRunningError as caught:
        # THEN the exception survives the raise/catch cycle
        assert caught.service == "test-svc"
        assert caught.current == "inactive"
