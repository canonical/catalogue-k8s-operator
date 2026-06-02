# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging

import jubilant

logger = logging.getLogger(__name__)


def get_unit_address(juju: jubilant.Juju, app_name: str, unit_num: int) -> str:
    """Get the IP address of a unit."""
    status = juju.status()
    return status.apps[app_name].units[f"{app_name}/{unit_num}"].address


def run_juju_ssh_command(
    juju: jubilant.Juju, container_name: str, unit_name: str, command: str
) -> str:
    """Run a command via juju ssh."""
    task = juju.exec(command, unit=unit_name, container=container_name)
    return task.stdout
