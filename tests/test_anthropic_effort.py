"""Tests for Anthropic effort-parameter gating (#831).

Haiku before 5.5 and Sonnet 4.5 reject the ``effort`` parameter with a
400. Opus 4.5+, Sonnet 4.6+ and Haiku 5.5+ accept it. The gate uses a per-family
minimum version so future ``claude-{opus,sonnet}-X-Y`` releases inherit
support automatically.
"""

import pytest

from tradingagents.llm_clients import anthropic_client as mod


def _capture_kwargs(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        mod, "NormalizedChatAnthropic",
        lambda **kwargs: captured.setdefault("kwargs", kwargs),
    )
    return captured


@pytest.mark.unit
class TestEffortGate:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-haiku-4-5", "claude-haiku-5-0", "claude-haiku-4-7-preview",
            # Sonnet 4.5 (and earlier) 400 on effort — only Sonnet 4.6+ supports it.
            "claude-sonnet-4-5", "claude-sonnet-4-0",
            # A dated snapshot's date is not its minor version (#1585).
            "claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-sonnet-4-5-20250929",
        ],
    )
    def test_unsupported_models_do_not_receive_effort(self, monkeypatch, model):
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(model=model, effort="medium", api_key="x").get_llm()
        assert "effort" not in captured["kwargs"]

    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7",
            "claude-sonnet-4-6", "claude-opus-4-5-20251101",
            # Haiku takes effort from 5.5 (default medium).
            "claude-haiku-5-5",
        ],
    )
    def test_current_opus_and_sonnet_receive_effort(self, monkeypatch, model):
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(model=model, effort="high", api_key="x").get_llm()
        assert captured["kwargs"]["effort"] == "high"

    @pytest.mark.parametrize(
        "model",
        ["claude-opus-5-0", "claude-opus-4-8", "claude-sonnet-5-0"],
    )
    def test_future_opus_sonnet_inherit_effort_via_pattern(self, monkeypatch, model):
        """Forward-compat: new Opus/Sonnet versions don't need a code change."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(model=model, effort="low", api_key="x").get_llm()
        assert captured["kwargs"]["effort"] == "low"

    @pytest.mark.parametrize(
        "model",
        # Claude 5 family uses single-number version IDs; all are effort-capable.
        ["claude-sonnet-5", "claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-5-5",
         "claude-fable-5-1"],
    )
    def test_claude_5_family_receives_effort(self, monkeypatch, model):
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(model=model, effort="high", api_key="x").get_llm()
        assert captured["kwargs"]["effort"] == "high"

    def test_mythos_preview_receives_effort(self, monkeypatch):
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(
            model="claude-mythos-preview", effort="medium", api_key="x"
        ).get_llm()
        assert captured["kwargs"]["effort"] == "medium"

    def test_unknown_anthropic_model_does_not_receive_effort(self, monkeypatch):
        """Default is conservative — unknown models don't get effort to avoid 400s."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(
            model="claude-experimental-x", effort="medium", api_key="x"
        ).get_llm()
        assert "effort" not in captured["kwargs"]

    def test_other_kwargs_still_forwarded_when_effort_skipped(self, monkeypatch):
        """Skipping effort must not break other passthrough kwargs."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(
            model="claude-haiku-4-5",
            effort="medium",
            api_key="placeholder",
            max_tokens=1024,
            timeout=30,
        ).get_llm()
        assert captured["kwargs"]["api_key"] == "placeholder"
        assert captured["kwargs"]["max_tokens"] == 1024
        assert captured["kwargs"]["timeout"] == 30
        assert "effort" not in captured["kwargs"]


@pytest.mark.unit
@pytest.mark.parametrize("model, method", [
    ("claude-sonnet-5-5", "json_schema"), ("claude-opus-5-5", "json_schema"),
    ("claude-haiku-5-5", "json_schema"), ("claude-fable-5-1", "json_schema"),
    ("claude-sonnet-4-6", "function_calling"), ("claude-haiku-4-5", "function_calling"),
])
def test_claude_5_models_get_native_structured_output(monkeypatch, model, method):
    """Forced tool calls are refused by Sonnet/Opus 5.5 and Fable 5.1; a miss costs a
    second call, so Claude 5 models use the API's JSON-schema output instead."""
    seen = {}
    monkeypatch.setattr(mod.ChatAnthropic, "with_structured_output",
                        lambda self, schema, *, method=None, **kw: seen.setdefault("method", method))
    mod.NormalizedChatAnthropic(model=model, api_key="x").with_structured_output(dict)
    assert seen["method"] == method
