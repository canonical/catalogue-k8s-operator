# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the logging (LogForwarder) integration."""

import logging
from pathlib import Path

import pytest
import yaml
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]
LOKI_APP_NAME = "loki-k8s"


@pytest.mark.abort_on_fail
async def test_logging_integration(ops_test: OpsTest, catalogue_charm):
    """Deploy catalogue and loki-k8s, and verify the logging integration."""
    assert ops_test.model

    resources = {"catalogue-image": METADATA["resources"]["catalogue-image"]["upstream-source"]}
    await ops_test.model.deploy(catalogue_charm, resources=resources, application_name=APP_NAME)

    await ops_test.model.deploy(
        LOKI_APP_NAME,
        application_name=LOKI_APP_NAME,
        channel="1/stable",
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
