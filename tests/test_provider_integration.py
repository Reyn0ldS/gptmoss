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
    assert provider.client.max_retries == 0


@pytest.mark.asyncio
async def test_native_tool_request_without_call_demotes_to_prompt_protocol():
    provider = QwenProvider(
        api_key="local-key",
        base_url="http://127.0.0.1:9/v1",
        default_model="local-model",
    )
    native_requests = []
    fallback_requests = []

    async def native_response(arguments, **_kwargs):
        native_requests.append(arguments)
        return {
            "content": "I need to call filesystem__write next.",
            "tool_calls": None,
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }

    async def prompt_response(messages, tools, model, **_kwargs):
        fallback_requests.append((messages, tools, model))
        return {
            "content": None,
            "tool_calls": [{
                "id": "prompt-write",
                "type": "function",
                "function": {
                    "name": "filesystem__write",
                    "arguments": {"path": "report.md", "content": "verified"},
                },
            }],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        }

    provider._create_with_context_recovery = native_response
    provider._prompt_based_tool_calling = prompt_response
    tools = [{
        "type": "function",
        "function": {
            "name": "filesystem__write",
            "description": "Write a file",
            "parameters": {"type": "object"},
        },
    }]
    try:
        first = await provider.completion(
            messages=[{"role": "user", "content": "Write the report"}],
            tools=tools,
        )
        second = await provider.completion(
            messages=[{"role": "user", "content": "Use the tool now"}],
            tools=tools,
        )
    finally:
        await provider.close()

    assert first["content"].startswith("I need to call")
    assert provider._native_tools_supported is False
    assert len(native_requests) == 1
    assert len(fallback_requests) == 1
    assert second["tool_calls"][0]["function"]["name"] == "filesystem__write"


@pytest.mark.asyncio
async def test_prompt_tool_protocol_makes_required_choice_explicit():
    provider = QwenProvider(
        api_key="local-key",
        base_url="http://127.0.0.1:9/v1",
        default_model="local-model",
    )
    captured = {}

    async def prompt_response(arguments, **_kwargs):
        captured.update(arguments)
        return {
            "content": '{"tool_call":{"name":"filesystem__append","arguments":{"path":"report.md","content":"chunk"}}}',
            "tool_calls": None,
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        }

    provider._native_tools_supported = False
    provider._create_with_context_recovery = prompt_response
    tools = [{
        "type": "function",
        "function": {
            "name": "filesystem__append",
            "description": "Append text",
            "parameters": {"type": "object"},
        },
    }]
    try:
        response = await provider.completion(
            messages=[{"role": "user", "content": "Continue the report"}],
            tools=tools,
            tool_choice="required",
        )
    finally:
        await provider.close()

    system_prompt = captured["messages"][0]["content"]
    assert "A tool call is REQUIRED for this turn" in system_prompt
    assert response["tool_calls"][0]["function"]["name"] == "filesystem__append"


def test_qwen_textual_tool_calls_are_normalized_and_deterministic():
    xml_content = """<tool_call>
<function=filesystem__write>
<parameter=path>
live-check.txt
</parameter>
<parameter=content>
private-llm-ok
</parameter>
<parameter=overwrite>
true
</parameter>
</function>
</tool_call>"""
    first = QwenProvider._parse_text_tool_calls(xml_content)
    second = QwenProvider._parse_text_tool_calls(xml_content)

    assert first == second
    assert len(first) == 1
    assert first[0]["id"].startswith("qwen-text-")
    assert first[0]["function"] == {
        "name": "filesystem__write",
        "arguments": {
            "path": "live-check.txt",
            "content": "private-llm-ok",
            "overwrite": True,
        },
    }

    json_content = (
        '<tool_call>{"name":"shell__execute",'
        '"arguments":{"command":"python -m pytest -q"}}</tool_call>'
    )
    parsed_json = QwenProvider._parse_text_tool_calls(json_content)
    assert parsed_json[0]["function"] == {
        "name": "shell__execute",
        "arguments": {"command": "python -m pytest -q"},
    }
    assert QwenProvider._parse_text_tool_calls("<tool_call>invalid</tool_call>") == []

    fenced_content = """```json
{"tool_call":{"name":"filesystem__write","arguments":{"path":"small.py","content":"VALUE = 1\\n"}}}
```"""
    fenced = QwenProvider._parse_text_tool_calls(fenced_content)
    assert fenced[0]["id"].startswith("qwen-text-")
    assert fenced[0]["function"] == {
        "name": "filesystem__write",
        "arguments": {"path": "small.py", "content": "VALUE = 1\n"},
    }
