import hashlib
import json
import os
import logging
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI
from gptmoss.interfaces.llm import LLMProvider
from gptmoss.providers.qwen_support import ContextWindowPolicy, ToolCallParser

logger = logging.getLogger("gptmoss.providers.qwen")

class QwenProvider(LLMProvider):
    """
    OpenAI-compatible LLM Provider.
    Configured for Qwen (DashScope or local OpenAI-compatible api).
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: str = "qwen-turbo"
    ):
        # Fall back to env variables or defaults
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or "mock-key"
        # DashScope OpenAI-compatible endpoint or local host
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        self.default_model = default_model
        self.vision_mode = "auto"
        self.supports_vision = self._infer_vision(default_model)
        self._native_tools_supported: Optional[bool] = None
        self._learned_context_chars: Optional[int] = None
        
        logger.info(f"Initializing QwenProvider calling base_url={self.base_url} with default_model={self.default_model}")
        import httpx
        # Certificate validation is secure by default. A local/self-signed
        # endpoint can still be enabled explicitly through settings.
        http_client = httpx.AsyncClient(verify=True)
        # ExecutionEngine owns provider recovery and persists waiting state.
        # Hidden SDK retries multiply that policy and can make one request
        # monopolize an execution for many minutes without observable state.
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, http_client=http_client,
                                  max_retries=0, timeout=90.0)

    @staticmethod
    def _infer_vision(model_name: str) -> bool:
        return any(
            marker in str(model_name).lower()
            for marker in ("vision", "-vl", "omni", "multimodal")
        )

    def set_vision_mode(self, mode: str = "auto") -> None:
        normalized = str(mode or "auto").strip().lower()
        if normalized not in {"auto", "enabled", "disabled"}:
            raise ValueError("vision_mode must be auto, enabled, or disabled.")
        self.vision_mode = normalized
        self.supports_vision = (
            self._infer_vision(self.default_model)
            if normalized == "auto" else normalized == "enabled"
        )

    @staticmethod
    def _log_completion_error(error: Exception):
        text = (error.__class__.__name__ + " " + str(error)).lower()
        if any(marker in text for marker in ("connection", "timeout", "rate limit", "429", "502", "503", "504")):
            logger.warning("Temporary LLM provider failure (%s): %s", error.__class__.__name__, error)
        else:
            logger.error("Error in LLM completion: %s", error, exc_info=True)

    def update_config(self, api_key: str, base_url: str, ssl_verify: bool = False, ssl_cert_path: str = "", model_name: str = "qwen-turbo"):
        self.api_key = api_key
        self.base_url = base_url
        self.default_model = model_name
        self.supports_vision = (
            self._infer_vision(model_name)
            if self.vision_mode == "auto" else self.vision_mode == "enabled"
        )
        self._native_tools_supported = None
        self._learned_context_chars = None
        
        import httpx
        if ssl_verify:
            verify_value = ssl_cert_path if ssl_cert_path else True
        else:
            verify_value = False
            
        http_client = httpx.AsyncClient(verify=verify_value)
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, http_client=http_client,
                                  max_retries=0, timeout=90.0)
        logger.info(f"QwenProvider config updated. base_url={self.base_url}, ssl_verify={ssl_verify}, ssl_cert_path={ssl_cert_path}")

    @staticmethod
    def _is_context_limit_error(error: Exception) -> bool:
        return ContextWindowPolicy.is_limit_error(error)

    @staticmethod
    def _message_chars(messages: List[Dict[str, Any]]) -> int:
        return ContextWindowPolicy.message_chars(messages)

    @classmethod
    def _compact_messages(
        cls, messages: List[Dict[str, Any]], target_chars: int
    ) -> List[Dict[str, Any]]:
        """Drop oldest complete context items while preserving instructions and recent tool ordering."""
        return ContextWindowPolicy.compact(messages, target_chars)

    async def _create_with_context_recovery(self, arguments: Dict[str, Any]):
        """Learn a provider's effective context size and recover without losing task state."""
        request = dict(arguments)
        messages = [dict(item) for item in request.get("messages") or []]
        if self._learned_context_chars:
            messages = self._compact_messages(messages, self._learned_context_chars)
        for attempt in range(5):
            request["messages"] = messages
            try:
                return await self.client.chat.completions.create(**request)
            except Exception as error:
                if not self._is_context_limit_error(error) or attempt >= 4:
                    raise
                current_size = self._message_chars(messages)
                learned = max(2_000, int(current_size * 0.7))
                self._learned_context_chars = (
                    learned if self._learned_context_chars is None
                    else min(self._learned_context_chars, learned)
                )
                compacted = self._compact_messages(messages, self._learned_context_chars)
                if compacted == messages:
                    raise
                messages = compacted
                logger.warning(
                    "Provider context limit reached; retrying with %s learned characters.",
                    self._learned_context_chars,
                )

    async def completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Send completion request to Qwen/OpenAI compatible API."""
        model = kwargs.pop("model", self.default_model)
        
        if not tools:
            # Standard chat completion without tool schema
            logger.debug(f"Calling LLM: {model} with {len(messages)} messages (no tools)")
            try:
                response = await self._create_with_context_recovery({
                    "model": model,
                    "messages": messages,
                    **kwargs,
                })
                return self._parse_openai_response(response)
            except Exception as e:
                self._log_completion_error(e)
                raise e

        if self._native_tools_supported is False:
            return await self._prompt_based_tool_calling(messages, tools, model, **kwargs)

        # Build arguments for openai client to try native tool calling
        client_kwargs = {
            "model": model,
            "messages": messages,
            **kwargs
        }
        client_kwargs["tools"] = tools
        if tool_choice:
            client_kwargs["tool_choice"] = tool_choice
            
        logger.debug(f"Calling LLM: {model} with {len(messages)} messages and {len(tools)} tools (trying native first)")

        try:
            response = await self._create_with_context_recovery(client_kwargs)
            parsed = self._parse_openai_response(response)
            self._native_tools_supported = True
            return parsed
        except Exception as e:
            err_msg = str(e).lower()
            # If native tool calling fails because auto tool choice is disabled on remote server, fall back
            if (
                "tool_choice" in err_msg or "tool-call-parser" in err_msg
                or "tool_call" in err_msg
                or ("400" in err_msg and not self._is_context_limit_error(e))
            ):
                logger.warning("Native tool calling failed/not supported by remote endpoint, falling back to prompt-based tool calling.")
                self._native_tools_supported = False
                return await self._prompt_based_tool_calling(messages, tools, model, **kwargs)
            else:
                self._log_completion_error(e)
                raise e

    def _parse_openai_response(self, response) -> Dict[str, Any]:
        return ToolCallParser.parse_response(response)

    @staticmethod
    def _parse_text_tool_calls(content: str) -> List[Dict[str, Any]]:
        """Parse common Qwen textual tool-call formats into OpenAI calls."""
        return ToolCallParser.parse_text(content)

    async def _prompt_based_tool_calling(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model: str,
        **kwargs
    ) -> Dict[str, Any]:
        import json
        
        # Format tools list for prompt injection
        tools_desc = []
        for t in tools:
            func = t.get("function", {})
            tools_desc.append({
                "name": func.get("name"),
                "description": func.get("description"),
                "parameters": func.get("parameters")
            })
            
        system_instruction = (
            "\n[SYSTEM INSTRUCTION: TOOL USE]\n"
            "You have access to the following tools:\n"
            f"{json.dumps(tools_desc, indent=2)}\n\n"
            "If you need to call a tool, you MUST respond ONLY with a JSON object matching this schema:\n"
            "{\n"
            '  "tool_call": {\n'
            '    "name": "tool_name",\n'
            '    "arguments": { ... }\n'
            '  }\n'
            "}\n"
            "Do not add any text or conversational filler outside of the JSON object. "
            "If you do not need to call a tool, reply with a normal message."
        )
        
        # Cleanse and translate messages to standard user/assistant text format
        cleaned_messages = []
        for msg in messages:
            role = msg.get("role")
            if isinstance(msg.get("content"), list):
                text_parts = []
                for part in msg["content"]:
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        text_parts.append("[image attached]")
                cleaned_messages.append({"role": role, "content": "\n".join(text_parts)})
                continue
            if role == "tool":
                cleaned_messages.append({
                    "role": "user",
                    "content": f"[Résultat de la capacité {msg.get('name')}]:\n{msg.get('content')}"
                })
            elif role == "assistant" and msg.get("tool_calls"):
                tc_list = msg.get("tool_calls", [])
                if tc_list:
                    tc = tc_list[0]
                    func = tc.get("function", {})
                    args = func.get("arguments", {})
                    tc_json = {
                        "tool_call": {
                            "name": func.get("name"),
                            "arguments": args
                        }
                    }
                    text_rep = f"```json\n{json.dumps(tc_json, indent=2)}\n```"
                    original_content = msg.get("content") or ""
                    new_content = (original_content + "\n" + text_rep).strip()
                    cleaned_messages.append({
                        "role": "assistant",
                        "content": new_content
                    })
                else:
                    cleaned_messages.append(dict(msg))
            else:
                cleaned_messages.append(dict(msg))

        # Inject instruction safely into system messages context (vLLM rejects system messages placed at the end)
        fallback_messages = []
        system_msg_found = False
        
        for msg in cleaned_messages:
            if msg.get("role") == "system" and not system_msg_found:
                new_content = msg.get("content", "") + "\n\n" + system_instruction
                fallback_messages.append({"role": "system", "content": new_content})
                system_msg_found = True
            else:
                fallback_messages.append(msg)
                
        if not system_msg_found:
            fallback_messages.insert(0, {"role": "system", "content": system_instruction})
        
        # Make a standard chat completion call
        response = await self._create_with_context_recovery({
            "model": model,
            "messages": fallback_messages,
            **kwargs,
        })
        
        choice = response.choices[0]
        content = choice.message.content or ""
        
        # Robustly extract JSON block from conversational text response
        def _extract_json_block(text: str) -> Optional[Dict[str, Any]]:
            import json
            text_str = text.strip()
            try:
                return json.loads(text_str)
            except Exception:
                pass
            if "```json" in text_str:
                try:
                    block = text_str.split("```json")[1].split("```")[0].strip()
                    return json.loads(block)
                except Exception:
                    pass
            first_idx = text_str.find("{")
            last_idx = text_str.rfind("}")
            if first_idx != -1 and last_idx != -1 and last_idx > first_idx:
                try:
                    block = text_str[first_idx:last_idx+1].strip()
                    return json.loads(block)
                except Exception:
                    pass
            return None

        parsed = _extract_json_block(content)
        tool_calls = None
        text_content = content
        
        if parsed:
            try:
                tc_data = parsed.get("tool_call") or parsed.get("tool_calls")
                if tc_data:
                    if isinstance(tc_data, list) and len(tc_data) > 0:
                        tc_single = tc_data[0]
                    else:
                        tc_single = tc_data
                        
                    tool_name = tc_single.get("name")
                    tool_args = tc_single.get("arguments")
                    if not tool_args:
                        # Fallback: if arguments key is missing or empty, treat all other keys (excluding 'name') as arguments
                        tool_args = {k: v for k, v in tc_single.items() if k not in ("name", "arguments")}
                    elif isinstance(tool_args, str):
                        try:
                            tool_args = json.loads(tool_args)
                        except Exception:
                            pass
                    while (isinstance(tool_args, dict) and len(tool_args) == 1
                           and isinstance(tool_args.get("arguments"), dict)):
                        tool_args = tool_args["arguments"]
                    
                    if not isinstance(tool_args, dict):
                        tool_args = {}
                    
                    # Verify that tool name matches available tools
                    if tool_name in [t.get("function", {}).get("name") for t in tools]:
                        stable_payload = json.dumps(
                            {"name": tool_name, "arguments": tool_args},
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        )
                        call_id = "qwen-prompt-" + hashlib.sha256(
                            stable_payload.encode("utf-8")
                        ).hexdigest()[:16]
                        tool_calls = [{
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": tool_args
                            }
                        }]
                        
                        # Preserve conversational text prefix written before the JSON block
                        first_brace = content.find("{")
                        if first_brace > 0:
                            text_content = content[:first_brace].strip()
                            if text_content.endswith("```json") or text_content.endswith("```"):
                                text_content = text_content.rsplit("```", 1)[0].strip()
                        else:
                            text_content = None
            except Exception:
                pass
            
        usage = {
            "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
            "completion_tokens": response.usage.completion_tokens if response.usage else 0,
            "total_tokens": response.usage.total_tokens if response.usage else 0
        }
        
        return {
            "content": text_content,
            "tool_calls": tool_calls,
            "usage": usage
        }


    async def embeddings(self, texts: List[str], **kwargs) -> List[List[float]]:
        model = kwargs.get("model", "text-embedding-v2")
        try:
            response = await self.client.embeddings.create(input=texts, model=model)
            return [data.embedding for data in response.data]
        except Exception as e:
            logger.error(f"Error generating embeddings: {e}", exc_info=True)
            raise e

    async def tokenize(self, text: str, **kwargs) -> List[int]:
        # Simple placeholder tokenization for Phase 1
        return [ord(c) for c in text]

    async def models(self) -> List[str]:
        try:
            models_list = await self.client.models.list()
            return [m.id for m in models_list.data]
        except Exception as e:
            logger.error(f"Error listing models: {e}", exc_info=True)
            return [self.default_model]
