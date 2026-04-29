# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the logging (LogForwarder) integration."""

import logging
from pathlib import Path

import pytest
import requests
import yaml
from pytest_operator.plugin import OpsTest
from tenacity import retry, stop_after_attempt, wait_exponential

from helpers import get_unit_address

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]
LOKI_APP_NAME = "loki-k8s"


@retry(wait=wait_exponential(multiplier=2, min=5, max=30), stop=stop_after_attempt(10), reraise=True)
async def assert_logs_found_in_loki(loki_url: str, app_name: str):
    """Query Loki and assert that logs from the catalogue application are present."""
    query = f'{{juju_application="{app_name}"}}'
    response = requests.get(
        f"{loki_url}/loki/api/v1/query_range",
        params={"query": query, "limit": 10},
        timeout=10,
    )
    response.raise_for_status()

    data = response.json()
    result = data.get("data", {}).get("result", [])
    assert len(result) > 0, f"No log streams found in Loki for application '{app_name}'"

    log_lines = [line for stream in result for line in stream.get("values", [])]
    assert len(log_lines) > 0, f"No log entries found in Loki for application '{app_name}'"
    logger.info("Found %d log lines in Loki for application '%s'", len(log_lines), app_name)


@pytest.mark.abort_on_fail
async def test_logging_integration(ops_test: OpsTest, catalogue_charm):
    """Deploy catalogue and loki-k8s, and verify the logging integration."""
    assert ops_test.model

    resources = {"catalogue-image": METADATA["resources"]["catalogue-image"]["upstream-source"]}
    await ops_test.model.deploy(catalogue_charm, resources=resources, application_name=APP_NAME)

    await ops_test.model.deploy(
        LOKI_APP_NAME,
        application_name=LOKI_APP_NAME,
        channel="dev/edge",
        trust=True,
    )

    await ops_test.model.wait_for_idle(
        apps=[APP_NAME],
        status="active",
        raise_on_blocked=True,
        timeout=1000,
    )

    await ops_test.model.integrate(f"{APP_NAME}:logging", f"{LOKI_APP_NAME}:logging")
    await ops_test.model.wait_for_idle(
        apps=[APP_NAME, LOKI_APP_NAME],
        status="active",
        raise_on_error=False,
        timeout=1000,
    )

    # Verify the relation is established
    assert ops_test.model.applications[APP_NAME].units[0].workload_status == "active"

    # Query Loki to verify catalogue logs are being forwarded
    loki_address = await get_unit_address(ops_test, LOKI_APP_NAME, 0)
    loki_url = f"http://{loki_address}:3100"
    await assert_logs_found_in_loki(loki_url, APP_NAME)
