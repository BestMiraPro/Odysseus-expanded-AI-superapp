"""Omnigent agent store + config compilation tests."""

from __future__ import annotations


def test_create_list_get_are_owner_scoped(tmp_path):
    from src.omnigent_agents import OmnigentAgentStore

    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    agent = store.create_agent(
        owner="alice", name="Researcher",
        role="Find context.", backend="api",
        model={"endpoint_id": "ep1", "model": "kimi-k2.6"},
    )
    assert agent["owner"] == "alice"
    assert agent["backend"] == "api"
    assert agent["enabled"] is True
    assert agent["id"].startswith("agent-")
    assert store.list_agents("bob") == []
    assert store.list_agents("alice")[0]["id"] == agent["id"]
    assert store.get_agent(agent["id"], owner="bob") is None
    assert store.get_agent(agent["id"], owner="alice")["name"] == "Researcher"


def test_update_and_delete_owner_scoped(tmp_path):
    from src.omnigent_agents import OmnigentAgentStore

    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    agent = store.create_agent(owner="alice", name="Coder", role="Build.", backend="claude-subscription")

    updated = store.update_agent(agent["id"], owner="alice", name="Coder 2", enabled=False)
    assert updated["name"] == "Coder 2"
    assert updated["enabled"] is False
    assert store.update_agent(agent["id"], owner="bob", name="x") is None

    assert store.delete_agent(agent["id"], owner="bob") is False
    assert store.delete_agent(agent["id"], owner="alice") is True
    assert store.list_agents("alice") == []


def test_create_validates_name_and_backend(tmp_path):
    from src.omnigent_agents import OmnigentAgentStore

    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    for bad in (lambda: store.create_agent(owner="a", name=" ", role="", backend="api"),
                lambda: store.create_agent(owner="a", name="X", role="", backend="bogus")):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_orchestrator_defaults_and_set_owner_scoped(tmp_path):
    from src.omnigent_agents import OmnigentAgentStore

    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    # default when never set
    assert store.get_orchestrator("alice") == {
        "backend": "claude-subscription", "model": None, "workspace": ""
    }
    saved = store.set_orchestrator("alice", backend="api",
                                   model={"endpoint_id": "ep1", "model": "gpt-x"},
                                   workspace="/proj")
    assert saved["backend"] == "api"
    assert saved["model"]["model"] == "gpt-x"
    assert saved["workspace"] == "/proj"
    assert store.get_orchestrator("alice")["backend"] == "api"
    assert store.get_orchestrator("bob")["backend"] == "claude-subscription"


def test_set_orchestrator_rejects_bad_backend(tmp_path):
    from src.omnigent_agents import OmnigentAgentStore

    store = OmnigentAgentStore(state_path=tmp_path / "agents.json")
    try:
        store.set_orchestrator("alice", backend="nope")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_compile_config_emits_valid_omnigent_spec():
    import yaml
    from src.omnigent_agents import compile_config

    agents = [
        {"name": "Researcher", "role": "Find context.", "backend": "api",
         "model": {"endpoint_id": "ep1", "model": "kimi-k2.6"}, "enabled": True},
        {"name": "Coder", "role": "Build it.", "backend": "claude-subscription",
         "model": None, "enabled": True},
        {"name": "Disabled", "role": "x", "backend": "api", "model": None, "enabled": False},
    ]
    orchestrator = {"backend": "claude-subscription", "model": None, "workspace": "/work"}

    text = compile_config(agents, orchestrator)
    spec = yaml.safe_load(text)

    assert spec["spec_version"] == 1
    assert spec["executor"]["config"]["harness"] == "claude-sdk"
    assert spec["workspace"] == "/work"
    names = [a["name"] for a in spec["agents"]]
    assert names == ["Researcher", "Coder"]               # disabled excluded
    assert spec["agents"][0]["harness"] == "claude-sdk"
    assert spec["agents"][0]["model"] == "kimi-k2.6"
    # canonical Odysseus tools are reused
    assert "odysseus_capabilities" in spec["tools"]


def test_compile_chatgpt_orchestrator_uses_codex_harness():
    import yaml
    from src.omnigent_agents import compile_config

    text = compile_config([], {"backend": "chatgpt-subscription", "model": None, "workspace": ""})
    spec = yaml.safe_load(text)
    assert spec["executor"]["config"]["harness"] == "codex"
    assert spec["agents"] == []
