"""Pure context and response policies for OpenAI-compatible Qwen endpoints."""

import hashlib
import html
import json
import re
from typing import Any, Dict, List


class ContextWindowPolicy:
    @staticmethod
    def is_limit_error(error: Exception) -> bool:
        text = (error.__class__.__name__ + " " + str(error)).lower()
        return any(marker in text for marker in (
            "context length", "context_length", "maximum context", "max context",
            "too many tokens", "token limit", "prompt is too long", "input length",
        ))

    @staticmethod
    def message_chars(messages: List[Dict[str, Any]]) -> int:
        return sum(len(json.dumps(message, ensure_ascii=False, default=str)) for message in messages)

    @classmethod
    def compact(cls, messages: List[Dict[str, Any]], target_chars: int) -> List[Dict[str, Any]]:
        items = [dict(message) for message in messages]
        if cls.message_chars(items) <= target_chars:
            return items
        first = items[0] if items[0].get("role") == "system" else None
        body, omitted = (items[1:] if first else items), 0
        while len(body) > 4 and cls.message_chars(([first] if first else []) + body) > target_chars:
            body.pop(0)
            omitted += 1
            while body and body[0].get("role") == "tool":
                body.pop(0)
                omitted += 1
        compacted = ([first] if first else [])
        if omitted:
            compacted.append({"role": "system", "content": (
                f"{omitted} older context message(s) were compacted after the provider "
                "reported its context limit. Durable execution state and the current plan remain authoritative."
            )})
        compacted.extend(body)
        while cls.message_chars(compacted) > target_chars:
            candidates = [(len(str(message.get("content") or "")), index)
                          for index, message in enumerate(compacted)
                          if message is not first and str(message.get("content") or "")]
            if not candidates:
                break
            _, index = max(candidates)
            content = str(compacted[index].get("content") or "")
            empty = [dict(message) for message in compacted]
            empty[index]["content"] = ""
            available = max(1, target_chars - cls.message_chars(empty) - 64)
            if len(content) <= available:
                break
            notice = "\n… [message compacted at provider context boundary] …\n"
            payload = max(0, available - len(notice))
            head, tail = (payload * 2) // 3, payload - ((payload * 2) // 3)
            compacted[index]["content"] = content[:head] + notice + (content[-tail:] if tail else "")
        return compacted


class ToolCallParser:
    @classmethod
    def parse_response(cls, response) -> Dict[str, Any]:
        message = response.choices[0].message
        content, calls = message.content, None
        if message.tool_calls:
            calls = []
            for call in message.tool_calls:
                try:
                    arguments = json.loads(call.function.arguments)
                except Exception:
                    arguments = call.function.arguments
                calls.append({"id": call.id, "type": "function",
                              "function": {"name": call.function.name, "arguments": arguments}})
        elif content:
            calls = cls.parse_text(content) or None
            if calls:
                content = None
        usage = {"prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                 "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                 "total_tokens": response.usage.total_tokens if response.usage else 0}
        return {"content": content, "tool_calls": calls, "usage": usage}

    @staticmethod
    def parse_text(content: str) -> List[Dict[str, Any]]:
        blocks = re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", content,
                            flags=re.DOTALL | re.IGNORECASE)
        if not blocks:
            candidates = [str(content or "").strip()]
            candidates.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", str(content or ""),
                                         flags=re.DOTALL | re.IGNORECASE))
            first, last = str(content or "").find("{"), str(content or "").rfind("}")
            if first >= 0 and last > first:
                candidates.append(str(content or "")[first:last + 1])
            blocks = list(dict.fromkeys(item for item in candidates if item))
        calls, seen = [], set()
        for block in blocks:
            name, arguments = "", {}
            try:
                candidate = json.loads(block.strip())
            except (TypeError, ValueError):
                candidate = None
            if isinstance(candidate, dict):
                candidate = candidate.get("tool_call", candidate)
                candidate = candidate if isinstance(candidate, dict) else {}
                function = candidate.get("function") if isinstance(candidate.get("function"), dict) else candidate
                name, arguments = str(function.get("name") or "").strip(), function.get("arguments", {})
            if not name:
                function = re.search(r"<function=([^>\r\n]+)>\s*(.*?)\s*</function>", block,
                                     flags=re.DOTALL | re.IGNORECASE)
                if function:
                    name = html.unescape(function.group(1)).strip().strip(chr(34) + chr(39))
                    arguments = {}
                    for parameter in re.finditer(r"<parameter=([^>\r\n]+)>\s*(.*?)\s*</parameter>",
                                                 function.group(2), flags=re.DOTALL | re.IGNORECASE):
                        key = html.unescape(parameter.group(1)).strip().strip(chr(34) + chr(39))
                        raw = html.unescape(parameter.group(2)).strip()
                        try:
                            arguments[key] = json.loads(raw)
                        except (TypeError, ValueError):
                            arguments[key] = raw
            if not name:
                continue
            arguments = arguments if isinstance(arguments, dict) else {}
            stable = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False,
                                sort_keys=True, separators=(",", ":"), default=str)
            digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]
            if digest in seen:
                continue
            seen.add(digest)
            calls.append({"id": f"qwen-text-{digest}", "type": "function",
                          "function": {"name": name, "arguments": arguments}})
        return calls
