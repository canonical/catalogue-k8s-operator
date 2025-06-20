#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.


import logging
from pathlib import Path

import pytest
import requests
import sh
import yaml
from helpers import get_unit_address
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest, catalogue_charm):
    assert ops_test.model
    # Given a fresh build of the charm
    # When deploying it
    # Then it should eventually go idle/active
    resources = {"catalogue-image": METADATA["resources"]["catalogue-image"]["upstream-source"]}
    await ops_test.model.deploy(catalogue_charm, resources=resources, application_name="catalogue")

    # issuing fake update_status just to trigger an event
    async with ops_test.fast_forward():
        await ops_test.model.wait_for_idle(
            apps=["catalogue"],
            status="active",
            raise_on_blocked=True,
            timeout=1000,
        )

    assert ops_test.model.applications["catalogue"]
    assert ops_test.model.applications["catalogue"].units[0].workload_status == "active"


async def test_ingress(ops_test: OpsTest):
    assert ops_test.model
    sh.juju.deploy(  # type: ignore
        "traefik-k8s", "traefik", channel="latest/edge", trust=True, model=ops_test.model.name
    )
    sh.juju.relate("catalogue:ingress", "traefik", model=ops_test.model.name)  # type: ignore
    await ops_test.model.wait_for_idle(apps=["catalogue", "traefik"], status="active")

    address = await get_unit_address(ops_test, "traefik", 0)
    url = f"https://{address}/cos-catalogue"
    response = requests.get(url, verify=False)
    assert response.status_code == 200
