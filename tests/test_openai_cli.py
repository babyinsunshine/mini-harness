"""Exercise the real provider adapter and CLI against a local SSE endpoint."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def test_cli_with_real_openai_adapter_streams_tools_and_restores_session(tmp_path):
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
                                        "arguments": json.dumps(
                                            {
                                                "path": "smoke.txt",
                                                "content": "working\n",
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                        None,
                    ),
                    ({}, "tool_calls"),
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
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
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
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
