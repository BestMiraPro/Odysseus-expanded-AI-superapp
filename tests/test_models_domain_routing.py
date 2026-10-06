"""Regression: model-delegation requests reach list_models / chat_with_model.

"Call list_models, then use chat_with_model to ask local-sage-8b ..." used to
match no intent domain, so the agent took the direct low-signal reply path and
the delegation tools were never advertised. The "models" domain seeds them.
"""
import pytest

agent_loop = pytest.importorskip("src.agent_loop")


@pytest.mark.parametrize(
    "prompt",
    [
        "Call list_models, then use chat_with_model to ask local-sage-8b 'What is 6 times 7?'",
        "get a second opinion from another model on this",
        "ask gpt what it thinks about my plan",
        "which models are available to you?",
        "delegate the summary to a cheaper model",
    ],
)
def test_delegation_prompts_select_models_domain(prompt):
    intent = agent_loop._classify_agent_request([], prompt)
    assert intent["low_signal"] is False, intent
    assert "models" in intent["domains"], intent
    tools = agent_loop._DOMAIN_TOOL_MAP["models"]
    assert {"list_models", "chat_with_model"} <= tools


@pytest.mark.parametrize("prompt", ["hey there, how are you", "what is a model in economics"])
def test_unrelated_prompts_stay_out_of_models_domain(prompt):
    intent = agent_loop._classify_agent_request([], prompt)
    assert "models" not in intent["domains"], intent


def test_models_domain_has_a_rule_pack():
    rules = agent_loop._domain_rules_for_tools({"chat_with_model"})
    assert any("chat_with_model" in r and "list_models" in r for r in rules), rules


def test_empty_list_models_fence_is_a_call():
    from src.agent_tools import parse_tool_blocks

    blocks = parse_tool_blocks("```list_models\n```\n\nI'll get the list first.")
    assert [(b.tool_type, b.content) for b in blocks] == [("list_models", "")]
    assert parse_tool_blocks("```bash\n```") == []
