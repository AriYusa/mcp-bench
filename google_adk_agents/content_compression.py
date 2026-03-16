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

    Tool Result Compression (after_tool_callback): Compresses large individual tool results
    before they are stored in history. Never breaks function_call/response
    ID pairings.

    History Compression (before_model_callback, soft limit): If total context still
    exceeds token_threshold, summarises all of contents[1:] in one LLM call.

    Rule-Based History Comperssion (rule-based fallback): If the LLM fails, doesn't reduce the
    context, or total context exceeds hard_limit_threshold, truncates history by removing middle rounds getails about function responces. 
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
            token_threshold: Soft token threshold to trigger History Compression (history
                compression). Should be well below the model's hard context limit.
            tool_result_threshold: Token threshold for a single tool result to
                trigger Tool Result Compression compression.
            hard_limit_threshold: Hard token threshold above which History Compression is
                skipped (LLM call itself would be too large) and Rule-Based History Comperssion fires
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
    
    def get_and_clear_compression_info(self, tool_name: str, call_id: Optional[str] = None) -> Optional[Dict[str, int]]:
        """Return and remove stored compression metadata for a given tool call.

        Called by the executor after a function_response event to annotate
        execution results with Tool Result Compression compression details.

        Args:
            tool_name: ADK tool name (as used in function_call / function_response)
            call_id: The function call ID (from FunctionResponse.id). When provided,
                     used as the primary lookup key to correctly handle multiple
                     parallel calls to the same tool.

        Returns:
            Dict with keys ``tokens_before`` and ``tokens_after`` if the result
            was compressed, otherwise ``None``.
        """
        if call_id is not None:
            return self._tool_compression_metadata.pop(call_id, None)
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
    # Tool Result Compression: Compress large tool results at source
    # ------------------------------------------------------------------

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: Dict[str, Any],
        tool_context: Any,
        result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Compress oversized tool results before they enter history (Tool Result Compression).

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
        error_msg = result.get("isError") or result.get("error", "")

        # Hard-limit guard: result too large to safely send to the LLM itself —
        # skip the LLM call and truncate directly (mirrors before_model_callback logic)
        if result_tokens >= self.hard_limit_threshold:
            logger.warning(
                f"[Tool Result Compression] Tool '{tool_name}' result is {result_tokens} tokens >= "
                f"hard_limit {self.hard_limit_threshold}. Skipping LLM, truncating directly."
            )
            target_chars = self.tool_result_threshold * 4  # ≈ tool_result_threshold tokens
            half = target_chars // 2
            truncated_text = (
                result_text[:half]
                + f"\n\n[...{len(result_text) - target_chars} chars truncated — "
                f"result exceeded hard limit...]\n\n"
                + result_text[-half:]
            )
            compressed_tokens = _estimate_tokens(truncated_text, self.model_name)
            tokens_saved = result_tokens - compressed_tokens
            self.tool_result_compressions += 1
            self.total_tokens_saved += tokens_saved
            metadata_key = tool_context.function_call_id if tool_context.function_call_id else tool_name
            self._tool_compression_metadata[metadata_key] = {
                "compression_method": "rule-based",
                "tokens_before": result_tokens,
                "tokens_after": compressed_tokens,
            }
            logger.info(
                f"[Tool Result Compression] '{tool_name}': {result_tokens} -> {compressed_tokens} tokens "
                f"({tokens_saved} saved) via rule-based truncation"
            )
            if error_msg:
                return {"output": truncated_text, "error": error_msg}
            return {"output": truncated_text}

        logger.info(
            f"[Tool Result Compression] Tool '{tool_name}' result is {result_tokens} tokens > "
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
                    f"[Tool Result Compression] LLM did not reduce tool result tokens "
                    f"({compressed_tokens} >= {result_tokens}). Keeping original."
                )
                return None

            tokens_saved = result_tokens - compressed_tokens
            self.tool_result_compressions += 1
            self.total_tokens_saved += tokens_saved
            logger.info(
                f"[Tool Result Compression] '{tool_name}': {result_tokens} -> {compressed_tokens} tokens "
                f"({tokens_saved} saved, {tokens_saved * 100 // result_tokens}% reduction)"
            )
            # Record metadata so the executor can annotate execution_results.
            # Key by function_call_id (when available) so that multiple parallel
            # calls to the same tool don't overwrite each other's metadata.
            metadata_key = tool_context.function_call_id if tool_context.function_call_id else tool_name
            self._tool_compression_metadata[metadata_key] = {
                "compression_method": "LLM",
                "tokens_before": result_tokens,
                "tokens_after": compressed_tokens,
            }
            if error_msg:
                return {"output": compressed_text, "error": error_msg}
            return {"output": compressed_text}

        except Exception as e:
            logger.error(f"[Tool Result Compression] Tool result compression failed for '{tool_name}': {e}")
            return None

    @observe(as_type="generation")
    async def _compress_tool_result_with_llm(
        self,
        tool_name: str,
        tool_args: Dict[str, Any],
        result_text: str,
        user_query: str,
    ) -> str:
        """Use LLM to compress a single tool result (used by Tool Result Compression).

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
    # History Compression: Soft-limit whole-history compression
    # ------------------------------------------------------------------

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> Optional[LlmResponse]:
        """Compress full conversation history if total tokens exceed the soft threshold (History Compression).

        Fires before every LLM call. If total tokens are under token_threshold,
        returns immediately. If over hard_limit_threshold, skips the LLM call
        entirely and goes straight to rule-based Rule-Based History Comperssion (the LLM call itself
        would be too expensive at that size).

        Otherwise performs a single LLM call that summarises ALL of contents[1:]
        — the entire history excluding the initial user query — into one compact
        block. Assumes all function_call/response pairs are complete by the time
        this callback fires (which is always the case in ADK's execution model).

        Falls back to Rule-Based History Comperssion if the LLM fails or doesn't reduce tokens.

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
        # rule-based truncation (Rule-Based History Comperssion)
        if total_tokens >= self.hard_limit_threshold:
            logger.warning(
                f"[Rule-Based History Comperssion] Context is {total_tokens} tokens >= hard_limit "
                f"{self.hard_limit_threshold}. Skipping LLM, applying rule-based truncation."
            )
            self._apply_rule_based_compression(contents)
            llm_request.contents = contents
            return None

        logger.warning(
            f"[History Compression] Context is {total_tokens} tokens > soft threshold "
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
                    f"[History Compression] LLM did not reduce history tokens "
                    f"({compressed_tokens} >= {history_tokens}). Falling back to Rule-Based History Comperssion."
                )
                self._apply_rule_based_compression(contents)
                llm_request.contents = contents
                return None

            tokens_saved = history_tokens - compressed_tokens
            self.compression_count += 1
            self.total_tokens_saved += tokens_saved
            logger.info(
                f"[History Compression] History compressed: {history_tokens} -> {compressed_tokens} tokens "
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
            logger.error(f"[History Compression] LLM history compression failed: {e}. Falling back to Rule-Based History Comperssion.")
            self._apply_rule_based_compression(contents)

        llm_request.contents = contents
        return None
    
    # ------------------------------------------------------------------
    # Rule-Based History Comperssion: Rule-based fallback truncation
    # ------------------------------------------------------------------

    def _apply_rule_based_compression(self, contents: List[types.Content]) -> None:
        """Truncate the largest parts in-place without any LLM call (Rule-Based History Comperssion).

        Called when LLM compression fails, doesn't reduce context, or the hard
        token limit is already exceeded. Operates on contents[1:] only, never
        touching the initial user query.

        Args:
            contents: Full contents list to modify in-place
        """
        logger.warning("[Rule-Based History Comperssion] Applying rule-based truncation of largest parts.")
        self._truncate_middle_rounds(contents)


        

    # ------------------------------------------------------------------
    # LLM helpers
    # ------------------------------------------------------------------

    @observe(as_type="generation")
    async def _compress_history_with_llm(
        self,
        history_text: str,
        user_query: str,
    ) -> str:
        """Use LLM to compress the full conversation history (used by History Compression).

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
    
    def _truncate_middle_rounds(self, contents: List[types.Content]) -> None:
        """Collapse all but the last round into compact text summaries (Rule-Based History Compression).

        A "round" is one role="model" content followed by zero or more role="tool"
        contents that carry its function_response parts. Round numbers are
        derived by counting model-role contents in order — no external metadata
        needed.

        function_call and function_response parts are matched by their shared
        call ID so that tool name, args, and success status are always reported
        together even when multiple tools were called in parallel.

        contents[0] (the original user query) is never touched.

        Args:
            contents: Full contents list to modify in-place
        """
        # --- 1. Parse rounds from contents[1:] -------------------------------------------
        # Each round: (round_num, model_content, [tool_contents])
        rounds: List[tuple] = []
        current_model: Optional[types.Content] = None
        current_tools: List[types.Content] = []
        round_num = 0

        for content in contents[1:]:
            if getattr(content, "role", None) == "model":
                # Save previous round if one was in progress
                if current_model is not None:
                    rounds.append((round_num, current_model, current_tools))
                round_num += 1
                current_model = content
                current_tools = []
            elif getattr(content, "role", None) == "tool" and current_model is not None:
                current_tools.append(content)
            # Any other content (e.g. stray user messages) is intentionally dropped
            # from middle rounds but will be kept as part of the last round if present.

        # Flush the final in-progress round
        if current_model is not None:
            rounds.append((round_num, current_model, current_tools))

        if len(rounds) <= 1:
            # Nothing to collapse — only one round exists, keep it untouched
            logger.info("[Rule-Based History Compression] Only 1 round found, nothing to truncate.")
            return

        original_len = len(contents)
        middle_rounds = rounds[:-1]
        last_round_num, last_model, last_tools = rounds[-1]

        # --- 2. Build a compact summary for each middle round ----------------------------
        summary_lines: List[str] = []

        for rnum, model_content, tool_contents in middle_rounds:
            lines = [f"[Round {rnum}]"]

            # Extract the model's reasoning text (first text part, capped at 300 chars)
            reasoning = ""
            # Also collect function_calls keyed by call ID
            call_map: Dict[str, tuple] = {}  # call_id -> (name, args_str)

            if model_content.parts:
                for part in model_content.parts:
                    if not reasoning and hasattr(part, "text") and part.text:
                        reasoning = part.text.strip()[:500]
                        if len(part.text.strip()) > 500:
                            reasoning += "…"
                    if hasattr(part, "function_call") and part.function_call:
                        fc = part.function_call
                        name = getattr(fc, "name", "unknown")
                        call_id = getattr(fc, "id", None) or name
                        try:
                            args_str = json.dumps(getattr(fc, "args", {}), default=str)[:200]
                        except Exception:
                            args_str = str(getattr(fc, "args", {}))
                        call_map[call_id] = (name, args_str)

            if reasoning:
                lines.append(f"Reasoning: {reasoning}")

            # Collect function_responses keyed by call ID -> error_msg (None = success)
            # We use a separate set to distinguish "found with no error" from "not found".
            response_map: Dict[str, Optional[str]] = {}  # call_id -> error_msg or None
            for tool_content in tool_contents:
                if not tool_content.parts:
                    continue
                for part in tool_content.parts:
                    if hasattr(part, "function_response") and part.function_response:
                        fr = part.function_response
                        resp_id = getattr(fr, "id", None) or getattr(fr, "name", "unknown")
                        response = getattr(fr, "response", {})
                        err = response.get("isError") or response.get("error")
                        response_map[resp_id] = str(err) if err else None

            # Emit one line per call with matched result
            if call_map:
                lines.append("Tool calls:")
                for call_id, (name, args_str) in call_map.items():
                    if call_id not in response_map:
                        status = "status unknown"
                    else:
                        matched_err = response_map[call_id]
                        if matched_err:
                            status = f"Failed with error: {matched_err[:500]}"
                        else:
                            status = "Succeeded: [response hidden for brevity]"
                    lines.append(f"  - {name}({args_str}) → {status}")

            summary_lines.append("\n".join(lines))

        # --- 3. Wrap all summaries in a single user content ------------------------------
        summary_text = "[Compressed history of previous rounds:\n\n" + "\n\n".join(summary_lines) + "]"
        summary_content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=summary_text)],
        )

        # --- 4. Rebuild contents in-place -----------------------------------------------
        last_round_contents = [last_model] + last_tools
        contents[:] = [contents[0], summary_content] + last_round_contents

        logger.info(
            f"[Rule-Based History Compression] Collapsed {len(middle_rounds)} middle round(s) "
            f"into summary; contents reduced from {original_len} to {len(contents)}"
        )
    
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
