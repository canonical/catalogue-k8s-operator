# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
import os
from pathlib import Path

import pytest
import sh
import yaml

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def charm_path() -> Path:
    """Charm used for integration testing.

    Set CHARM_PATH env var to use a pre-built charm, otherwise pack it.
    """
    if charm_file := os.environ.get("CHARM_PATH"):
        return Path(charm_file).resolve()

    sh.charmcraft.pack()  # type: ignore[attr-defined]
    charms = sorted(Path(".").glob("*.charm"))
    assert charms, "No .charm file found after 'charmcraft pack'"
    return charms[-1].resolve()


@pytest.fixture(scope="module")
def resources() -> dict:
    """Resources for the charm."""
    metadata = yaml.safe_load(Path("./charmcraft.yaml").read_text())
    return {"catalogue-image": metadata["resources"]["catalogue-image"]["upstream-source"]}
