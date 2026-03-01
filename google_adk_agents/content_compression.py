"""
Content Compression Module for ADK Agents
"""

import json
import logging
from typing import Any, Dict, List, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types
from langfuse import get_client as get_langfuse_client
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

    Three-layer defense against context window overflow:

    Layer 1 (after_tool_callback): Compresses large individual tool results
    before they are stored in history. Never breaks function_call/response
    ID pairings.

    Layer 2 (before_model_callback, soft limit): If total context still
    exceeds token_threshold, summarises all of contents[1:] in one LLM call.

    Layer 3 (rule-based fallback): If the LLM fails, doesn't reduce the
    context, or total context exceeds hard_limit_threshold, truncates the
    largest parts in-place without an LLM call.
    """

    def __init__(
        self,
        model_name: str,
        token_threshold: int = 100_000,
        tool_result_threshold: int = 8_000,
        hard_limit_threshold: int = 180_000,
    ):
        """Initialize the content compressor.

        Args:
            model_name: Model name for the LLM provider (LiteLLM format)
            token_threshold: Soft token threshold to trigger Layer 2 (history
                compression). Should be well below the model's hard context limit.
            tool_result_threshold: Token threshold for a single tool result to
                trigger Layer 1 compression.
            hard_limit_threshold: Hard token threshold above which Layer 2 is
                skipped (LLM call itself would be too large) and Layer 3 fires
                immediately.
        """
        super().__init__(name="content_compressor")
        self.trace_id = None

        self.token_threshold = token_threshold
        self.tool_result_threshold = tool_result_threshold
        self.hard_limit_threshold = hard_limit_threshold
        self.model_name = model_name

        # Stored by on_user_message_callback for use in later callbacks
        self._current_user_query: str = ""

        # Lazy-init LLM provider to avoid import issues at module level
        self._llm_provider = None

        # Stats tracking
        self.compression_count = 0
        self.total_tokens_saved = 0
        self.tool_result_compressions = 0

        # Tokens consumed by compression LLM calls themselves
        self.compression_prompt_tokens = 0
        self.compression_output_tokens = 0
        self.compression_total_tokens = 0

        # Per-tool compression metadata: tool_name -> {tokens_before, tokens_after}
        # Populated by after_tool_callback; consumed (and cleared) by get_and_clear_compression_info.
        self._tool_compression_metadata: Dict[str, Dict[str, int]] = {}

        logger.info(
            f"ContentCompressor initialized: token_threshold={token_threshold}, "
            f"tool_result_threshold={tool_result_threshold}, "
            f"hard_limit_threshold={hard_limit_threshold}, model={model_name}"
        )
    
    def get_and_clear_compression_info(self, tool_name: str) -> Optional[Dict[str, int]]:
        """Return and remove stored compression metadata for a given tool.

        Called by the executor after a function_response event to annotate
        execution results with Layer 1 compression details.

        Args:
            tool_name: ADK tool name (as used in function_call / function_response)

        Returns:
            Dict with keys ``tokens_before`` and ``tokens_after`` if the result
            was compressed, otherwise ``None``.
        """
        return self._tool_compression_metadata.pop(tool_name, None)

    def _get_llm_provider(self):
        """Lazy-initialize the LLM provider."""
        if self._llm_provider is None:
            from llm.provider import LLMProvider
            self._llm_provider = LLMProvider(self.model_name)
            logger.info(f"ContentCompressor LLM provider initialized with model: {self.model_name}")
        return self._llm_provider

    async def on_user_message_callback(self, *, invocation_context: Any, user_message: Any) -> Optional[types.Content]:
        """Cache the user's original query for use in compression prompts.

        Fires once per invocation when the user message is received. Storing
        the query here avoids re-parsing it from contents in every subsequent
        callback.

        Args:
            invocation_context: ADK invocation context (unused)
            user_message: The user's Content message

        Returns:
            None (never modifies the message)
        """
        if user_message and user_message.parts:
            for part in user_message.parts:
                if hasattr(part, 'text') and part.text:
                    self._current_user_query = part.text
                    break
        return None

    # ------------------------------------------------------------------
    # Layer 1: Compress large tool results at source
    # ------------------------------------------------------------------

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: Dict[str, Any],
        tool_context: Any,
        result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Compress oversized tool results before they enter history (Layer 1).

        Fires after every tool call. Estimates the token count of the result.
        If it exceeds tool_result_threshold, calls the LLM to produce a
        faithful summary. Returns the compressed result dict so the stored
        function_response content is smaller while the function_call/response
        ID pairing is fully preserved.

        Args:
            tool: The tool that was called
            tool_args: Arguments passed to the tool
            tool_context: ADK tool context
            result: Raw tool result dict

        Returns:
            Compressed result dict to replace the original, or None to keep it
        """
        try:
            result_text = json.dumps(result, default=str)
        except Exception:
            result_text = str(result)

        result_tokens = _estimate_tokens(result_text, self.model_name)
        if result_tokens <= self.tool_result_threshold:
            return None  # Small enough — no compression needed

        tool_name = getattr(tool, 'name', str(tool))
        logger.info(
            f"[Layer 1] Tool '{tool_name}' result is {result_tokens} tokens > "
            f"{self.tool_result_threshold}. Compressing..."
        )

        try:
            compressed_text = await self._compress_tool_result_with_llm(
                tool_name=tool_name,
                tool_args=tool_args,
                result_text=result_text,
                user_query=self._current_user_query,
            )
            compressed_tokens = _estimate_tokens(compressed_text, self.model_name)

            if compressed_tokens >= result_tokens:
                logger.warning(
                    f"[Layer 1] LLM did not reduce tool result tokens "
                    f"({compressed_tokens} >= {result_tokens}). Keeping original."
                )
                return None

            tokens_saved = result_tokens - compressed_tokens
            self.tool_result_compressions += 1
            self.total_tokens_saved += tokens_saved
            logger.info(
                f"[Layer 1] '{tool_name}': {result_tokens} -> {compressed_tokens} tokens "
                f"({tokens_saved} saved, {tokens_saved * 100 // result_tokens}% reduction)"
            )
            # Record metadata so the executor can annotate execution_results
            self._tool_compression_metadata[tool_name] = {
                "tokens_before": result_tokens,
                "tokens_after": compressed_tokens,
            }
            return {"output": compressed_text}

        except Exception as e:
            logger.error(f"[Layer 1] Tool result compression failed for '{tool_name}': {e}")
            return None

    @observe(as_type="generation")
    async def _compress_tool_result_with_llm(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        result_text: str,
        user_query: str,
    ) -> str:
        """Use LLM to compress a single tool result (used by Layer 1).

        Args:
            tool_name: Name of the tool that produced the result
            tool_args: Arguments the tool was called with
            result_text: Raw tool result as a JSON string
            user_query: The user's original question for context

        Returns:
            Compressed result as a plain-text string
        """
        llm = self._get_llm_provider()

        try:
            args_str = json.dumps(tool_args, default=str)
        except Exception:
            args_str = str(tool_args)

        system_prompt = (
            "You are an expert at compressing tool outputs while preserving all "
            "information that is relevant to answering the user's question. "
            "Produce a concise but complete summary of the tool result."
        )

        user_prompt = f"""Compress the following tool result.

USER'S ORIGINAL QUESTION: {user_query}

TOOL CALLED: {tool_name}
TOOL ARGUMENTS: {args_str}

RULES:
- Preserve ALL key data points, facts, numbers, and identifiers
- Remove redundant fields, boilerplate, and duplicate entries
- Use concise bullet points or structured text
- Do NOT add new information — only summarize what's there
- Output only the compressed summary, no preamble

TOOL RESULT TO COMPRESS:
{result_text}

COMPRESSED SUMMARY:"""

        compressed, usage = await llm.get_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=self.tool_result_threshold,
            return_usage=True,
        )

        langfuse_client = get_langfuse_client()
        langfuse_client.update_current_generation(
            model=self.model_name,
            usage_details={
                "input": usage["prompt_tokens"],
                "output": usage["completion_tokens"],
            }
        )
        # Accumulate tokens used by this compression call
        self.compression_prompt_tokens += usage.get("prompt_tokens", 0)
        self.compression_output_tokens += usage.get("completion_tokens", 0)
        self.compression_total_tokens += usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        return compressed.strip()

    # ------------------------------------------------------------------
    # Layer 2: Soft-limit whole-history compression
    # ------------------------------------------------------------------

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        """Compress full conversation history if total tokens exceed the soft threshold (Layer 2).

        Fires before every LLM call. If total tokens are under token_threshold,
        returns immediately. If over hard_limit_threshold, skips the LLM call
        entirely and goes straight to rule-based Layer 3 (the LLM call itself
        would be too expensive at that size).

        Otherwise performs a single LLM call that summarises ALL of contents[1:]
        — the entire history excluding the initial user query — into one compact
        block. Assumes all function_call/response pairs are complete by the time
        this callback fires (which is always the case in ADK's execution model).

        Falls back to Layer 3 if the LLM fails or doesn't reduce tokens.

        Args:
            callback_context: ADK callback context
            llm_request: The LLM request about to be sent

        Returns:
            None to continue with the (potentially modified) request
        """
        contents = llm_request.contents
        if not contents:
            return None

        total_text = _extract_text_from_contents(contents)
        total_tokens = _estimate_tokens(total_text, self.model_name)

        if total_tokens <= self.token_threshold:
            return None  # Under soft threshold — nothing to do

        # If already past the hard limit, skip the LLM call — go straight to
        # rule-based truncation (Layer 3)
        if total_tokens >= self.hard_limit_threshold:
            logger.warning(
                f"[Layer 3] Context is {total_tokens} tokens >= hard_limit "
                f"{self.hard_limit_threshold}. Skipping LLM, applying rule-based truncation."
            )
            self._apply_rule_based_compression(contents)
            llm_request.contents = contents
            return None

        logger.warning(
            f"[Layer 2] Context is {total_tokens} tokens > soft threshold "
            f"{self.token_threshold}. Summarising full history with LLM..."
        )

        first_content = contents[0]
        history_contents = contents[1:]

        if not history_contents:
            return None

        history_text = _extract_text_from_contents(history_contents)
        history_tokens = _estimate_tokens(history_text, self.model_name)

        try:
            compressed_text = await self._compress_history_with_llm(
                history_text=history_text,
                user_query=self._current_user_query,
            )
            compressed_tokens = _estimate_tokens(compressed_text, self.model_name)

            if compressed_tokens >= history_tokens:
                logger.warning(
                    f"[Layer 2] LLM did not reduce history tokens "
                    f"({compressed_tokens} >= {history_tokens}). Falling back to Layer 3."
                )
                self._apply_rule_based_compression(contents)
                llm_request.contents = contents
                return None

            tokens_saved = history_tokens - compressed_tokens
            self.compression_count += 1
            self.total_tokens_saved += tokens_saved
            logger.info(
                f"[Layer 2] History compressed: {history_tokens} -> {compressed_tokens} tokens "
                f"({tokens_saved} saved, {tokens_saved * 100 // history_tokens}% reduction)"
            )

            compressed_content = types.Content(
                role="user",
                parts=[types.Part.from_text(
                    text=f"[Compressed summary of the full conversation history so far:\n\n{compressed_text}]"
                )],
            )

            contents.clear()
            contents.append(first_content)
            contents.append(compressed_content)

        except Exception as e:
            logger.error(f"[Layer 2] LLM history compression failed: {e}. Falling back to Layer 3.")
            self._apply_rule_based_compression(contents)

        llm_request.contents = contents
        return None
    
    # ------------------------------------------------------------------
    # Layer 3: Rule-based fallback truncation
    # ------------------------------------------------------------------

    def _apply_rule_based_compression(self, contents: List[types.Content]) -> None:
        """Truncate the largest parts in-place without any LLM call (Layer 3).

        Called when LLM compression fails, doesn't reduce context, or the hard
        token limit is already exceeded. Operates on contents[1:] only, never
        touching the initial user query.

        Args:
            contents: Full contents list to modify in-place
        """
        logger.warning("[Layer 3] Applying rule-based truncation of largest parts.")
        # Skip contents[0] (the initial user query)
        self._truncate_largest_parts(contents[1:])

    # ------------------------------------------------------------------
    # LLM helpers
    # ------------------------------------------------------------------

    @observe(as_type="generation")
    async def _compress_history_with_llm(
        self,
        history_text: str,
        user_query: str,
    ) -> str:
        """Use LLM to compress the full conversation history (used by Layer 2).

        Args:
            history_text: Serialised text of all history contents (excluding
                the initial user query)
            user_query: The user's original question for context-aware compression

        Returns:
            Compressed history as a plain-text string
        """
        llm = self._get_llm_provider()

        system_prompt = (
            "You are an expert at compressing conversation history while preserving "
            "critical information. Produce a concise summary that retains all key "
            "findings, tool results, important data points, and progress toward "
            "answering the user's question."
        )

        user_prompt = f"""Compress the following full conversation history into a single summary.

USER'S ORIGINAL QUESTION: {user_query}

RULES:
- Preserve ALL key findings, data points, and factual results from tool calls
- Preserve which tools were called and their key outputs
- Remove redundant information, verbose formatting, and duplicate data
- Keep information directly relevant to answering the user's question
- Use concise bullet points or brief paragraphs
- Do NOT add new information — only summarize what's there
- Output only the compressed summary, no preamble

CONVERSATION HISTORY TO COMPRESS:
{history_text}

COMPRESSED SUMMARY:"""

        compressed, usage = await llm.get_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=self.token_threshold // 2,
            return_usage=True,
        )

        langfuse_client = get_langfuse_client()
        langfuse_client.update_current_generation(
            model=self.model_name,
            usage_details={
                "input": usage["prompt_tokens"],
                "output": usage["completion_tokens"],
            }
        )
        # Accumulate tokens used by this compression call
        self.compression_prompt_tokens += usage.get("prompt_tokens", 0)
        self.compression_output_tokens += usage.get("completion_tokens", 0)
        self.compression_total_tokens += usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
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
            Dictionary with compression stats including per-layer counters
        """
        return {
            "history_compression_count": self.compression_count,
            "tool_result_compressions": self.tool_result_compressions,
            "total_tokens_saved": self.total_tokens_saved,
            "token_threshold": self.token_threshold,
            "tool_result_threshold": self.tool_result_threshold,
            "hard_limit_threshold": self.hard_limit_threshold,
            "compression_prompt_tokens": self.compression_prompt_tokens,
            "compression_output_tokens": self.compression_output_tokens,
            "compression_total_tokens": self.compression_total_tokens,
        }
