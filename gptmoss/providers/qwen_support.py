"""Pure context and response policies for OpenAI-compatible Qwen endpoints."""

import hashlib
import html
import json
import re
import math
from typing import Any, Dict, List


class ContextWindowPolicy:
    # JSON, French prose and code vary widely.  Counting UTF-8 bytes at three
    # bytes per token is intentionally conservative and does not require a
    # model-specific tokenizer to be downloaded for offline operation.
    BYTES_PER_TOKEN = 3

    @staticmethod
    def is_limit_error(error: Exception) -> bool:
        text = (error.__class__.__name__ + " " + str(error)).lower()
        return any(marker in text for marker in (
            "context length", "context_length", "maximum context", "max context",
            "too many tokens", "token limit", "prompt is too long", "input length",
        ))

    @classmethod
    def _token_projection(cls, value: Any) -> Any:
        """Replace Base64 image bytes by a conservative fixed token charge."""
        if isinstance(value, list):
            return [cls._token_projection(item) for item in value]
        if isinstance(value, dict):
            projected = {
                key: cls._token_projection(item) for key, item in value.items()
            }
            image_url = value.get("image_url")
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "")
                if url.startswith("data:image/"):
                    projected["image_url"] = {
                        **image_url,
                        # Approximately 4k input tokens per image without
                        # downloading a model-specific vision tokenizer.
                        "url": "[local image payload]" + ("x" * 12_288),
                    }
            return projected
        if isinstance(value, str) and value.startswith("data:image/"):
            return "[local image payload]" + ("x" * 12_288)
        return value

    @staticmethod
    def message_chars(messages: List[Dict[str, Any]]) -> int:
        projected = ContextWindowPolicy._token_projection(messages)
        return len(json.dumps(projected, ensure_ascii=False, default=str))

    @classmethod
    def estimate_tokens(cls, value: Any) -> int:
        encoded = json.dumps(
            cls._token_projection(value), ensure_ascii=False, default=str
        ).encode("utf-8")
        return max(1, math.ceil(len(encoded) / cls.BYTES_PER_TOKEN))

    @staticmethod
    def limit_tokens(error: Exception) -> int | None:
        text = str(error)
        patterns = (
            r"maximum context length is\s*([\d, _]+)\s*tokens",
            r"maximum context(?: length)?[^\d]{0,30}([\d, _]+)\s*tokens",
            r"context_length[^\d]{0,20}([\d, _]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                try:
                    return int(re.sub(r"[^0-9]", "", match.group(1)))
                except ValueError:
                    continue
        return None

    @classmethod
    def compact_to_tokens(
        cls,
        messages: List[Dict[str, Any]],
        target_tokens: int,
    ) -> List[Dict[str, Any]]:
        target = max(256, int(target_tokens))
        if cls.estimate_tokens(messages) <= target:
            return [dict(message) for message in messages]
        # Convert the token envelope to a conservative character envelope,
        # then tighten it if JSON structure still pushes the estimate over.
        target_chars = max(512, target * 2)
        compacted = cls.compact(messages, target_chars)
        for _ in range(8):
            observed = cls.estimate_tokens(compacted)
            if observed <= target:
                break
            target_chars = max(256, int(target_chars * target / max(1, observed) * 0.92))
            reduced = cls.compact(messages, target_chars)
            if reduced == compacted:
                break
            compacted = reduced
        return compacted

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
            digest_candidates = [
                item for item in candidates
                if "Pinned local source evidence" not in str(compacted[item[1]].get("content") or "")
            ]
            if not digest_candidates:
                if first is not None:
                    break
                _, index = max(candidates)
            else:
                _, index = max(digest_candidates)
            original_content = compacted[index].get("content")
            if isinstance(original_content, list):
                compacted[index]["content"] = (
                    "[multimodal attachment omitted at provider context boundary]"
                )
                continue
            content = str(original_content or "")
            empty = [dict(message) for message in compacted]
            empty[index]["content"] = ""
            available = max(1, target_chars - cls.message_chars(empty) - 64)
            if len(content) <= available:
                break
            notice = "\n… [message compacted at provider context boundary] …\n"
            payload = max(0, available - len(notice))
            head, tail = (payload * 2) // 3, payload - ((payload * 2) // 3)
            replacement = (
                content[:head] + notice + (content[-tail:] if tail else "")
                if payload else ""
            )
            compacted[index]["content"] = (
                "" if len(replacement) >= len(content) else replacement
            )
        # A generated system contract can itself contain a very large source
        # manifest. Preserve its authoritative beginning and latest suffix,
        # but never allow it to defeat the provider-wide envelope.
        if cls.message_chars(compacted) > target_chars and first is not None:
            first_index = next(
                (index for index, message in enumerate(compacted) if message is first),
                0,
            )
            content = str(compacted[first_index].get("content") or "")
            empty = [dict(message) for message in compacted]
            empty[first_index]["content"] = ""
            available = max(1, target_chars - cls.message_chars(empty) - 64)
            notice = "\n… [system contract compacted at provider context boundary] …\n"
            payload = max(0, available - len(notice))
            head = (payload * 3) // 4
            tail = payload - head
            compacted[first_index]["content"] = (
                content[:head] + notice + (content[-tail:] if tail else "")
            )
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
