#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import jubilant
import pytest
import requests
import yaml
from helpers import get_unit_address

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]
TRAEFIK_APP_NAME = "traefik"
RESOURCES = {"catalogue-image": METADATA["resources"]["catalogue-image"]["upstream-source"]}


@pytest.mark.juju_setup
def test_build_and_deploy(juju: jubilant.Juju, charm_path: Path):
    """Deploy the charm and wait for it to become active."""
    juju.deploy(charm_path, app=APP_NAME, resources=RESOURCES)
    juju.wait(jubilant.all_active, timeout=1000)

    status = juju.status()
    assert status.apps[APP_NAME].units[f"{APP_NAME}/0"].workload_status.current == "active"


def test_ingress(juju: jubilant.Juju):
    """Test ingress integration with traefik."""
    juju.deploy("traefik-k8s", app=TRAEFIK_APP_NAME, channel="latest/edge", trust=True)
    juju.integrate(f"{APP_NAME}:ingress", TRAEFIK_APP_NAME)
    juju.wait(jubilant.all_active, timeout=600)

    address = get_unit_address(juju, TRAEFIK_APP_NAME, 0)
    url = f"http://{address}/{juju.model}-{APP_NAME}"
    response = requests.get(url, verify=False, timeout=10)
    assert response.status_code == 200
