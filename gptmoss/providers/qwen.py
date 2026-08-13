import hashlib
import inspect
import json
import os
import logging
import asyncio
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
        default_model: str = "qwen-turbo",
        ssl_verify: bool = True,
        ssl_cert_path: str = "",
    ):
        # Fall back to env variables or defaults
        self.api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or "mock-key"
        # DashScope OpenAI-compatible endpoint or local host
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        self.default_model = default_model
        self.ssl_verify = bool(ssl_verify)
        self.ssl_cert_path = ssl_cert_path or ""
        self.vision_mode = "auto"
        self.supports_vision = self._infer_vision(default_model)
        self._native_tools_supported: Optional[bool] = None
        self._learned_context_chars: Optional[int] = None
        self._retired_clients = []
        self._close_tasks: set[asyncio.Task] = set()
        
        logger.info(f"Initializing QwenProvider calling base_url={self.base_url} with default_model={self.default_model}")
        import httpx
        # Certificate validation is secure by default. A local/self-signed
        # endpoint can still be enabled explicitly through settings.
        verify_value = ssl_cert_path if ssl_verify and ssl_cert_path else bool(ssl_verify)
        http_client = httpx.AsyncClient(verify=verify_value)
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

    def _retire_client(self, client) -> None:
        if client is None:
            return
        self._retired_clients.append(client)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def close_retired() -> None:
            try:
                await client.close()
            finally:
                if client in self._retired_clients:
                    self._retired_clients.remove(client)

        task = loop.create_task(close_retired())
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)

    async def close(self) -> None:
        """Close the active SDK client and every superseded transport."""
        pending_closes = list(self._close_tasks)
        if pending_closes:
            await asyncio.gather(*pending_closes, return_exceptions=True)
        clients = list({id(client): client for client in [self.client, *self._retired_clients]}.values())
        self._retired_clients = []
        for client in clients:
            try:
                await client.close()
            except Exception:
                logger.warning("Unable to close an LLM HTTP client cleanly.", exc_info=True)

    def update_config(self, api_key: str, base_url: str, ssl_verify: bool = True, ssl_cert_path: str = "", model_name: str = "qwen-turbo"):
        self.api_key = api_key
        self.base_url = base_url
        self.default_model = model_name
        self.ssl_verify = bool(ssl_verify)
        self.ssl_cert_path = ssl_cert_path or ""
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
        previous_client = getattr(self, "client", None)
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, http_client=http_client,
                                  max_retries=0, timeout=90.0)
        self._retire_client(previous_client)
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

    @staticmethod
    async def _notify_text_delta(callback, text: str) -> None:
        if not callback or not text:
            return
        result = callback(text)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _extract_usage(response: Any) -> Dict[str, int]:
        """Normalize usage from SDK objects and assembled streaming dictionaries."""
        usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
        if isinstance(usage, dict):
            value = lambda key: int(usage.get(key) or 0)
        else:
            value = lambda key: int(getattr(usage, key, 0) or 0)
        return {
            "prompt_tokens": value("prompt_tokens"),
            "completion_tokens": value("completion_tokens"),
            "total_tokens": value("total_tokens"),
        }

    async def _consume_chat_stream(self, stream, on_text_delta=None) -> Dict[str, Any]:
        """Assemble an OpenAI-compatible stream into the provider response dict."""
        content_parts: List[str] = []
        tool_acc: Dict[int, Dict[str, str]] = {}
        usage = self._extract_usage(None)
        async for chunk in stream:
            chunk_usage = self._extract_usage(chunk)
            if any(chunk_usage.values()):
                usage = chunk_usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                content_parts.append(piece)
                await self._notify_text_delta(on_text_delta, piece)
            for call in getattr(delta, "tool_calls", None) or []:
                index = int(getattr(call, "index", 0) or 0)
                entry = tool_acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if getattr(call, "id", None):
                    entry["id"] = call.id
                function = getattr(call, "function", None)
                if function is not None:
                    if getattr(function, "name", None):
                        entry["name"] += function.name
                    if getattr(function, "arguments", None):
                        entry["arguments"] += function.arguments
        content = "".join(content_parts) or None
        calls = None
        if tool_acc:
            calls = []
            for index in sorted(tool_acc):
                entry = tool_acc[index]
                raw_args = entry["arguments"] or "{}"
                try:
                    arguments = json.loads(raw_args)
                except Exception:
                    arguments = raw_args
                calls.append({
                    "id": entry["id"] or f"stream-{index}",
                    "type": "function",
                    "function": {"name": entry["name"], "arguments": arguments},
                })
        elif content:
            parsed = ToolCallParser.parse_text(content) or None
            if parsed:
                calls = parsed
                content = None
        return {"content": content, "tool_calls": calls, "usage": usage}

    async def _create_with_context_recovery(self, arguments: Dict[str, Any], on_text_delta=None):
        """Learn a provider's effective context size and recover without losing task state."""
        request = dict(arguments)
        request.pop("stream", None)
        messages = [dict(item) for item in request.get("messages") or []]
        if self._learned_context_chars:
            messages = self._compact_messages(messages, self._learned_context_chars)
        for attempt in range(5):
            request["messages"] = messages
            try:
                if on_text_delta is not None:
                    stream_request = dict(request)
                    stream_request.setdefault("stream_options", {"include_usage": True})
                    try:
                        stream = await self.client.chat.completions.create(
                            **stream_request, stream=True
                        )
                    except Exception as stream_error:
                        error_text = str(stream_error).lower()
                        if not any(
                            marker in error_text
                            for marker in ("stream_options", "include_usage")
                        ):
                            raise
                        # Some OpenAI-compatible servers stream correctly but
                        # reject the optional usage extension.
                        stream_request.pop("stream_options", None)
                        stream = await self.client.chat.completions.create(
                            **stream_request, stream=True
                        )
                    return await self._consume_chat_stream(stream, on_text_delta)
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
        on_text_delta = kwargs.pop("on_text_delta", None)
        kwargs.pop("stream", None)
        
        if not tools:
            # Standard chat completion without tool schema
            logger.debug(f"Calling LLM: {model} with {len(messages)} messages (no tools)")
            try:
                response = await self._create_with_context_recovery({
                    "model": model,
                    "messages": messages,
                    **kwargs,
                }, on_text_delta=on_text_delta)
                if isinstance(response, dict) and "content" in response:
                    return response
                return self._parse_openai_response(response)
            except Exception as e:
                self._log_completion_error(e)
                raise e

        if self._native_tools_supported is False:
            return await self._prompt_based_tool_calling(
                messages, tools, model, on_text_delta=on_text_delta, **kwargs
            )

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
            response = await self._create_with_context_recovery(
                client_kwargs, on_text_delta=on_text_delta
            )
            parsed = (
                response
                if isinstance(response, dict) and "content" in response
                else self._parse_openai_response(response)
            )
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
                return await self._prompt_based_tool_calling(
                    messages, tools, model, on_text_delta=on_text_delta, **kwargs
                )
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
        
        on_text_delta = kwargs.pop("on_text_delta", None)
        kwargs.pop("stream", None)
        # Make a standard chat completion call
        response = await self._create_with_context_recovery({
            "model": model,
            "messages": fallback_messages,
            **kwargs,
        }, on_text_delta=on_text_delta)
        pre_parsed_tool_calls = None
        if isinstance(response, dict) and "content" in response:
            content = response.get("content") or ""
            available_names = {
                tool.get("function", {}).get("name") for tool in tools
            }
            pre_parsed_tool_calls = [
                call for call in (response.get("tool_calls") or [])
                if call.get("function", {}).get("name") in available_names
            ] or None
        else:
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
        tool_calls = pre_parsed_tool_calls
        text_content = None if pre_parsed_tool_calls else content
        
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
            
        usage = self._extract_usage(response)
        
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
        """Interface placeholder: character ordinals, not a model tokenizer."""
        return [ord(c) for c in text]

    async def models(self) -> List[str]:
        try:
            models_list = await self.client.models.list()
            return [m.id for m in models_list.data]
        except Exception as e:
            logger.error(f"Error listing models: {e}", exc_info=True)
            return [self.default_model]
