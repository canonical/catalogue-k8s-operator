#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the logging (LogForwarder) integration."""

import logging
from pathlib import Path

import jubilant
import pytest
import requests
import yaml
from helpers import get_unit_address
from tenacity import retry, stop_after_attempt, wait_fixed

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]
LOKI_APP_NAME = "loki"
RESOURCES = {"catalogue-image": METADATA["resources"]["catalogue-image"]["upstream-source"]}


@pytest.mark.juju_setup
def test_logging_integration(juju: jubilant.Juju, charm_path: Path):
    """Deploy catalogue and loki, integrate via logging, and verify the relation is active."""
    juju.deploy(
        charm_path,
        app=APP_NAME,
        resources=RESOURCES,
        trust=True,
    )
    juju.deploy(charm="loki-k8s", app=LOKI_APP_NAME, channel="dev/edge", trust=True)

    juju.integrate(f"{APP_NAME}:logging", f"{LOKI_APP_NAME}:logging")

    juju.wait(jubilant.all_active, delay=10, timeout=600)


@retry(wait=wait_fixed(15), stop=stop_after_attempt(20))
def test_logs_are_forwarded_to_loki(juju: jubilant.Juju):
    """Verify that catalogue logs are present in Loki."""
    # Generate some traffic to produce access logs
    catalogue_address = get_unit_address(juju, APP_NAME, 0)
    requests.get(f"http://{catalogue_address}/", timeout=10)

    # Query Loki for catalogue logs
    loki_address = get_unit_address(juju, LOKI_APP_NAME, 0)
    url = f"http://{loki_address}:3100/loki/api/v1/query_range"
    response = requests.get(url, params={"query": f'{{juju_application="{APP_NAME}"}}'}, timeout=10)
    response.raise_for_status()

    result = response.json().get("data", {}).get("result", [])
    assert len(result) > 0, f"No log entries found in Loki for {APP_NAME}"
