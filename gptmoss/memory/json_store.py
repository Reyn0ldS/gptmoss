import os
import json
import uuid
import logging
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
        self._load_from_disk()

    def _load_from_disk(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self.memories = json.load(f)
                logger.info(f"Loaded {len(self.memories)} memories from {self.file_path}")
            except Exception as e:
                logger.error(f"Failed to load memories from file: {e}")
                self.memories = []
        else:
            self.memories = []

    def _save_to_disk(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.memories, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save memories to file: {e}")

    async def search(self, query: str, limit: int = 5, **kwargs) -> List[Dict[str, Any]]:
        """Find matching memory items using simple substring checking."""
        results = []
        q_lower = query.lower()
        for item in self.memories:
            if q_lower in str(item["value"]).lower():
                results.append(item)
            if len(results) >= limit:
                break
        
        if not results:
            results = self.memories[-limit:]
            
        return results

    async def store(self, value: Any, metadata: Optional[Dict[str, Any]] = None, **kwargs) -> str:
        """Store item in JSON list and persist to disk."""
        key = str(uuid.uuid4())
        self.memories.append({
            "id": key,
            "value": value,
            "metadata": metadata or {}
        })
        self._save_to_disk()
        return key

    async def delete(self, key: str, **kwargs) -> bool:
        """Delete matching memory item and save."""
        for idx, item in enumerate(self.memories):
            if item["id"] == key:
                self.memories.pop(idx)
                self._save_to_disk()
                return True
        return False

    async def summarize(self, **kwargs) -> str:
        """Provide simple summary of all stored memories."""
        if not self.memories:
            return "No memories stored."
        lines = []
        for item in self.memories:
            lines.append(f"- {item['value']}")
        return "\n".join(lines)

    async def compress(self, **kwargs) -> None:
        """Stub compression."""
        pass
