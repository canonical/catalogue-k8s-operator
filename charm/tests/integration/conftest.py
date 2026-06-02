# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
import os
from pathlib import Path

import jubilant
import pytest

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def charm_path() -> Path:
    """Charm used for integration testing.

    Set CHARM_PATH env var to use a pre-built charm, otherwise pack it.
    """
    if charm_file := os.environ.get("CHARM_PATH"):
        return Path(charm_file)

    return jubilant.pack(".")


@pytest.fixture(scope="module")
def juju(juju: jubilant.Juju) -> jubilant.Juju:
    """Re-export the juju fixture from pytest-jubilant."""
    return juju
