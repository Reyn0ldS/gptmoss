import os
import logging
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI
from gptmoss.interfaces.llm import LLMProvider

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
        
        logger.info(f"Initializing QwenProvider calling base_url={self.base_url} with default_model={self.default_model}")
        import httpx
        # Bypass SSL verification to support local self-signed certificates
        http_client = httpx.AsyncClient(verify=False)
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, http_client=http_client)

    def update_config(self, api_key: str, base_url: str, ssl_verify: bool = False, ssl_cert_path: str = "", model_name: str = "qwen-turbo"):
        self.api_key = api_key
        self.base_url = base_url
        self.default_model = model_name
        
        import httpx
        if ssl_verify:
            verify_value = ssl_cert_path if ssl_cert_path else True
        else:
            verify_value = False
            
        http_client = httpx.AsyncClient(verify=verify_value)
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, http_client=http_client)
        logger.info(f"QwenProvider config updated. base_url={self.base_url}, ssl_verify={ssl_verify}, ssl_cert_path={ssl_cert_path}")

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
                response = await self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **kwargs
                )
                return self._parse_openai_response(response)
            except Exception as e:
                logger.error(f"Error in LLM completion: {e}", exc_info=True)
                raise e

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
            response = await self.client.chat.completions.create(**client_kwargs)
            return self._parse_openai_response(response)
        except Exception as e:
            err_msg = str(e).lower()
            # If native tool calling fails because auto tool choice is disabled on remote server, fall back
            if "tool_choice" in err_msg or "tool-call-parser" in err_msg or "tool_call" in err_msg or "400" in err_msg:
                logger.warning("Native tool calling failed/not supported by remote endpoint, falling back to prompt-based tool calling.")
                return await self._prompt_based_tool_calling(messages, tools, model, **kwargs)
            else:
                logger.error(f"Error in LLM completion: {e}", exc_info=True)
                raise e

    def _parse_openai_response(self, response) -> Dict[str, Any]:
        choice = response.choices[0]
        message = choice.message
        
        tool_calls = None
        if message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                import json
                try:
                    args = json.loads(tc.function.arguments)
                except Exception:
                    args = tc.function.arguments
                    
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": args
                    }
                })
                
        usage = {
            "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
            "completion_tokens": response.usage.completion_tokens if response.usage else 0,
            "total_tokens": response.usage.total_tokens if response.usage else 0
        }
        
        return {
            "content": message.content,
            "tool_calls": tool_calls,
            "usage": usage
        }

    async def _prompt_based_tool_calling(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model: str,
        **kwargs
    ) -> Dict[str, Any]:
        import json
        import random
        import string
        
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
        response = await self.client.chat.completions.create(
            model=model,
            messages=fallback_messages,
            **kwargs
        )
        
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
                    
                    if not isinstance(tool_args, dict):
                        tool_args = {}
                    
                    # Verify that tool name matches available tools
                    if tool_name in [t.get("function", {}).get("name") for t in tools]:
                        call_id = "call_" + "".join(random.choices(string.ascii_letters + string.digits, k=16))
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
