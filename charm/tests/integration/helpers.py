# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

from pytest_operator.plugin import OpsTest
from subprocess import check_output, PIPE

logger = logging.getLogger(__name__)


async def get_unit_address(ops_test: OpsTest, app_name: str, unit_num: int) -> str:
    status = await ops_test.model.get_status()  # noqa: F821
    return status["applications"][app_name]["units"][f"{app_name}/{unit_num}"]["address"]


async def run_juju_ssh_command(model_full_name: str, container_name: str, unit_name: str, command: str):
    result = check_output(
        f"JUJU_MODEL={model_full_name} juju ssh --container {container_name} {unit_name} '{command}' ",
        stderr=PIPE,
        shell=True,
        universal_newlines=True,
    )
    return result
