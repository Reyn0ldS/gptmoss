import os
import json
import uuid
import logging
import re
import time
from typing import List, Dict, Any, Optional
from gptmoss.interfaces.memory import MemoryProvider

logger = logging.getLogger("gptmoss.memory.json_store")

class JSONMemoryProvider(MemoryProvider):
    """
    JSON File-backed persistent storage for MOSS memory.
    Ensures memories survive application restarts by writing to a local file.
    """
    def __init__(self, file_path: str = "workspace/memories.json"):
        self.file_path = os.path.abspath(file_path)
        # Ensure workspace directory exists
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        self.memories: List[Dict[str, Any]] = []
        self.session_memories: Dict[str, List[Dict[str, Any]]] = {}
        self._index: Dict[str, set[str]] = {}
        self._load_from_disk()

    @staticmethod
    def _tokens(value: Any) -> set[str]:
        return {token for token in re.findall(r"[\\w'-]+", str(value).lower()) if len(token) > 1}

    @staticmethod
    def _is_expired(item: Dict[str, Any], now: Optional[float] = None) -> bool:
        expires_at = item.get("expires_at")
        return expires_at is not None and float(expires_at) <= (now or time.time())

    def _normalise(self, item: Dict[str, Any], legacy: bool = False) -> Dict[str, Any]:
        metadata = dict(item.get("metadata") or {})
        created_at = float(item.get("created_at", metadata.pop("created_at", time.time())))
        expires_at = item.get("expires_at", metadata.pop("expires_at", None))
        if expires_at is None and metadata.get("ttl_seconds") is not None:
            expires_at = created_at + float(metadata.pop("ttl_seconds"))
        return {
            "id": item.get("id") or str(uuid.uuid4()),
            "value": item.get("value", ""),
            "metadata": metadata,
            "created_at": created_at,
            "expires_at": float(expires_at) if expires_at is not None else None,
            "provenance": item.get("provenance") or metadata.pop("provenance", {"source": "legacy" if legacy else "runtime"}),
            "validated": bool(item.get("validated", True if legacy else metadata.pop("validated", False))),
        }

    def _rebuild_index(self) -> None:
        self._index = {}
        for item in self.memories:
            for token in self._tokens(item["value"]):
                self._index.setdefault(token, set()).add(item["id"])

    def _prune_expired(self) -> bool:
        before = len(self.memories)
        self.memories = [item for item in self.memories if not self._is_expired(item)]
        if len(self.memories) != before:
            self._rebuild_index()
            return True
        return False

    def _load_from_disk(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    raw_memories = json.load(f)
                self.memories = [self._normalise(item, legacy="validated" not in item) for item in raw_memories]
                if self._prune_expired():
                    self._save_to_disk()
                self._rebuild_index()
                logger.info(f"Loaded {len(self.memories)} memories from {self.file_path}")
            except Exception as e:
                logger.error(f"Failed to load memories from file: {e}")
                self.memories = []
        else:
            self.memories = []

    def _save_to_disk(self):
        try:
            temp_path = self.file_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self.memories, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, self.file_path)
        except Exception as e:
            logger.error(f"Failed to save memories to file: {e}")

    async def search(self, query: str, limit: int = 5, session_id: Optional[str] = None, include_pending: bool = False, **kwargs) -> List[Dict[str, Any]]:
        """Search validated persistent memories plus short-lived session memory."""
        if self._prune_expired():
            self._save_to_disk()
        query_tokens = self._tokens(query)
        candidates = list(self.session_memories.get(session_id, [])) if session_id else []
        candidate_ids = set().union(*(self._index.get(token, set()) for token in query_tokens)) if query_tokens else set()
        for item in self.memories:
            if item["id"] in candidate_ids or not query_tokens:
                if item["validated"] or include_pending:
                    candidates.append(item)

        def score(item: Dict[str, Any]) -> tuple[float, float]:
            terms = self._tokens(item["value"])
            overlap = len(query_tokens & terms) / max(1, len(query_tokens))
            phrase_bonus = 1.0 if query.lower() in str(item["value"]).lower() else 0.0
            return (overlap + phrase_bonus, float(item.get("created_at", 0)))

        return sorted(candidates, key=score, reverse=True)[:limit]

    async def store(self, value: Any, metadata: Optional[Dict[str, Any]] = None, provenance: Optional[Dict[str, Any]] = None, validated: bool = False, ttl_seconds: Optional[float] = None, **kwargs) -> str:
        """Store a persistent memory pending explicit validation by default."""
        created_at = time.time()
        key = str(uuid.uuid4())
        self.memories.append(self._normalise({
            "id": key, "value": value, "metadata": metadata or {}, "created_at": created_at,
            "expires_at": created_at + ttl_seconds if ttl_seconds is not None else None,
            "provenance": provenance or {"source": "runtime"}, "validated": validated,
        }))
        self._rebuild_index()
        self._save_to_disk()
        return key

    async def store_session(self, session_id: str, value: Any, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Store short-term memory that is never written to disk."""
        key = str(uuid.uuid4())
        self.session_memories.setdefault(session_id, []).append(self._normalise({
            "id": key, "value": value, "metadata": metadata or {}, "validated": True,
            "provenance": {"source": "session"},
        }))
        return key

    async def validate(self, key: str, validated_by: str = "system") -> bool:
        for item in self.memories:
            if item["id"] == key:
                item["validated"] = True
                item["validated_by"] = validated_by
                item["validated_at"] = time.time()
                self._save_to_disk()
                return True
        return False

    async def update(self, key: str, value: Any, metadata: Optional[Dict[str, Any]] = None, provenance: Optional[Dict[str, Any]] = None, validated: bool = False, ttl_seconds: Optional[float] = None, **kwargs) -> bool:
        for item in self.memories:
            if item["id"] == key:
                item["value"] = value
                item["metadata"] = metadata or {}
                item["provenance"] = provenance or item.get("provenance") or {"source": "gui"}
                item["validated"] = validated
                item["expires_at"] = time.time() + ttl_seconds if ttl_seconds is not None else None
                self._rebuild_index()
                self._save_to_disk()
                return True
        return False

    async def clear_session(self, session_id: str) -> None:
        self.session_memories.pop(session_id, None)

    async def delete(self, key: str, **kwargs) -> bool:
        """Delete matching memory item and save."""
        for idx, item in enumerate(self.memories):
            if item["id"] == key:
                self.memories.pop(idx)
                self._rebuild_index()
                self._save_to_disk()
                return True
        return False

    async def summarize(self, **kwargs) -> str:
        """Provide simple summary of all stored memories."""
        valid_memories = [item for item in self.memories if item["validated"] and not self._is_expired(item)]
        if not valid_memories:
            return "No memories stored."
        lines = []
        for item in valid_memories:
            lines.append(f"- {item['value']}")
        return "\n".join(lines)

    async def compress(self, **kwargs) -> None:
        """Stub compression."""
        pass
