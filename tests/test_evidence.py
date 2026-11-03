import base64

import pytest
from pydantic import ValidationError
from test_client import request

from decisions_mcp.client import Settings
from decisions_mcp.evidence import prepare
from decisions_mcp.models import EvaluateRequest


def test_root_escape_and_symlink(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("not evidence")
    link = allowed / "link.txt"
    link.symlink_to(outside)
    for path in [outside, allowed / ".." / "secret.txt", link]:
        with pytest.raises(ValueError, match="outside"):
            prepare(request([{"type": "text_path", "path": str(path)}]).evidence, (allowed,))


def test_binary_and_invalid_images_are_rejected(tmp_path):
    path = tmp_path / "binary.txt"
    path.write_bytes(b"\xff\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        prepare(request([{"type": "text_path", "path": str(path)}]).evidence, (tmp_path,))
    with pytest.raises(ValueError, match="Supported local image"):
        prepare(request([{"type": "image_path", "path": str(path)}]).evidence, (tmp_path,))


def test_data_urls_checked_and_cacheable(tmp_path):
    url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    parts, cacheable = prepare(request([{"type": "image_url", "url": url}]).evidence, (tmp_path,))
    assert cacheable and parts[0]["image_url"] == url
    with pytest.raises(ValueError, match="valid base64"):
        prepare(
            request([{"type": "image_url", "url": "data:image/png;base64,!!!"}]).evidence,
            (tmp_path,),
        )


def test_duplicate_question_names_and_choices():
    q = {"type": "predicate", "name": "same", "instructions": "Is it present?"}
    with pytest.raises(ValidationError, match="unique"):
        EvaluateRequest(evidence=[{"type": "text", "text": "x"}], questions=[q, q])
    with pytest.raises(ValidationError, match="unique"):
        EvaluateRequest(
            evidence=[{"type": "text", "text": "x"}],
            questions=[
                {
                    "type": "choice",
                    "name": "same",
                    "instructions": "Choose.",
                    "choices": [{"value": "x"}, {"value": "x"}],
                }
            ],
        )


def test_explicit_env_file_api_key_alias(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("API_KEY=not-a-real-test-key\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("DECISIONS_ENV_FILE", str(path))
    assert Settings.from_env().api_key == "not-a-real-test-key"
    assert "not-a-real-test-key" not in repr(Settings.from_env())
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    assert Settings.from_env().api_key == "environment-key"


def test_cache_expiration(tmp_path, monkeypatch):
    from decisions_mcp.cache import ResultCache

    cache = ResultCache(tmp_path / "cache.sqlite3", ttl=10)
    monkeypatch.setattr("decisions_mcp.cache.time.time", lambda: 100)
    cache.put("key", {"answers": []})
    assert cache.get("key")
    monkeypatch.setattr("decisions_mcp.cache.time.time", lambda: 111)
    assert cache.get("key") is None


def test_process_api_key_alias_overrides_dotenv_canonical_key(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=file-key\n")
    monkeypatch.setenv("DECISIONS_ENV_FILE", str(path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("API_KEY", "process-key")
    assert Settings.from_env().api_key == "process-key"
