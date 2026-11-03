"""Real binary integration tests against an isolated local HTTP endpoint.

Run after cargo build: .venv/bin/python -m unittest discover -s tests -p test_rust_cli.py -v
No live credential store or paid API is used.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "target/debug/decisions"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, self.headers.get("Authorization"), body))
        mode = self.server.mode
        if mode == "http_error":
            self.send_response(401)
            payload = b'{"error":"sensitive-upstream-body"}'
        elif mode == "invalid_json":
            self.send_response(200)
            payload = b"not json: sensitive-upstream-body"
        else:
            self.send_response(200)
            answers = []
            for q in body["questions"]:
                a = {"type": q["type"], "name": q["name"]}
                if q["type"] == "predicate":
                    a["probability"] = 0.75
                elif q["type"] == "choice":
                    a.update(choice=q["choices"][0]["value"], confidence=0.8,
                             probabilities=[{"value": c["value"], "probability": 1 / len(q["choices"])} for c in q["choices"]])
                else:
                    a.update(score=0.5, confidence=0.8,
                             probabilities=[{"value": i, "label": level["label"], "probability": 1 / len(q["levels"])} for i, level in enumerate(q["levels"])])
                answers.append(a)
            if mode == "invalid_answer":
                answers[0]["name"] = "wrong-name"
            if mode == "refusal":
                answers[0] = {"type": "refusal", "name": body["questions"][0]["name"], "refusal": "Cannot evaluate"}
            payload = json.dumps({"answers": answers, "usage": {"input_tokens": 5, "output_tokens": 2}}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@unittest.skipUnless(BINARY.exists(), "Run cargo build first")
class RustCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.calls = []
        self.server.mode = "normal"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.stop_server)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("DECISIONS_") and k not in {"API_KEY", "OPENAI_API_KEY", "XDG_CONFIG_HOME"}}
        self.env.update(
            DECISIONS_CONFIG=str(self.dir / "config.toml"),
            DECISIONS_CACHE_PATH=str(self.dir / "cache.sqlite3"),
            DECISIONS_ALLOWED_ROOTS=str(self.dir),
            DECISIONS_BASE_URL=f"http://127.0.0.1:{self.server.server_port}/v1",
            DECISIONS_API_KEY="fake-local-test-key",
        )
        self.evidence = [{"type": "text", "text": "A concrete test sample"}]
        self.check = {"evidence": self.evidence, "statement": "Is this a sample?"}

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def invoke(self, *args, data=None, env=None):
        return subprocess.run([str(BINARY), *args], input=json.dumps(data) if data is not None else "",
                              text=True, capture_output=True, env=env or self.env, timeout=20)

    def result(self, tool, args):
        p = self.invoke(tool, data=args)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_all_cli_question_types_and_gateway_routing(self):
        self.env["DECISIONS_MODEL"] = "gateway-luna-alias"
        requests = {
            "check": self.check,
            "classify": {"evidence": self.evidence, "instructions": "Choose", "choices": [{"value": True}, {"value": "other"}]},
            "score": {"evidence": self.evidence, "instructions": "Rate", "levels": [{"label": "low"}, {"label": "high"}]},
            "evaluate": {"evidence": self.evidence, "questions": [{"type": "predicate", "name": "batch", "instructions": "Is this useful?"}]},
        }
        for tool, args in requests.items():
            with self.subTest(tool=tool):
                result = self.result(tool, args)
                self.assertTrue(result["api_called"])
                self.assertFalse(result["cache"]["hit"])
        self.assertEqual(len(self.server.calls), 4)
        for path, auth, body in self.server.calls:
            self.assertEqual(path, "/v1/decisions")
            self.assertEqual(auth, "Bearer fake-local-test-key")
            self.assertEqual(body["model"], "gateway-luna-alias")
            self.assertNotIn("use_cache", body)

    def test_cache_is_opt_in_for_json_and_cli(self):
        for _ in range(2):
            result = self.result("check", self.check)
            self.assertTrue(result["api_called"])
            self.assertIsNone(result["cache"]["stored_at"])
        self.assertEqual(len(self.server.calls), 2)
        self.assertFalse((self.dir / "cache.sqlite3").exists())
        for expected_hit in (False, True):
            p = self.invoke("check", "--cache", data=self.check)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(p.stdout)["cache"]["hit"], expected_hit)
        self.assertEqual(len(self.server.calls), 3)
        self.assertTrue(self.result("check", self.check)["api_called"])
        p = self.invoke("check", "--no-cache", data=dict(self.check, use_cache=True))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)["api_called"])
        self.assertEqual(len(self.server.calls), 5)

    def test_cache_hits_bypass_and_identity_separation(self):
        self.check["use_cache"] = True
        first = self.result("check", self.check)
        second = self.result("check", self.check)
        self.assertFalse(second["api_called"])
        self.assertIsNone(second["usage"])
        self.assertEqual(second["cached_usage"], first["usage"])
        self.assertEqual(len(self.server.calls), 1)
        self.result("check", dict(self.check, use_cache=False))
        self.assertEqual(len(self.server.calls), 2)
        self.env["DECISIONS_API_KEY"] = "another-fake-key"
        self.assertTrue(self.result("check", self.check)["api_called"])
        self.env["DECISIONS_BASE_URL"] += "/tenant"
        self.assertTrue(self.result("check", self.check)["api_called"])
        self.assertEqual(self.server.calls[-1][0], "/v1/tenant/decisions")
        self.assertEqual(len(self.server.calls), 4)

    def test_file_and_convenience_inputs(self):
        path = self.dir / "request.json"
        path.write_text(json.dumps(self.check))
        p = self.invoke("check", "--input", str(path))
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.invoke("check", "--text", "hello", "--statement", "Is this a greeting?", "--no-cache")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(self.server.calls), 2)
        self.assertEqual(self.server.calls[-1][2]["input"][1]["content"][0]["text"], "hello")

    def test_failures_never_cache_or_leak_upstream(self):
        for mode in ["http_error", "invalid_json", "invalid_answer"]:
            with self.subTest(mode=mode):
                self.server.mode = mode
                p = self.invoke("check", data=dict(self.check, use_cache=True))
                self.assertNotEqual(p.returncode, 0)
                self.assertNotIn("sensitive-upstream-body", p.stdout + p.stderr)
                self.assertNotIn("fake-local-test-key", p.stdout + p.stderr)
        self.server.mode = "normal"
        self.assertTrue(self.result("check", self.check)["api_called"])
        self.assertEqual(len(self.server.calls), 4)

    def test_invalid_input_never_calls_network(self):
        bad = [dict(self.check, extra="no"), {"evidence": [], "statement": "x"},
               {"evidence": [{"type": "text_path", "path": str(self.dir.parent / "forbidden")}], "statement": "x"}]
        for args in bad:
            self.assertNotEqual(self.invoke("check", data=args).returncode, 0)
        self.assertFalse(self.server.calls)

    def test_refusals_preserved_and_cached(self):
        self.check["use_cache"] = True
        self.server.mode = "refusal"
        first = self.result("check", self.check)
        second = self.result("check", self.check)
        self.assertEqual(first["answers"][0]["type"], "refusal")
        self.assertEqual(second["answers"], first["answers"])
        self.assertFalse(second["api_called"])
        self.assertEqual(len(self.server.calls), 1)

    def test_configuration_defaults_persistence_and_url_validation(self):
        env = dict(self.env)
        env.pop("DECISIONS_BASE_URL")
        p = self.invoke("config", "show", env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["base_url"], "https://api.openai.com/v1")
        self.assertNotIn("fake-local-test-key", p.stdout)
        self.assertEqual(self.invoke("config", "set", "base_url", "https://gateway.example/v1/", env=env).returncode, 0)
        self.assertEqual(self.invoke("config", "set", "model", "my-luna", env=env).returncode, 0)
        settings = json.loads(self.invoke("config", "show", env=env).stdout)
        self.assertEqual(settings["base_url"], "https://gateway.example/v1")
        self.assertEqual(settings["model"], "my-luna")
        for bad in ["https://user:secret@example.com/v1", "https://example.com/v1?key=secret", "http://remote.example/v1"]:
            self.assertNotEqual(self.invoke("config", "set", "base_url", bad, env=env).returncode, 0)

    def test_mcp_handshake_discovery_and_structured_call(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "check", "arguments": self.check}},
            {"jsonrpc": "2.0", "id": 4, "method": "ping"},
        ]
        p = subprocess.run([str(BINARY), "mcp"], input="\n".join(map(json.dumps, messages)) + "\n",
                           text=True, capture_output=True, env=self.env, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        outputs = [json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual([r["id"] for r in outputs], [1, 2, 3, 4])
        self.assertEqual(outputs[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual({t["name"] for t in outputs[1]["result"]["tools"]}, {"check", "classify", "score", "evaluate"})
        answer = outputs[2]["result"]
        self.assertFalse(answer["isError"])
        self.assertEqual(answer["structuredContent"]["answers"][0]["probability"], 0.75)
        self.assertEqual(json.loads(answer["content"][0]["text"]), answer["structuredContent"])
        self.assertEqual(len(self.server.calls), 1)

    def test_mcp_cache_is_opt_in(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        for i, args in enumerate([self.check, self.check,
                                  dict(self.check, use_cache=True), dict(self.check, use_cache=True)], 3):
            messages.append({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                             "params": {"name": "check", "arguments": args}})
        p = subprocess.run([str(BINARY), "mcp"], input="\n".join(map(json.dumps, messages)) + "\n",
                           text=True, capture_output=True, env=self.env, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        outputs = [json.loads(line) for line in p.stdout.splitlines()]
        for tool in outputs[1]["result"]["tools"]:
            self.assertIs(tool["inputSchema"]["properties"]["use_cache"]["default"], False)
        results = [o["result"]["structuredContent"] for o in outputs[2:]]
        self.assertEqual([r["api_called"] for r in results], [True, True, True, False])
        self.assertTrue(all(r["cache"]["stored_at"] is None for r in results[:2]))
        self.assertEqual(len(self.server.calls), 3)

    def test_mcp_discovery_does_not_require_credentials(self):
        env = dict(self.env)
        env.pop("DECISIONS_API_KEY")
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        p = subprocess.run([str(BINARY), "mcp"], input="\n".join(map(json.dumps, messages)) + "\n",
                           text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(json.loads(p.stdout.splitlines()[1])["result"]["tools"]), 4)
        self.assertFalse(self.server.calls)


if __name__ == "__main__":
    unittest.main()
