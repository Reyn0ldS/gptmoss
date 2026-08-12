import inspect
from typing import List, Dict, Any, Optional
from gptmoss.interfaces.llm import LLMProvider

class MockLLMProvider(LLMProvider):
    """
    Mock LLM provider for unit testing the execution loops and kernel orchestration.
    """
    def __init__(self):
        self.responses: List[Dict[str, Any]] = []
        self.call_count = 0
        self.api_key = ""
        self.base_url = ""
        self.default_model = ""

    def update_config(self, api_key: str, base_url: str, ssl_verify: bool = False, ssl_cert_path: str = "", model_name: str = "qwen-turbo"):
        self.api_key = api_key
        self.base_url = base_url
        self.default_model = model_name

    def add_response(self, content: Optional[str] = None, tool_calls: Optional[List[Dict[str, Any]]] = None):
        self.responses.append({
            "content": content,
            "tool_calls": tool_calls
        })

    async def completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        if self.call_count < len(self.responses):
            res = self.responses[self.call_count]
        else:
            res = {"content": "Default mock response", "tool_calls": None}
            
        self.call_count += 1
        payload = {
            "content": res.get("content"),
            "tool_calls": res.get("tool_calls"),
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
        }
        on_text_delta = kwargs.get("on_text_delta")
        content = payload.get("content")
        if on_text_delta and content:
            result = on_text_delta(content)
            if inspect.isawaitable(result):
                await result
        return payload

    async def embeddings(self, texts: List[str], **kwargs) -> List[List[float]]:
        return [[0.1] * 1536 for _ in texts]

    async def tokenize(self, text: str, **kwargs) -> List[int]:
        return [ord(c) for c in text]

    async def models(self) -> List[str]:
        return ["mock-qwen"]
