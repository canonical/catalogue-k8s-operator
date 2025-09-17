
import json
import logging

from ops.testing import Container, Context, Relation, State

from charm import CatalogueCharm

logger = logging.getLogger(__name__)

def test_override_hostname():
    override_hostname = "foobar"
    initial_url = "https://localhost/remote_charm"
    overridden_url = f"https://{override_hostname}/remote_charm"

    context = Context(CatalogueCharm)

    container_name = "catalogue"
    container = Container(name=container_name, can_connect=True)

    remote_app_data = {
                "name": "remote-charm",
                "url": initial_url,
                "icon": "some-cool-icon",
                "description": "Remote charm description"
            }
    relation = Relation(remote_app_name="remote-charm", endpoint="catalogue", remote_app_data=remote_app_data)

    state = State(
        leader=True,
        containers=[container],
        relations=[relation],
    )

    state_out = context.run(context.on.config_changed(), state)

    container_fs = state_out.get_container(container_name).get_filesystem(context)
    cfg_file = container_fs / "web" / "config.json"

    config = json.loads(cfg_file.read_text())
    assert config['apps'][0]['url'] == initial_url

    state = State(
        leader=True,
        containers=[container],
        relations=[relation],
        config={"override_hostname": override_hostname}
    )

    state_out = context.run(context.on.config_changed(), state)

    container_fs = state_out.get_container(container_name).get_filesystem(context)
    cfg_file = container_fs / "web" / "config.json"

    config = json.loads(cfg_file.read_text())
    assert config['apps'][0]['url'] == overridden_url

