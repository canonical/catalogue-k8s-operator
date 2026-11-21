#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for workload.py using mock containers (constructor seam)."""

import json
from collections.abc import Mapping
from unittest.mock import MagicMock

import pytest
from ops.pebble import PathError, ServiceInfo, ServiceStatus

from nginx_config import CA_CERT_PATH, CERT_PATH, KEY_PATH, NGINX_CONFIG_PATH
from workload import ServiceNotRunningError, Workload

CONTAINER_NAME = "catalogue"
ROOT_PATH = "/web"
CONFIG_PATH = ROOT_PATH + "/config.json"


# --- Helpers ---


def _make_container(
    can_connect: bool = True,
    files: dict[str, str] | None = None,
    services: Mapping[str, ServiceInfo] | None = None,
) -> MagicMock:
    """Build a mock Container with configurable state."""
    mock = MagicMock()
    mock.can_connect.return_value = can_connect
    mock.name = CONTAINER_NAME

    # File operations
    stored_files: dict[str, str] = dict(files) if files else {}

    def _push(path: str, source: str, *, make_dirs: bool = False) -> None:
        stored_files[path] = source

    def _pull(path: str, *, encoding: str = "utf-8") -> MagicMock:
        if path not in stored_files:
            # Pebble raises PathError("not-found") from pull, not
            # the builtin FileNotFoundError — the mock lies like
            # the real API.
            raise PathError("not-found", f"stat {path}: no such file or directory")
        m = MagicMock()
        m.read.return_value = stored_files[path]
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=None)
        return m

    def _exists(path: str) -> bool:
        return path in stored_files

    mock.push.side_effect = _push
    mock.pull.side_effect = _pull
    mock.exists.side_effect = _exists
    mock.remove_path = MagicMock()

    # Service operations
    _services: dict[str, ServiceInfo] = {}
    if services:
        _services = dict(services)

    def _get_services(*names: str) -> dict[str, ServiceInfo]:
        if not names:
            return dict(_services)
        return {n: _services[n] for n in names if n in _services}

    mock.get_services.side_effect = _get_services
    mock.restart = MagicMock()
    mock.add_layer = MagicMock()
    mock.autostart = MagicMock()
    mock.get_plan = MagicMock()

    return mock


def _make_service_info(
    name: str = "catalogue",
    current: ServiceStatus = ServiceStatus.ACTIVE,
) -> ServiceInfo:
    """Build a ServiceInfo with the given current status."""
    info = MagicMock(spec=ServiceInfo)
    info.is_running.return_value = current == ServiceStatus.ACTIVE
    info.current = current
    info.name = name
    return info


# --- Facts tests ---


def test_can_connect_true() -> None:
    """can_connect returns True when the container is reachable."""
    # GIVEN a connected container
    container = _make_container(can_connect=True)
    wl = Workload(container)

    # WHEN can_connect is called
    result = wl.can_connect()

    # THEN it returns True
    assert result is True


def test_can_connect_false() -> None:
    """can_connect returns False when the container is not reachable."""
    # GIVEN a disconnected container
    container = _make_container(can_connect=False)
    wl = Workload(container)

    # WHEN can_connect is called
    result = wl.can_connect()

    # THEN it returns False
    assert result is False


def test_nginx_config_returns_content() -> None:
    """nginx_config reads the on-disk nginx config."""
    # GIVEN a container with a pushed nginx config
    container = _make_container(files={NGINX_CONFIG_PATH: "test-nginx-config"})
    wl = Workload(container)

    # WHEN the config is read
    result = wl.nginx_config()

    # THEN the content matches
    assert result == "test-nginx-config"


def test_nginx_config_returns_empty_on_missing() -> None:
    """nginx_config returns '' when the file does not exist."""
    # GIVEN a container with no nginx config
    container = _make_container()
    wl = Workload(container)

    # WHEN the config is read
    result = wl.nginx_config()

    # THEN an empty string is returned
    assert result == ""


def test_catalogue_config_returns_content() -> None:
    """catalogue_config reads and parses the on-disk config.json."""
    # GIVEN a container with a pushed catalogue config
    expected: dict[str, object] = {"title": "Test", "apps": []}
    container = _make_container(files={CONFIG_PATH: json.dumps(expected)})
    wl = Workload(container)

    # WHEN the config is read
    result = wl.catalogue_config()

    # THEN the parsed content matches
    assert result == expected


def test_catalogue_config_returns_empty_on_missing() -> None:
    """catalogue_config returns {} when the file does not exist."""
    # GIVEN a container with no catalogue config
    container = _make_container()
    wl = Workload(container)

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
    container = _make_container(files={CONFIG_PATH: "not-json{"})
    wl = Workload(container)

    # WHEN the config is read
    result = wl.catalogue_config()

    # THEN an empty dict is returned, not a raised error
    assert result == {}


def test_layer_services_returns_plan() -> None:
    """layer_services reads the running Pebble plan services."""
    # GIVEN a container with a plan
    container = _make_container()
    plan_mock = MagicMock()
    svc_mock = MagicMock()
    svc_mock.to_dict.return_value = {
        "override": "replace",
        "summary": "catalogue",
        "command": "nginx",
        "startup": "enabled",
    }
    plan_mock.services = {"catalogue": svc_mock}
    container.get_plan.return_value = plan_mock
    wl = Workload(container)

    # WHEN the layer services are read
    result = wl.layer_services()

    # THEN the services dict matches
    assert "catalogue" in result
    assert result["catalogue"]["command"] == "nginx"


def test_certs_true_when_all_exist() -> None:
    """Certs returns True when all three cert files exist."""
    # GIVEN a container with all three cert files
    container = _make_container(
        files={
            CERT_PATH: "cert",
            KEY_PATH: "key",
            CA_CERT_PATH: "ca",
        }
    )
    wl = Workload(container)

    # WHEN certs are checked
    result = wl.certs()

    # THEN all three are reported present
    assert result is True


def test_certs_false_when_one_missing() -> None:
    """Certs returns False when any cert file is missing."""
    # GIVEN a container with only two of three cert files
    container = _make_container(
        files={
            CERT_PATH: "cert",
            KEY_PATH: "key",
            # CA_CERT_PATH is missing
        }
    )
    wl = Workload(container)

    # WHEN certs are checked
    result = wl.certs()

    # THEN certs are reported absent
    assert result is False


# --- Effect tests ---


def test_push_writes_file() -> None:
    """Push writes a file to the container."""
    # GIVEN a container
    container = _make_container()
    wl = Workload(container)

    # WHEN a file is pushed
    wl.push("/tmp/test.txt", "hello world", make_dirs=True)

    # THEN push was called with the right arguments
    container.push.assert_called_once_with("/tmp/test.txt", "hello world", make_dirs=True)


def test_remove_path() -> None:
    """remove_path calls container.remove_path."""
    # GIVEN a container
    container = _make_container()
    wl = Workload(container)

    # WHEN a path is removed
    wl.remove_path("/tmp/to-remove.txt", recursive=True)

    # THEN remove_path was called
    container.remove_path.assert_called_once_with("/tmp/to-remove.txt", recursive=True)


def test_ensure_layer_adds_and_autostarts() -> None:
    """ensure_layer adds a Pebble layer and autostarts services."""
    # GIVEN a container
    container = _make_container()
    wl = Workload(container)

    services = {
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
    container.add_layer.assert_called_once()
    container.autostart.assert_called_once()


def test_restart_verifies_service_running() -> None:
    """Restart succeeds when the service comes up after restart."""
    # GIVEN a container with a running service
    svc = _make_service_info("catalogue", ServiceStatus.ACTIVE)
    container = _make_container(services={"catalogue": svc})
    wl = Workload(container)

    # WHEN the service is restarted
    # THEN no exception is raised (service is active)
    wl.restart("catalogue")
    container.restart.assert_called_once_with("catalogue")


def test_restart_raises_when_service_stays_down() -> None:
    """Restart raises ServiceNotRunningError when service stays down."""
    # GIVEN a container whose service is inactive after restart
    svc = _make_service_info("catalogue", ServiceStatus.INACTIVE)
    container = _make_container(services={"catalogue": svc})
    wl = Workload(container)

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
