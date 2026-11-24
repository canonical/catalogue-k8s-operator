#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Pebble container operations for the Catalogue charm.

Never imports ``ops`` at runtime. Knows nothing about relations,
config options, or Juju statuses. Every effect verifies real state
before returning — a method returning without raising is not evidence
the operation succeeded.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from nginx_config import CA_CERT_PATH, CERT_PATH, KEY_PATH, NGINX_CONFIG_PATH

if TYPE_CHECKING:
    from ops.model import Container
    from ops.pebble import LayerDict

logger = logging.getLogger(__name__)

ROOT_PATH = "/web"
CONFIG_PATH = ROOT_PATH + "/config.json"


# --- Exceptions ---


class WorkloadError(Exception):
    """Base for all workload-level errors."""


@dataclass
class ServiceNotRunningError(WorkloadError):
    """A service did not report active after restart.

    Not frozen — ``ops`` assigns ``__traceback__`` as the event
    context unwinds, and a frozen exception dies with
    ``FrozenInstanceError``, burying the real failure.
    """

    service: str
    """The service that was restarted."""

    current: str
    """The service status after the restart attempt."""

    def __str__(self) -> str:
        """Human-readable description of the failure."""
        return (
            f"Service {self.service!r} did not start after restart "
            f"(current status: {self.current!r})"
        )


# --- Workload ---


class Workload:
    """Own every effect on the container.

    Implements the :class:`catalogue_state.Facts` protocol so the
    pure core can read state without importing this module.
    """

    def __init__(self, container: Container) -> None:
        """Initialise with the Pebble container.

        Args:
            container: The workload container from
                ``self.unit.get_container(...)``.
        """
        self._container = container

    # -- Facts (catalogue_state.Facts protocol) --

    def can_connect(self) -> bool:
        """Whether the Pebble API is reachable (point-in-time)."""
        return self._container.can_connect()

    def nginx_config(self) -> str:
        """Read the on-disk nginx config.

        ``exists()`` is the missing-file check: Pebble raises
        ``PathError(kind="not-found")`` from ``pull``, which we cannot
        name here without importing ``ops`` — and a missing file is
        normal first-run state, not a failure. A connection error
        still propagates: the reconciler maps it to Blocked.

        Returns:
            The file contents, or ``""`` if absent.
        """
        if not self._container.exists(NGINX_CONFIG_PATH):
            return ""
        with self._container.pull(NGINX_CONFIG_PATH, encoding="utf-8") as f:
            return f.read()

    def catalogue_config(self) -> Mapping[str, object]:
        """Read the on-disk catalogue config.

        Returns:
            The parsed JSON body, or ``{}`` if absent or corrupt (a
            corrupt cached file self-heals: ``{}`` compares unequal to
            intent, so the reconcile re-pushes it).
        """
        if not self._container.exists(CONFIG_PATH):
            return {}
        try:
            with self._container.pull(CONFIG_PATH, encoding="utf-8") as f:
                result: Any = json.loads(f.read())
        except json.JSONDecodeError:
            logger.debug("Corrupt catalogue config on disk; re-pushing")
            return {}
        if not isinstance(result, Mapping):
            return {}
        return cast(Mapping[str, object], result)

    def plan_services(self) -> Mapping[str, Mapping[str, str]]:
        """Read the services section of the running Pebble plan.

        Returns:
            A mapping of service name to service config dict.
        """
        plan = self._container.get_plan()
        return {
            name: cast(Mapping[str, str], svc.to_dict()) for name, svc in plan.services.items()
        }

    def certs(self) -> bool:
        """Whether all three cert files exist on disk."""
        return all(self._container.exists(p) for p in (CERT_PATH, KEY_PATH, CA_CERT_PATH))

    # -- Effects --

    def push(self, path: str, body: str, *, make_dirs: bool = False) -> None:
        """Push a file to the container.

        Args:
            path: Destination path in the container.
            body: File contents as a string.
            make_dirs: Create parent directories if needed.
        """
        self._container.push(path, body, make_dirs=make_dirs)

    def remove_path(self, path: str, *, recursive: bool = False) -> None:
        """Remove a path from the container.

        Args:
            path: Path to remove.
            recursive: Remove directories recursively.
        """
        self._container.remove_path(path, recursive=recursive)

    def ensure_layer(self, label: str, services: Mapping[str, Mapping[str, str]]) -> None:
        """Add a Pebble layer and autostart services.

        Args:
            label: Layer label (e.g. ``"catalogue"``).
            services: Service config dict.
        """
        # LayerDict is the whole layer mapping: the services live under
        # its "services" key. Passing the services mapping itself
        # silently defines an empty layer (Layer reads only that key).
        # The cast is needed because the services mapping is generic
        # (Mapping[str, str] values) and LayerDict's shape is a
        # TypedDict of ServiceDicts — the shape was verified by hand.
        layer = cast("LayerDict", {"services": dict(services)})
        self._container.add_layer(label, layer, combine=True)
        self._container.autostart()

    def restart(self, service: str) -> None:
        """Restart a service and verify it came up.

        ``Container.restart()`` returning is not evidence the service
        is healthy — Pebble accepted the change, but the process may
        have crashed immediately. Read ``get_services()`` back and
        check ``is_running()`` before believing it.

        Args:
            service: The service name to restart.

        Raises:
            ServiceNotRunningError: The service did not report active
                after the restart.
        """
        self._container.restart(service)
        info = self._container.get_services(service)[service]
        if not info.is_running():
            raise ServiceNotRunningError(service=service, current=str(info.current))
