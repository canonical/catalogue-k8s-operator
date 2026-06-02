#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
from pathlib import Path

import jubilant
import pytest
import requests
import yaml
from helpers import get_unit_address, run_juju_ssh_command

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]
RESOURCES = {"catalogue-image": METADATA["resources"]["catalogue-image"]["upstream-source"]}

SSC_APP_NAME = "ssc"
PROMETHEUS_APP_NAME = "prometheus-k8s"


@pytest.mark.juju_setup
def test_build_and_deploy(juju: jubilant.Juju, charm_path: Path):
    """Deploy the charm and wait for it to become active."""
    juju.deploy(charm_path, app=APP_NAME, resources=RESOURCES)
    juju.wait(jubilant.all_active, timeout=1000)

    status = juju.status()
    assert status.apps[APP_NAME].units[f"{APP_NAME}/0"].workload_status.current == "active"


def test_tls(juju: jubilant.Juju):
    """Test TLS integration with self-signed-certificates."""
    juju.deploy(
        "self-signed-certificates",
        app=SSC_APP_NAME,
        channel="1/edge",
        trust=True,
    )
    juju.integrate(APP_NAME, SSC_APP_NAME)
    juju.wait(jubilant.all_active, timeout=600)

    address = get_unit_address(juju, APP_NAME, 0)
    url = f"https://{address}/"
    response = requests.get(url, verify=False, timeout=10)
    assert response.status_code == 200


def test_app_integration(juju: jubilant.Juju):
    """Test that catalogue can integrate apps and display them in config.json."""
    juju.deploy(
        PROMETHEUS_APP_NAME,
        app=PROMETHEUS_APP_NAME,
        channel="1/stable",
        trust=True,
    )
    juju.integrate(APP_NAME, PROMETHEUS_APP_NAME)
    juju.wait(jubilant.all_active, timeout=1000)

    # retrieve the content of /web/config.json which holds application data in the catalogue
    new_config = run_juju_ssh_command(
        juju=juju,
        container_name="catalogue",
        unit_name=f"{APP_NAME}/0",
        command="cat /web/config.json",
    )
    config_dict = json.loads(new_config)
    first_app_name = config_dict["apps"][0]["name"]
    assert first_app_name == "Prometheus"
