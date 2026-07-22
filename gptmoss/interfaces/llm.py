from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class LLMProvider(ABC):
    """
    Interface for LLM Providers.
    All LLM client plugins must implement this class.
    """

    @abstractmethod
    async def completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Send a chat completion request to the LLM.
        
        Args:
            messages: List of chat messages in standard format: [{"role": "user", "content": "..."}]
            tools: Optional list of tool descriptions in JSON schema format.
            tool_choice: Optional tool choice setting.
            **kwargs: Extra arguments passed to the completion API.
            
        Returns:
            Dict containing:
                - "content": Optional[str]
                - "tool_calls": Optional[List[Dict[str, Any]]]
                - "usage": Dict[str, int] (prompt_tokens, completion_tokens, total_tokens)
        """
        pass

    @abstractmethod
    async def embeddings(self, texts: List[str], **kwargs) -> List[List[float]]:
        """
        Generate vector embeddings for a list of input texts.
        
        Args:
            texts: List of strings to embed.
            **kwargs: Additional parameters.
            
        Returns:
            List of embedding vectors (list of floats).
        """
        pass

    @abstractmethod
    async def tokenize(self, text: str, **kwargs) -> List[int]:
        """
        Tokenize a text string.
        
        Args:
            text: String to tokenize.
            **kwargs: Additional parameters.
            
        Returns:
            List of token IDs.
        """
        pass

    @abstractmethod
    async def models(self) -> List[str]:
        """
        List available models for this provider.
        
        Returns:
            List of model names/identifiers.
        """
        pass
