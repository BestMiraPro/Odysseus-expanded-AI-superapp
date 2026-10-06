"""Regression: "remember this about me" reaches manage_memory.

Memory requests matched no intent domain, so a fresh agent chat took the
direct low-signal reply path, which sends no tools at all. The model replied
"noted in memory" and nothing was saved. The "memory" domain keeps these turns
on the normal tool path, where manage_memory is always available.
"""
import pytest

agent_loop = pytest.importorskip("src.agent_loop")


@pytest.mark.parametrize(
    "prompt",
    [
        "Remember this about me: I prefer metric units.",
        "remember that I'm vegetarian",
        "Please forget my old address",
        "I prefer short answers",
        "call me Dee from now on",
        "my name is Dinis",
        "show my memories",
        "what do you know about me?",
    ],
)
def test_memory_prompts_are_not_low_signal(prompt):
    intent = agent_loop._classify_agent_request([], prompt)
    assert intent["low_signal"] is False, intent
    assert "memory" in intent["domains"], intent


@pytest.mark.parametrize("prompt", ["hey there", "what is a memory leak?", "I remembered the keys"])
def test_unrelated_prompts_stay_out_of_memory_domain(prompt):
    intent = agent_loop._classify_agent_request([], prompt)
    assert "memory" not in intent["domains"], intent


def test_manage_memory_is_always_offered():
    from src.tool_index import ALWAYS_AVAILABLE

    assert "manage_memory" in ALWAYS_AVAILABLE
