#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the logging (LogForwarder) integration."""

import logging
from pathlib import Path

import jubilant
import pytest
import requests
from helpers import active_idle, get_unit_address
from tenacity import retry, stop_after_attempt, wait_fixed

logger = logging.getLogger(__name__)

APP_NAME = "catalogue-k8s"
LOKI_APP_NAME = "loki"


@pytest.mark.juju_setup
def test_logging_integration(juju: jubilant.Juju, charm_path: Path, resources: dict):
    """Deploy catalogue and loki, integrate via logging, and verify the relation is active."""
    juju.deploy(
        charm_path,
        app=APP_NAME,
        resources=resources,
        trust=True,
    )
    juju.deploy(charm="loki-k8s", app=LOKI_APP_NAME, channel="dev/edge", trust=True)
    juju.integrate(f"{APP_NAME}:logging", f"{LOKI_APP_NAME}:logging")
    juju.wait(active_idle, delay=10, timeout=5 * 60)


@retry(wait=wait_fixed(15), stop=stop_after_attempt(20))
def test_logs_are_forwarded_to_loki(juju: jubilant.Juju):
    """Verify that catalogue logs are present in Loki."""
    # Generate some traffic to produce access logs
    catalogue_address = get_unit_address(juju, APP_NAME, 0)
    requests.get(f"http://{catalogue_address}/", timeout=10)

    # Query Loki for catalogue logs
    loki_address = get_unit_address(juju, LOKI_APP_NAME, 0)
    url = f"http://{loki_address}:3100/loki/api/v1/query_range"
    response = requests.get(
        url, params={"query": f'{{juju_application="{APP_NAME}"}}'}, timeout=10
    )
    response.raise_for_status()

    result = response.json().get("data", {}).get("result", [])
    assert len(result) > 0, f"No log entries found in Loki for {APP_NAME}"
