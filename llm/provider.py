"""LLM Provider Module.

This module handles all interactions with Azure OpenAI and other LLM services,
providing a unified interface for model communication with retry logic and
error handling.

Classes:
    LLMProvider: Universal LLM provider supporting multiple model types
"""

import asyncio
import json
import logging
from typing import Any, Optional, Tuple, Set, Type
import json_repair
import litellm
from pydantic import BaseModel

litellm.callbacks = ["langfuse_otel"]


logger = logging.getLogger(__name__)


class LLMProvider:
    """Universal LLM provider using LiteLLM for multiple model types and providers.
    
    This class provides a unified interface for interacting with various LLM
    providers via LiteLLM, with built-in retry logic, error handling, and
    token management.
    
    Attributes:
        model_name: Name of the model to use (LiteLLM format)
        
    Example:
        >>> provider = LLMProvider("anthropic/claude-sonnet-4-5-20250929")
        >>> response = await provider.get_completion("You are helpful", "Hello", 100)
    """
    
    def __init__(self, model_name: str) -> None:
        """Initialize the LLM provider.
        
        Args:
            model_name: Name of the model to use (LiteLLM format, e.g., 'azure/gpt-4o', 'anthropic/claude-sonnet-4-5-20250929')
        """
        self.model_name: str = model_name

    def _is_token_limit_error(self, error_message: str) -> bool:
        """Check if the error is related to token limits.
        
        Args:
            error_message: Error message to analyze
            
        Returns:
            True if the error is token limit related, False otherwise
        """
        error_lower = str(error_message).lower()
        token_limit_indicators = [
            "maximum context length",
            "context length",
            "token limit",
            "too many tokens",
            "exceeds maximum",
            "requested too many tokens"
        ]
        return any(indicator in error_lower for indicator in token_limit_indicators)
    
    def _is_content_filter_error(self, error_message: str) -> bool:
        """Check if the error is related to Azure content filtering.
        
        Args:
            error_message: Error message to analyze
            
        Returns:
            True if the error is content filter related, False otherwise
        """
        error_lower = str(error_message).lower()
        content_filter_indicators = [
            "content management policy",
            "content filtering policies",
            "content_filter",
            "jailbreak",
            "responsibleaipolicyviolation"
        ]
        return any(indicator in error_lower for indicator in content_filter_indicators)
    
    def _extract_requested_tokens(self, error_message: str) -> Tuple[Optional[int], Optional[int]]:
        """Extract requested and max tokens from error message.
        
        Args:
            error_message: Error message containing token information
            
        Returns:
            Tuple of (requested_tokens, max_allowed_tokens), either can be None
        """
        import re
        
        # Pattern to match: "you requested X tokens ... maximum context length is Y tokens"
        pattern = r"you requested (\d+) tokens.*maximum context length is (\d+) tokens"
        match = re.search(pattern, str(error_message), re.IGNORECASE)
        
        if match:
            requested = int(match.group(1))
            max_allowed = int(match.group(2))
            return requested, max_allowed
        
        # Alternative pattern: "X tokens in the messages, Y in the completion"
        pattern2 = r"(\d+) tokens.*in the messages.*(\d+) in the completion"
        match2 = re.search(pattern2, str(error_message), re.IGNORECASE)
        
        if match2:
            message_tokens = int(match2.group(1))
            completion_tokens = int(match2.group(2))
            return message_tokens + completion_tokens, None
            
        return None, None

    async def get_completion(self, system_prompt: str, user_prompt: str, max_tokens: int, return_usage: bool = False) -> Any:
        """Get a completion from the LLM with retry mechanism.
        
        Args:
            system_prompt: System message to set context
            user_prompt: User's input prompt
            max_tokens: Maximum tokens for the response
            return_usage: If True, returns tuple of (content, usage_dict)
            
        Returns:
            The LLM's response as a string, or tuple of (content, usage_dict) if return_usage=True
            
        Raises:
            Exception: If all retry attempts fail
        """
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        # Prepare LiteLLM parameters
        params = {
            "model": self.model_name,
            "messages": messages,
        }
        
        params["max_tokens"] = max_tokens
        
        # Simple retry mechanism: 3 attempts with exponential backoff
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                logger.info(f"Generating completion using {self.model_name} (attempt {attempt + 1}/{max_attempts}, max_tokens: {max_tokens})")
                
                response = await litellm.acompletion(**params)
                content = response.choices[0].message.content
                
                if content is None or content.strip() == "":
                    raise ValueError("Empty content received from LLM")
                
                if attempt > 0:
                    logger.info(f"Success on attempt {attempt + 1} for {self.model_name}")
                
                if return_usage and hasattr(response, 'usage') and response.usage:
                    usage_dict = {
                        'prompt_tokens': getattr(response.usage, 'prompt_tokens', 0),
                        'completion_tokens': getattr(response.usage, 'completion_tokens', 0),
                        'total_tokens': getattr(response.usage, 'total_tokens', 0)
                    }
                    return content.strip(), usage_dict
                
                return content.strip()
                
            except Exception as e:
                error_msg = str(e)
                logger.warning(f"Attempt {attempt + 1}/{max_attempts} failed for {self.model_name}: {e}")
                
                # Check for content filter errors - fail fast, no retries
                if self._is_content_filter_error(error_msg):
                    logger.info(f"Content filter error detected for {self.model_name}, failing fast")
                    raise e
                
                # For other errors, wait before retry (except last attempt)
                if attempt < max_attempts - 1:
                    wait_time = 2 ** attempt  # 1, 2 seconds
                    logger.info(f"Waiting {wait_time} seconds before retry...")
                    await asyncio.sleep(wait_time)
                else:
                    # Last attempt failed
                    raise e
        
        # This should never be reached due to the raise in the last attempt
        raise Exception("Unexpected completion flow")

    async def get_completion_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        response_model: Type[BaseModel],
    ) -> BaseModel:
        """Get a structured completion using json_schema response_format if supported.

        If the model supports response_schema (json_schema), the Pydantic model is passed
        directly as response_format and the result is validated against it.
        If not supported, falls back to plain text completion + JSON parsing + Pydantic
        validation so callers always receive a response_model instance.

        Args:
            system_prompt: System message to set context
            user_prompt: User's input prompt
            max_tokens: Maximum tokens for the response
            response_model: Pydantic model class that defines the expected output shape

        Returns:
            An instance of response_model populated with the LLM's output
        """
        # Check at runtime whether the model supports json_schema structured output
        try:
            model_supports_schema = litellm.supports_response_schema(model=self.model_name)
        except Exception:
            model_supports_schema = False

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        if model_supports_schema:
            params = {
                "model": self.model_name,
                "messages": messages,
                "max_tokens": max_tokens,
                "response_format": response_model,
            }

            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    logger.info(
                        f"Generating structured completion using {self.model_name} "
                        f"(attempt {attempt + 1}/{max_attempts}, max_tokens: {max_tokens})"
                    )
                    response = await litellm.acompletion(**params)
                    content = response.choices[0].message.content
                    if content is None or content.strip() == "":
                        raise ValueError("Empty content received from LLM")
                    if attempt > 0:
                        logger.info(f"Success on attempt {attempt + 1} for {self.model_name}")
                    return response_model.model_validate_json(content.strip())

                except Exception as e:
                    error_msg = str(e)
                    logger.warning(
                        f"Structured attempt {attempt + 1}/{max_attempts} failed for {self.model_name}: {e}"
                    )
                    if self._is_content_filter_error(error_msg):
                        raise
                    if attempt < max_attempts - 1:
                        wait_time = 2 ** attempt
                        logger.info(f"Waiting {wait_time} seconds before retry...")
                        await asyncio.sleep(wait_time)
                    else:
                        raise

            raise Exception("Unexpected structured completion flow")

        else:
            # Fallback: plain text completion + manual JSON parse + Pydantic validation
            logger.info(
                f"Model {self.model_name} does not support response_schema; "
                "falling back to text completion with manual JSON parsing"
            )
            content = await self.get_completion(system_prompt, user_prompt, max_tokens)
            data = self.clean_and_parse_json(content)
            return response_model.model_validate(data)

    def clean_and_parse_json(self, raw_json: str) -> Any:
        """Clean and parse JSON response with enhanced error handling."""
        try:
            # Remove markdown code blocks if present
            if '```json' in raw_json:
                raw_json = raw_json.split('```json')[1].split('```')[0].strip()
            elif '```' in raw_json:
                # Handle cases where it's just ```
                parts = raw_json.split('```')
                if len(parts) >= 2:
                    raw_json = parts[1].strip()
            
            # Clean up common formatting issues
            raw_json = raw_json.strip()
            if not raw_json.startswith('{') and not raw_json.startswith('['):
                # Find the first { or [
                first_brace = raw_json.find('{')
                first_bracket = raw_json.find('[')
                
                if first_brace == -1 and first_bracket == -1:
                    logger.error(f"No JSON object or array found in the raw response: {raw_json}")
                    raise ValueError(f"No JSON object or array found in LLM response: {raw_json[:500]}")
                
                start_idx = -1
                if first_brace != -1 and first_bracket != -1:
                    start_idx = min(first_brace, first_bracket)
                elif first_brace != -1:
                    start_idx = first_brace
                else:
                    start_idx = first_bracket
                    
                if start_idx != -1:
                    raw_json = raw_json[start_idx:]
            
            # Try standard JSON parsing first
            try:
                return json.loads(raw_json)
            except json.JSONDecodeError:
                # Fall back to json_repair for malformed JSON
                return json_repair.loads(raw_json)
                
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON: {e}")
            logger.error(f"Raw response: {raw_json[:500]}...")
            raise ValueError(f"Failed to parse JSON from LLM response: {e}. Raw response: {raw_json[:500]}")
        except Exception as e:
            logger.error(f"Unexpected error parsing JSON: {e}")
            raise ValueError(f"Unexpected error parsing JSON from LLM response: {e}. Raw response: {raw_json[:500] if 'raw_json' in locals() else 'N/A'}")
        
        