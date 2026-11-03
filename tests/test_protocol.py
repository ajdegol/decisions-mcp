import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from decisions_mcp import server

EVIDENCE = [{"type": "text", "text": "A practical explanation."}]
PREDICATE = {"type": "predicate", "name": "useful", "instructions": "Is this useful?"}
CASES = [
    ("evaluate", {"questions": [PREDICATE]}, "predicate"),
    ("check", {"statement": "Is this useful?"}, "predicate"),
    (
        "classify",
        {
            "instructions": "Choose a category.",
            "choices": [{"value": "useful"}, {"value": "filler"}],
        },
        "choice",
    ),
    (
        "score",
        {"instructions": "Rate usefulness.", "levels": [{"label": "low"}, {"label": "high"}]},
        "score",
    ),
]


def error_text(result):
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


async def exercise_protocol(tmp_path):
    env = {
        **os.environ,
        "OPENAI_API_KEY": "",
        "API_KEY": "",
        "DECISIONS_ENV_FILE": str(tmp_path / "does-not-exist.env"),
        "DECISIONS_ALLOWED_ROOTS": str(tmp_path),
        "DECISIONS_CACHE_PATH": str(tmp_path / "results.sqlite3"),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "decisions_mcp.server"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        initialized = await session.initialize()
        assert initialized.serverInfo.name == "Decisions"
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert set(tools) == {"evaluate", "check", "classify", "score"}
        for name, arguments, _ in CASES:
            schema = tools[name].inputSchema
            assert schema["type"] == "object" and "evidence" in schema["required"]
            assert "use_cache" in schema["properties"]
            result = await session.call_tool(
                name,
                {
                    "evidence": EVIDENCE,
                    "use_cache": False,
                    **arguments,
                },
            )
            assert result.isError
            assert "API key is not configured" in error_text(result)
        malformed = await session.call_tool(
            "classify",
            {
                "evidence": EVIDENCE,
                "instructions": "Choose.",
                "choices": [{"value": "only"}],
                "use_cache": False,
            },
        )
        assert malformed.isError
        assert "API key is not configured" not in error_text(malformed)
        unsupported = await session.call_tool(
            "check",
            {
                "evidence": [{"type": "audio", "path": "/fake.wav"}],
                "statement": "Is speech present?",
                "use_cache": False,
            },
        )
        assert unsupported.isError
        assert "API key is not configured" not in error_text(unsupported)


async def test_real_stdio_protocol_without_api_credentials(tmp_path):
    await asyncio.wait_for(exercise_protocol(tmp_path), timeout=30)


@pytest.mark.parametrize("name,arguments,question_type", CASES)
async def test_wrappers_preserve_question_semantics(monkeypatch, name, arguments, question_type):
    expected = {"answers": [{"type": "refusal", "name": "result"}]}
    delegate = AsyncMock(return_value=expected)
    monkeypatch.setattr(server, "get_client", lambda: SimpleNamespace(evaluate=delegate))
    result = await getattr(server, name)(evidence=EVIDENCE, use_cache=False, **arguments)
    assert result is expected
    delegate.assert_awaited_once()
    request = delegate.await_args.args[0]
    assert delegate.await_args.kwargs == {"use_cache": False}
    assert request.evidence[0].text == EVIDENCE[0]["text"]
    question = request.questions[0]
    assert question.type == question_type
    if name == "evaluate":
        assert question.model_dump() == PREDICATE
    elif name == "check":
        assert question.instructions == arguments["statement"]
    elif name == "classify":
        assert [choice.value for choice in question.choices] == ["useful", "filler"]
    else:
        assert [level.label for level in question.levels] == ["low", "high"]
