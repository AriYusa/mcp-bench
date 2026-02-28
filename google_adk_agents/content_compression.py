"""
Content Compression Module for ADK Agents

Provides callbacks for compressing conversation context to prevent
context window overflow.

before_model_callback: Smart compression — when total context exceeds
   a token threshold, uses LLM to summarize older conversation parts
   while preserving recent context and key findings.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types
from langfuse import observe


logger = logging.getLogger(__name__)


def _estimate_tokens(text: str, model: str = "") -> int:
    """Estimate token count for a text string.
    
    Tries litellm.token_counter first for accuracy, falls back to
    character-based approximation (1 token ≈ 4 chars).
    
    Args:
        text: Text to count tokens for
        model: Model name for tokenizer selection
        
    Returns:
        Estimated token count
    """
    if not text:
        return 0
    try:
        import litellm
        return litellm.token_counter(model=model, text=text)
    except Exception:
        # Fallback: ~4 chars per token
        return len(text) // 4
    

def get_content_text(content: types.Content) -> str:
    parts_text = []
    for part in content.parts:
        if hasattr(part, 'text') and part.text:
            parts_text.append(part.text)
        elif hasattr(part, 'function_call') and part.function_call:
            fc = part.function_call
            name = getattr(fc, 'name', '')
            args = getattr(fc, 'args', {})
            parts_text.append(f"function_call:{name}({json.dumps(args, default=str)})")
        elif hasattr(part, 'function_response') and part.function_response:
            fr = part.function_response
            name = getattr(fr, 'name', '')
            response = getattr(fr, 'response', {})
            try:
                resp_str = json.dumps(response, default=str)
            except Exception:
                resp_str = str(response)
            parts_text.append(f"function_response:{name}={resp_str}")
    return "\n".join(parts_text)

def _extract_text_from_contents(contents: List[types.Content]) -> str:
    """Extract all text from a list of Content objects for token counting.
    
    Args:
        contents: List of google.genai.types.Content objects
        
    Returns:
        Concatenated text representation of all content
    """
    return "\n\n".join(
        get_content_text(c) for c in contents if c and c.parts
    )


class ContentCompressor(BasePlugin):
    """Manages context compression for ADK agents via callbacks.
    
    Attaches to agents as before_model_callback to prevent context window overflow.
    
    The before_model_callback:
    - Counts total tokens in the LLM request contents
    - If over threshold, identifies older content entries 
    - Calls LLM to produce a compressed summary preserving key findings
    - Replaces older entries with a single summarized Content
    - Repeats up to max_compression_passes if still over threshold
    """
    
    def __init__(
        self,
        model_name: str,
        token_threshold: int = 100_000,
        max_compression_passes: int = 3,
    ):
        """Initialize the content compressor.
        
        Args:
            model_name: Model name for the LLM provider (LiteLLM format)
            token_threshold: Token count threshold to trigger compression
            max_compression_passes: Max LLM compression iterations
        """
        super().__init__(name="content_compressor")
        self.trace_id = None

        self.token_threshold = token_threshold
        self.model_name = model_name
        self.max_compression_passes = max_compression_passes
        
        # Lazy-init LLM provider to avoid import issues at module level
        self._llm_provider = None
        
        # Stats tracking
        self.compression_count = 0
        self.total_tokens_saved = 0
        
        logger.info(
            f"ContentCompressor initialized: threshold={token_threshold} tokens, "
            f"max_compression_passes={max_compression_passes}, model={model_name}"
        )
    
    def _get_llm_provider(self):
        """Lazy-initialize the LLM provider."""
        if self._llm_provider is None:
            from llm.provider import LLMProvider
            self._llm_provider = LLMProvider(self.model_name)
            logger.info(f"ContentCompressor LLM provider initialized with model: {self.model_name}")
        return self._llm_provider
    
    async def before_model_callback(
        self,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        """Compress context if total tokens exceed threshold.
        
        This callback fires before each LLM call. It estimates the total
        token count of the conversation contents. If over threshold, it
        uses the LLM to summarize older parts while keeping recent context
        intact.
        
        Args:
            callback_context: ADK callback context
            llm_request: The LLM request about to be sent
            
        Returns:
            None to continue with (potentially modified) request,
            or LlmResponse to skip the model call entirely
        """
        contents = llm_request.contents
        if not contents:
            return None
        
        total_text = _extract_text_from_contents(contents)
        total_tokens = _estimate_tokens(total_text, self.model_name)
        
        if total_tokens <= self.token_threshold:
            return None  # Under threshold, no compression needed
        
        logger.warning(
            f"Context exceeds threshold: {total_tokens} tokens > {self.token_threshold}. "
            f"Starting LLM-based compression..."
        )
        
        # Extract user query from first user content for context-aware compression
        user_query = ""
        first_content = contents[0]
        if not (first_content.role == "user" and first_content.parts):
            raise ValueError("Expected first content to be user query with parts")

        for part in first_content.parts:
            if hasattr(part, 'text') and part.text:
                user_query = part.text
                break
        
        for pass_num in range(1, self.max_compression_passes + 1):
            contents_to_compress = contents[1:]  # Excluding first user query
            texts_to_summarise = []
            content_count_to_summarise = 0
            to_summarise_tokens = 0
            for content in contents_to_compress:
                if not content or not content.parts:
                    continue
                
                content_tokens = _estimate_tokens(get_content_text(content), self.model_name)
                if to_summarise_tokens + content_tokens > self.token_threshold:  # Summarise up to the point we exceed the threshold
                    if content_count_to_summarise == 0:
                        logger.warning(
                            f"Single content item exceeds threshold: {content_tokens} tokens > "
                            f"{self.token_threshold}."
                        )
                        break
                    logger.info(f"Summarising {content_count_to_summarise} content items")
                    break
                else:
                    texts_to_summarise.append(get_content_text(content))
                    to_summarise_tokens += content_tokens
                    content_count_to_summarise += 1

            total_text = "\n\n".join(texts_to_summarise)
            # Slice contents_to_compress directly so empty items don't skew the index
            remaining_contents = contents_to_compress[content_count_to_summarise:]
            remaining_tokens = 0
            for content in remaining_contents:
                if not content or not content.parts:
                    continue
                remaining_tokens += _estimate_tokens(get_content_text(content), self.model_name)
            
            try:
                compressed_text = await self._compress_with_llm(
                    total_text, user_query
                )
                compressed_tokens = _estimate_tokens(compressed_text, self.model_name)
                
                if compressed_tokens >= to_summarise_tokens:
                    logger.warning(
                        f"LLM compression did not reduce tokens ({compressed_tokens} >= {to_summarise_tokens}). "
                    )
                    return None 

                tokens_saved = to_summarise_tokens - compressed_tokens
                self.compression_count += 1
                self.total_tokens_saved += tokens_saved
                
                logger.info(
                    f"Compression pass {pass_num} successful: {to_summarise_tokens} -> {compressed_tokens} tokens "
                    f"({tokens_saved} tokens saved, {tokens_saved * 100 // to_summarise_tokens}% reduction)"
                )
                
                # Build compressed content entry
                compressed_content = types.Content(
                    role="user",
                    parts=[types.Part.from_text(
                        text=f"[Compressed context summary from earlier conversation entries:\n\n"
                        f"{compressed_text}"
                    )]
                )
                
                # Replace contents: first + compressed + recent
                contents.clear()
                contents.append(first_content)
                contents.append(compressed_content)
                contents.extend(remaining_contents)

                if compressed_tokens + remaining_tokens < self.token_threshold:
                    logger.info("Compressed under threshold.")
                    break

            except Exception as e:
                logger.error(f"LLM compression failed on pass {pass_num}: {e}")
                return None 
        
        llm_request.contents = contents
        return None  # Continue with modified request
    
    @observe()
    async def _compress_with_llm(
        self, 
        text: str, 
        user_query: str, 
    ) -> str:
        """Use LLM to compress conversation history.
        
        Args:
            text: Text to compress
            user_query: Original user query for context-aware compression
            target_tokens: Target token count for the compressed output
            
        Returns:
            Compressed text
        """
        llm = self._get_llm_provider()
        
        system_prompt = (
            "You are an expert at compressing conversation history while preserving "
            "critical information. Your task is to produce a concise summary of the "
            "conversation history that retains all key findings, tool results, "
            "important data points, and progress toward answering the user's question."
        )
        
        user_prompt = f"""Compress the following conversation history.

USER'S ORIGINAL QUESTION: {user_query}

RULES:
- Preserve ALL key findings, data points, and factual results from tool calls
- Preserve tool names and which tools were called, and their key outputs
- Remove redundant information, verbose formatting, and duplicate data
- Keep information that is directly relevant to answering the user's question
- Use concise bullet points or brief paragraphs
- Do NOT add new information — only summarize what's there

CONVERSATION HISTORY TO COMPRESS:
{text}

COMPRESSED SUMMARY:"""
        
        compressed = await llm.get_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=self.token_threshold // 2,  # Aim for significant reduction
        )
        
        return compressed.strip()
    
    def _truncate_largest_parts(self, contents: List[types.Content]) -> None:
        """Truncate the largest function_response parts in-place as a fallback.
        
        Finds the largest text/function_response parts and truncates them
        to reduce total size.
        
        Args:
            contents: List of Content objects to modify in-place
        """
        # Collect all parts with their sizes
        parts_with_sizes = []
        for content in contents:
            if not content or not content.parts:
                continue
            for i, part in enumerate(content.parts):
                if hasattr(part, 'function_response') and part.function_response:
                    fr = part.function_response
                    response = getattr(fr, 'response', {})
                    try:
                        text = json.dumps(response, default=str)
                    except Exception:
                        text = str(response)
                    parts_with_sizes.append((content, i, len(text), 'function_response'))
                elif hasattr(part, 'text') and part.text:
                    parts_with_sizes.append((content, i, len(part.text), 'text'))
        
        # Sort by size descending
        parts_with_sizes.sort(key=lambda x: x[2], reverse=True)
        
        # Truncate top 3 largest parts
        for content, part_idx, size, part_type in parts_with_sizes[:3]:
            if size <= 10_000:  # Don't truncate small parts
                continue
                
            target_chars = min(size // 4, 40_000)  # Reduce to 25% or 40K chars max
            part = content.parts[part_idx]
            
            if part_type == 'function_response':
                fr = part.function_response
                response = getattr(fr, 'response', {})
                truncated = self._truncate_dict_values(response, target_chars)
                # We can't easily modify the function_response in place,
                # so replace the part with a text summary
                name = getattr(fr, 'name', 'unknown_tool')
                try:
                    truncated_str = json.dumps(truncated, default=str)
                except Exception:
                    truncated_str = str(truncated)[:target_chars]
                content.parts[part_idx] = types.Part.from_text(
                    text=f"[Truncated response from {name}, original {size} chars]: {truncated_str}"
                )
            elif part_type == 'text' and size > target_chars:
                original_text = part.text
                keep_start = target_chars // 2
                keep_end = target_chars // 2
                content.parts[part_idx] = types.Part.from_text(
                    text=f"{original_text[:keep_start]}\n\n[...{size - target_chars} chars truncated...]\n\n{original_text[-keep_end:]}"
                )
            
            logger.info(f"Truncated {part_type} part from {size} chars to ~{target_chars} chars")
    
    def _truncate_dict_values(self, d: dict, max_total_chars: int) -> dict:
        """Truncate string values in a dict to fit within max_total_chars.
        
        Args:
            d: Dictionary to truncate
            max_total_chars: Maximum total characters for all string values
            
        Returns:
            New dictionary with truncated values
        """
        if not isinstance(d, dict):
            s = str(d)
            if len(s) > max_total_chars:
                return s[:max_total_chars] + f"...[truncated from {len(s)} chars]"
            return d
        
        result = {}
        # First pass: find total size and identify large values
        for key, value in d.items():
            if isinstance(value, str) and len(value) > max_total_chars // 2:
                # Truncate large string values
                truncated = value[:max_total_chars // 2]
                result[key] = f"{truncated}...[truncated from {len(value)} chars]"
            elif isinstance(value, (dict, list)):
                kind = "dict" if isinstance(value, dict) else "list"
                try:
                    val_str = json.dumps(value, default=str)
                except Exception:
                    val_str = str(value)
                if len(val_str) > max_total_chars // 2:
                    result[key] = f"{val_str[:max_total_chars // 2]}...[truncated {kind} from {len(val_str)} chars]"
                else:
                    result[key] = value
            else:
                result[key] = value
        
        return result
    
    def get_stats(self) -> Dict[str, Any]:
        """Get compression statistics.
        
        Returns:
            Dictionary with compression stats
        """
        return {
            "compression_count": self.compression_count,
            "total_tokens_saved": self.total_tokens_saved,
            "token_threshold": self.token_threshold,
        }
