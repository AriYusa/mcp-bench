"""LLM Factory Module.

This module handles model configuration and factory creation for different LLM 
providers, supporting multiple model types and deployment configurations.

Classes:
    ModelConfig: Configuration container for specific models
    LLMFactory: Factory for creating LLM provider instances
"""

import os
from typing import Dict, Any
from .provider import LLMProvider
import config.config_loader as config_loader


class ModelConfig:
    """Configuration container for a specific model.
    
    Stores model-specific configuration including provider type,
    API credentials, and deployment details.
    
    Attributes:
        name: Model name identifier
        config: Dictionary of additional configuration parameters
        
    Example:
        >>> config = ModelConfig("gpt-4o", model_name="azure/gpt-4o")
    """
    
    def __init__(self, name: str, **kwargs: Any) -> None:
        """Initialize model configuration.
        
        Args:
            name: Model name identifier
            **kwargs: Additional configuration parameters
        """
        self.name: str = name
        self.config: Dict[str, Any] = kwargs


class LLMFactory:
    """Factory for creating LLM providers for different models.
    
    This class manages model configurations and creates appropriate
    LLM provider instances based on the model type and configuration.
    
    Example:
        >>> configs = LLMFactory.get_model_configs()
        >>> provider = await LLMFactory.create_llm_provider(configs["gpt-4o"])
    """
    
    @staticmethod
    def get_model_configs() -> Dict[str, ModelConfig]:
        """Get all available model configurations from environment variables.
        
        Scans environment variables to detect available API keys and endpoints,
        then creates ModelConfig instances for each available model.
        
        Returns:
            Dictionary mapping model names to ModelConfig instances
        """
        configs = {}
        
        configs["claude-sonnet-4-5"] = ModelConfig(
                name="claude-sonnet-4-5",
                model_name="anthropic/claude-sonnet-4-5-20250929"
            )
        
        # configs["claude-3-haiku"] = ModelConfig(
        #         name="claude-3-haiku",
        #         model_name="anthropic/claude-3-haiku-20240307"
        # )

        return configs
    
    @staticmethod
    async def create_llm_provider(model_config: ModelConfig) -> LLMProvider:
        return LLMProvider(model_name=model_config.config['model_name'])