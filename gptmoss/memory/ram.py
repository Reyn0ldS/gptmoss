import uuid
import time
from typing import List, Dict, Any, Optional
from gptmoss.interfaces.memory import MemoryProvider

class RAMMemoryProvider(MemoryProvider):
    """
    In-Memory (RAM) storage for MOSS memory.
    Useful for Phase 1 and fast local testing.
    """
    def __init__(self):
        self.memories: List[Dict[str, Any]] = []

    async def search(self, query: str, limit: int = 5, **kwargs) -> List[Dict[str, Any]]:
        """Find matching memory items using simple substring checking."""
        results = []
        q_lower = query.lower()
        for item in self.memories:
            if q_lower in str(item["value"]).lower():
                results.append(item)
            if len(results) >= limit:
                break
        
        # If no direct matches, return the most recent items
        if not results:
            results = self.memories[-limit:]
            
        return results

    async def store(self, value: Any, metadata: Optional[Dict[str, Any]] = None, **kwargs) -> str:
        """Store item in list."""
        key = str(uuid.uuid4())
        self.memories.append({
            "id": key,
            "value": value,
            "metadata": metadata or {},
            "provenance": kwargs.get("provenance") or {"source": "runtime"},
            "validated": bool(kwargs.get("validated", False)),
            "created_at": time.time(),
            "expires_at": time.time() + kwargs["ttl_seconds"] if kwargs.get("ttl_seconds") else None,
        })
        return key

    async def update(self, key: str, value: Any, metadata: Optional[Dict[str, Any]] = None, **kwargs) -> bool:
        for item in self.memories:
            if item["id"] == key:
                item.update({
                    "value": value, "metadata": metadata or {},
                    "provenance": kwargs.get("provenance") or item.get("provenance", {"source": "gui"}),
                    "validated": bool(kwargs.get("validated", item.get("validated", False))),
                    "expires_at": time.time() + kwargs["ttl_seconds"] if kwargs.get("ttl_seconds") else None,
                })
                return True
        return False

    async def validate(self, key: str, validated_by: str = "system") -> bool:
        for item in self.memories:
            if item["id"] == key:
                item["validated"] = True
                item["validated_by"] = validated_by
                item["validated_at"] = time.time()
                return True
        return False

    async def delete(self, key: str, **kwargs) -> bool:
        """Delete matching memory item."""
        for idx, item in enumerate(self.memories):
            if item["id"] == key:
                self.memories.pop(idx)
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
        """Stub compression for RAM."""
        pass
