from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class MemoryProvider(ABC):
    """
    Interface for Memory Providers.
    Allows loading, searching, and storing conversation/knowledge memories.
    """

    @abstractmethod
    async def search(self, query: str, limit: int = 5, **kwargs) -> List[Dict[str, Any]]:
        """
        Search memory for relevant logs, facts, or conversations.
        
        Args:
            query: The search query (text or semantic query).
            limit: Maximum number of results to return.
            **kwargs: Extra parameters.
            
        Returns:
            List of matching records.
        """
        pass

    @abstractmethod
    async def store(self, value: Any, metadata: Optional[Dict[str, Any]] = None, **kwargs) -> str:
        """
        Store a new memory item.
        
        Args:
            value: The content to remember.
            metadata: Optional dictionary of associated metadata.
            **kwargs: Extra parameters.
            
        Returns:
            A unique key/ID representing the stored item.
        """
        pass

    @abstractmethod
    async def delete(self, key: str, **kwargs) -> bool:
        """
        Delete a memory item by its unique ID/key.
        
        Args:
            key: Unique key to delete.
            **kwargs: Extra parameters.
            
        Returns:
            True if deletion was successful, False otherwise.
        """
        pass

    @abstractmethod
    async def summarize(self, **kwargs) -> str:
        """
        Summarize the memories managed by this provider.
        """
        pass

    @abstractmethod
    async def compress(self, **kwargs) -> None:
        """
        Compress or optimize memories (e.g. prune duplicates, consolidate old context).
        """
        pass
