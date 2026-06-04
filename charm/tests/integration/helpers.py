# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import jubilant


def active_idle(status: jubilant.Status) -> bool:
    """Check if all apps are active and all agents are idle."""
    return jubilant.all_active(status) and jubilant.all_agents_idle(status)


def get_unit_address(juju: jubilant.Juju, app_name: str, unit_num: int) -> str:
    """Get the IP address of a unit."""
    status = juju.status()
    return status.apps[app_name].units[f"{app_name}/{unit_num}"].address
