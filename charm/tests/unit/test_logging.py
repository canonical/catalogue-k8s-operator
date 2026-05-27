# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the logging (LogForwarder) integration."""

from ops.testing import Container, Context, Relation, State

from charm import CatalogueCharm


def test_charm_initializes_with_logging_relation():
    """The charm should initialize without errors when a logging relation is present."""
    context = Context(CatalogueCharm)

    container = Container(name="catalogue", can_connect=True)
    logging_relation = Relation(endpoint="logging", remote_app_name="loki")

    state = State(
        leader=True,
        containers=[container],
        relations=[logging_relation],
    )

    # WHEN a relation-changed event fires on the logging relation
    state_out = context.run(context.on.relation_changed(logging_relation), state)

    # THEN no exceptions are raised (charm handles the relation gracefully)
    assert state_out is not None


def test_charm_starts_without_logging_relation():
    """The charm should start fine without the optional logging relation."""
    context = Context(CatalogueCharm)

    container = Container(name="catalogue", can_connect=True)

    state = State(
        leader=True,
        containers=[container],
        relations=[],
    )

    # WHEN a pebble-ready event fires
    state_out = context.run(context.on.pebble_ready(container), state)

    # THEN no exceptions are raised
    assert state_out is not None
