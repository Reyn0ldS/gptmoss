import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from gptmoss.providers import QwenProvider


class LocalOpenAIHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append({"path": self.path, "body": body})
        arguments = json.dumps({"path": "local.txt", "content": "from local model"})
        payload = json.dumps({
            "id": "chatcmpl-local",
            "object": "chat.completion",
            "created": 1,
            "model": body.get("model", "local-model"),
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-local-write",
                        "type": "function",
                        "function": {"name": "filesystem__write", "arguments": arguments},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@pytest.mark.asyncio
async def test_local_openai_compatible_tool_call_round_trip():
    LocalOpenAIHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalOpenAIHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = QwenProvider(
        api_key="local-key",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        default_model="local-model",
    )
    try:
        response = await provider.completion(
            messages=[{"role": "user", "content": "Write the local file"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "filesystem__write",
                    "description": "Write a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"],
                    },
                },
            }],
        )
    finally:
        await provider.client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert LocalOpenAIHandler.requests[0]["path"] == "/v1/chat/completions"
    assert LocalOpenAIHandler.requests[0]["body"]["model"] == "local-model"
    tool_call = response["tool_calls"][0]
    assert tool_call["id"] == "call-local-write"
    assert tool_call["function"] == {
        "name": "filesystem__write",
        "arguments": {"path": "local.txt", "content": "from local model"},
    }
    assert response["usage"]["total_tokens"] == 12
