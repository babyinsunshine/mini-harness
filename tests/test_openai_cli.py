"""Exercise the real provider adapter and CLI against a local SSE endpoint."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.parametrize(
    "outside, broken, full_access",
    [
        (False, None, False),
        (True, None, False),
        (True, None, True),
        (False, "length", False),
        (False, "invalid", False),
        (False, "partial-normal", False),
        (False, "partial-missing", False),
    ],
    ids=[
        "project-write",
        "approval-denied",
        "full-access",
        "truncated",
        "malformed",
        "partial-normal",
        "partial-missing",
    ],
)
def test_cli_with_real_openai_adapter_streams_tools_and_restores_session(
    tmp_path, outside, broken, full_access,
):
    pytest.importorskip("langchain_openai")
    requests = []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if any(message["role"] == "tool" for message in body["messages"]):
                chunks = [({"content": "Local "}, None), ({"content": "verified"}, "stop")]
            else:
                arguments = json.dumps(
                    {
                        "path": str(tmp_path / "outside.txt") if outside else "smoke.txt",
                        "content": "working\n",
                    }
                )
                if broken == "invalid":
                    arguments = '{"path": }'
                elif broken:
                    arguments = '{"path":"smoke.txt","content":"partial'
                chunks = [
                    (
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "local-write",
                                    "type": "function",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": arguments,
                                    },
                                }
                            ],
                        },
                        None,
                    ),
                    ({}, "length" if broken == "length" else "tool_calls"),
                ]
            for delta, finish in chunks:
                chunk = {
                    "id": "chatcmpl-local",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "local-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "finish_reason": finish,
                        }
                    ],
                }
                if broken == "partial-missing":
                    del chunk["choices"][0]["finish_reason"]
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
            usage = {
                "id": "chatcmpl-local", "object": "chat.completion.chunk", "created": 1,
                "model": "local-model", "choices": [],
                "usage": {
                    "prompt_tokens": 700, "completion_tokens": 50, "total_tokens": 750,
                    "prompt_tokens_details": {"cached_tokens": 560},
                    "prompt_cache_hit_tokens": 560, "prompt_cache_miss_tokens": 140,
                },
            }
            self.wfile.write(("data: " + json.dumps(usage) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    project = tmp_path / "project"
    project.mkdir()
    root = tmp_path / "sessions"
    environment = dict(
        os.environ,
        OPENAI_API_KEY="local-test-not-a-secret",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
    )
    command = [sys.executable, "-m", "mini_harness", "--session-dir", str(root)]
    try:
        result = subprocess.run(
            [
                *command,
                *(["--full-access"] if full_access else []),
                "--cwd",
                str(project),
                "--model",
                "openai:local-model",
                "--base-url",
                f"http://127.0.0.1:{server.server_port}/v1",
                "-p",
                "Create a file",
            ],
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "Local verified"
        assert "700 in / 50 out" in result.stderr
        assert "cache 80.0% (560/700)" in result.stderr
        if (outside and not full_access) or broken:
            assert not (tmp_path / "outside.txt").exists()
            assert not (project / "smoke.txt").exists()
            assert any(
                message["role"] == "tool" and "not executed" in message["content"]
                for message in requests[-1]["messages"]
            )
        elif full_access:
            assert (tmp_path / "outside.txt").read_text() == "working\n"
        else:
            assert (project / "smoke.txt").read_text() == "working\n"
        session_id = next(root.iterdir()).name
        resumed = subprocess.run(
            [
                *command,
                "--resume",
                session_id,
                "-p",
                "Continue",
            ],
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert resumed.returncode == 0, resumed.stderr
        assert resumed.stdout.strip() == "Local verified"
        assert all(request["stream"] for request in requests)
        assert all(request.get("stream_options", {}).get("include_usage") for request in requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
