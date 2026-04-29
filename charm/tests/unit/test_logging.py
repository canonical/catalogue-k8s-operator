# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the logging (LogForwarder) integration."""

import logging

from ops.testing import Container, Context, Relation, State

from charm import CatalogueCharm

logger = logging.getLogger(__name__)


def test_charm_initializes_with_logging_relation():
    """Test that the charm initializes correctly with a logging relation."""
    context = Context(CatalogueCharm)

    container = Container(name="catalogue", can_connect=True)
    logging_relation = Relation(endpoint="logging")

    state = State(
        leader=True,
        containers=[container],
        relations=[logging_relation],
    )

    state_out = context.run(context.on.config_changed(), state)
    assert state_out is not None


def test_charm_initializes_without_logging_relation():
    """Test that the charm initializes correctly without a logging relation."""
    context = Context(CatalogueCharm)

    container = Container(name="catalogue", can_connect=True)

    state = State(
        leader=True,
        containers=[container],
    )

    state_out = context.run(context.on.config_changed(), state)
    assert state_out is not None
