import base64
import json

import httpx
import pytest

from decisions_mcp.client import ENDPOINT, MODEL, DecisionError, DecisionsClient, Settings
from decisions_mcp.models import EvaluateRequest


def request(evidence=None, instructions="Is the condition present?"):
    return EvaluateRequest(
        evidence=evidence or [{"type": "text", "text": "Example evidence"}],
        questions=[{"type": "predicate", "name": "q", "instructions": instructions}],
    )


@pytest.fixture
async def rig(tmp_path):
    state = {
        "calls": [],
        "status": 200,
        "error": None,
        "payload": {
            "model": MODEL,
            "answers": [{"type": "predicate", "name": "q", "probability": 0.75}],
            "usage": {"input_tokens": 12, "output_tokens": 0, "total_tokens": 12},
        },
    }

    def handler(incoming):
        state["calls"].append(incoming)
        if state["error"]:
            raise state["error"]
        return httpx.Response(state["status"], json=state["payload"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        settings = Settings(
            api_key="test-secret", roots=(tmp_path,), cache_path=tmp_path / "results.sqlite3"
        )
        yield DecisionsClient(settings, http), state


async def test_request_and_persistent_cache(rig):
    client, state = rig
    first = await client.evaluate(request())
    assert first["api_called"] and not first["cache"]["hit"]
    sent = state["calls"][0]
    assert str(sent.url) == ENDPOINT
    assert sent.headers["authorization"] == "Bearer test-secret"
    body = json.loads(sent.content)
    assert body["model"] == MODEL
    assert body["questions"] == [
        {"type": "predicate", "name": "q", "instructions": "Is the condition present?"}
    ]
    assert body["input"][-1]["content"] == [{"type": "input_text", "text": "Example evidence"}]
    restarted = DecisionsClient(client.settings, client.http)
    second = await restarted.evaluate(request())
    assert len(state["calls"]) == 1
    assert second["cache"]["hit"] and not second["api_called"]
    assert second["usage"] is None
    assert second["cached_usage"] == first["usage"]
    assert second["answers"] == first["answers"]


async def test_canonical_json_and_changed_question(rig):
    client, state = rig
    a = request([{"type": "json", "data": {"a": 1, "b": {"x": 2, "y": 3}}}])
    b = request([{"type": "json", "data": {"b": {"y": 3, "x": 2}, "a": 1}}])
    first, second = await client.evaluate(a), await client.evaluate(b)
    assert first["cache"]["key"] == second["cache"]["key"]
    assert second["cache"]["hit"]
    b.questions[0].instructions = "A different condition?"
    await client.evaluate(b)
    assert len(state["calls"]) == 2


async def test_local_text_hashes_contents_not_paths(rig, tmp_path):
    client, state = rig
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("First transcript", encoding="utf-8")
    b.write_text("First transcript", encoding="utf-8")
    first = await client.evaluate(request([{"type": "text_path", "path": str(a)}]))
    second = await client.evaluate(request([{"type": "text_path", "path": str(b)}]))
    assert second["cache"]["hit"] and first["cache"]["key"] == second["cache"]["key"]
    b.write_text("Changed transcript", encoding="utf-8")
    third = await client.evaluate(request([{"type": "text_path", "path": str(b)}]))
    assert third["cache"]["key"] != first["cache"]["key"]
    assert len(state["calls"]) == 2


async def test_local_image_bytes_invalidate_cache(rig, tmp_path):
    client, state = rig
    path = tmp_path / "image.png"
    image = b"\x89PNG\r\n\x1a\nfirst"
    path.write_bytes(image)
    evidence = [{"type": "image_path", "path": str(path)}]
    first = await client.evaluate(request(evidence))
    body = json.loads(state["calls"][0].content)
    assert body["input"][-1]["content"][0]["image_url"] == (
        "data:image/png;base64," + base64.b64encode(image).decode()
    )
    assert (await client.evaluate(request(evidence)))["cache"]["hit"]
    path.write_bytes(image + b"changed")
    second = await client.evaluate(request(evidence))
    assert second["cache"]["key"] != first["cache"]["key"]
    assert len(state["calls"]) == 2


async def test_remote_images_always_bypass_cache(rig):
    client, state = rig
    item = request([{"type": "image_url", "url": "https://example.com/mutable.png"}])
    for _ in range(2):
        result = await client.evaluate(item)
        assert result["api_called"] and not result["cache"]["hit"]
        assert result["cache"]["stored_at"] is None
    assert len(state["calls"]) == 2
    assert not client.settings.cache_path.exists()


async def test_explicit_bypass_makes_fresh_call(rig):
    client, state = rig
    await client.evaluate(request())
    result = await client.evaluate(request(), use_cache=False)
    assert result["api_called"] and result["cache"]["stored_at"] is None
    assert len(state["calls"]) == 2


async def test_refusal_is_preserved_and_cacheable(rig):
    client, state = rig
    refusal = {"type": "refusal", "name": "q"}
    state["payload"]["answers"] = [refusal]
    assert (await client.evaluate(request()))["answers"] == [refusal]
    cached = await client.evaluate(request())
    assert cached["answers"] == [refusal] and cached["cache"]["hit"]


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
async def test_http_errors_are_sanitized_and_not_cached(rig, status):
    client, state = rig
    good = state["payload"]
    state.update(status=status, payload={"error": "private upstream text test-secret"})
    with pytest.raises(DecisionError) as error:
        await client.evaluate(request())
    assert str(status) in str(error.value)
    assert "private upstream" not in str(error.value) and "test-secret" not in str(error.value)
    state.update(status=200, payload=good)
    assert (await client.evaluate(request()))["api_called"]
    assert len(state["calls"]) == 2


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("private connection detail"),
        httpx.ConnectError("private connection detail"),
    ],
)
async def test_transport_errors_are_not_cached(rig, error):
    client, state = rig
    state["error"] = error
    with pytest.raises(DecisionError, match="no result was cached") as caught:
        await client.evaluate(request())
    assert "private connection detail" not in str(caught.value)
    state["error"] = None
    assert (await client.evaluate(request()))["api_called"]


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "predicate", "name": "wrong", "probability": 0.5},
        {"type": "predicate", "name": "q"},
        {"type": "predicate", "name": "q", "probability": 1.1},
        {"type": "predicate", "name": "q", "probability": True},
        {"type": "choice", "name": "q", "choice": "unknown"},
    ],
)
async def test_invalid_answers_are_rejected_without_caching(rig, answer):
    client, state = rig
    good = state["payload"]["answers"]
    state["payload"]["answers"] = [answer]
    with pytest.raises(DecisionError, match="invalid response"):
        await client.evaluate(request())
    state["payload"]["answers"] = good
    assert (await client.evaluate(request()))["api_called"]
    assert len(state["calls"]) == 2


async def test_mixed_questions_preserve_boolean_choices_and_fractional_scores(rig):
    client, state = rig
    req = EvaluateRequest(
        evidence=[{"type": "text", "text": "Evidence"}],
        questions=[
            {
                "type": "choice",
                "name": "category",
                "instructions": "Choose.",
                "choices": [{"value": True}, {"value": "true"}],
            },
            {
                "type": "score",
                "name": "quality",
                "instructions": "Rate.",
                "levels": [{"label": "low"}, {"label": "high"}],
            },
        ],
    )
    state["payload"]["answers"] = [
        {
            "type": "choice",
            "name": "category",
            "choice": True,
            "confidence": 0.8,
            "probabilities": [
                {"value": True, "probability": 0.8},
                {"value": "true", "probability": 0.2},
            ],
        },
        {
            "type": "score",
            "name": "quality",
            "score": 0.7,
            "confidence": 0.7,
            "probabilities": [
                {"value": 0, "label": "low", "probability": 0.3},
                {"value": 1, "label": "high", "probability": 0.7},
            ],
        },
    ]
    result = await client.evaluate(req)
    assert result["answers"][0]["choice"] is True
    assert result["answers"][1]["score"] == 0.7
