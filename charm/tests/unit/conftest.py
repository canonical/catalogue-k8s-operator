# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for Catalogue unit tests."""

import pytest
from ops import testing

from charm import CatalogueCharm


@pytest.fixture
def ctx() -> testing.Context[CatalogueCharm]:
    """A Scenario Context for the Catalogue charm."""
    return testing.Context(CatalogueCharm)


@pytest.fixture
def base_state() -> testing.State:
    """A leader unit on Kubernetes, catalogue container connected."""
    return testing.State(
        leader=True,
        containers=[testing.Container(name="catalogue", can_connect=True)],
    )
