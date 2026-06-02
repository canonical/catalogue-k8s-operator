#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path

import jubilant
import pytest
import requests
from helpers import get_unit_address

logger = logging.getLogger(__name__)

APP_NAME = "catalogue-k8s"
TRAEFIK_APP_NAME = "traefik"


def active_idle(status: jubilant.Status) -> bool:
    """Check if all apps are active and all agents are idle."""
    return jubilant.all_active(status) and jubilant.all_agents_idle(status)


@pytest.mark.juju_setup
def test_build_and_deploy(juju: jubilant.Juju, charm_path: Path, resources: dict):
    """Deploy the charm and wait for it to become active."""
    juju.deploy(charm_path, app=APP_NAME, resources=resources)
    juju.wait(active_idle, timeout=5 * 60)


def test_ingress(juju: jubilant.Juju):
    """Test ingress integration with traefik."""
    juju.deploy("traefik-k8s", app=TRAEFIK_APP_NAME, channel="latest/edge", trust=True)
    juju.integrate(f"{APP_NAME}:ingress", TRAEFIK_APP_NAME)
    juju.wait(active_idle, timeout=5 * 60)

    address = get_unit_address(juju, TRAEFIK_APP_NAME, 0)
    url = f"http://{address}/{juju.model}-{APP_NAME}"
    response = requests.get(url, verify=False, timeout=10)
    assert response.status_code == 200
