#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
from pathlib import Path

import jubilant
import pytest
import requests
from helpers import active_idle, get_unit_address

logger = logging.getLogger(__name__)

APP_NAME = "catalogue-k8s"
SSC_APP_NAME = "ssc"
PROMETHEUS_APP_NAME = "prometheus-k8s"


@pytest.mark.juju_setup
def test_build_and_deploy(juju: jubilant.Juju, charm_path: Path, resources: dict):
    """Deploy the charm and wait for it to become active."""
    juju.deploy(charm_path, app=APP_NAME, resources=resources)
    juju.wait(active_idle, timeout=5 * 60)


def test_tls(juju: jubilant.Juju):
    """Test TLS integration with self-signed-certificates."""
    juju.deploy(
        "self-signed-certificates",
        app=SSC_APP_NAME,
        channel="1/edge",
        trust=True,
    )
    juju.integrate(f"{APP_NAME}:certificates", f"{SSC_APP_NAME}:certificates")
    juju.wait(active_idle, timeout=5 * 60)

    address = get_unit_address(juju, APP_NAME, 0)
    url = f"https://{address}/"
    response = requests.get(url, verify=False, timeout=10)
    assert response.status_code == 200


def test_app_integration(juju: jubilant.Juju):
    """Test that catalogue can integrate apps and display them in config.json."""
    juju.deploy(
        PROMETHEUS_APP_NAME,
        app=PROMETHEUS_APP_NAME,
        channel="dev/edge",
        trust=True,
    )
    juju.integrate(APP_NAME, PROMETHEUS_APP_NAME)
    juju.wait(active_idle, timeout=5 * 60)

    # retrieve the content of /web/config.json which holds application data in the catalogue
    new_config = juju.ssh(f"{APP_NAME}/0", "cat /web/config.json", container="catalogue")
    config_dict = json.loads(new_config)
    first_app_name = config_dict["apps"][0]["name"]
    assert first_app_name == "Prometheus"
