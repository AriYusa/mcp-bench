"""
Resilient MCP Toolset

A drop-in replacement for McpToolset that:
1. Silently returns an empty tool list when the MCP server is unreachable at
   setup time (instead of crashing the entire agent setup).
2. Wraps every tool's run_async so that a timeout during execution returns a
   structured error dict instead of propagating the exception and aborting the
   whole task.
"""

import asyncio
import logging
from typing import Any, List, Optional
from mcp import McpError

from google.adk.tools.base_tool import BaseTool
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

logger = logging.getLogger(__name__)


def _wrap_tool_run_async(tool: BaseTool) -> None:
    """Monkey-patch *tool*.run_async to catch TimeoutError and return an error dict."""
    original_run_async = tool.run_async

    async def resilient_run_async(*args: Any, **kwargs: Any):
        try:
            return await original_run_async(*args, **kwargs)
        except Exception as e:
            # Always re-raise genuine cancellation.
            if isinstance(e, asyncio.CancelledError):
                raise
            task = asyncio.current_task()
            if task is not None:
                cancelling = getattr(task, "cancelling", None)
                if cancelling is not None and cancelling() > 0:
                    raise

            is_timeout = (
                isinstance(e, (TimeoutError, asyncio.TimeoutError))
                or (isinstance(e, McpError) and "Timed out" in str(e))
            )
            if is_timeout:
                tool_name = getattr(tool, "name", "<unknown>")
                logger.warning("MCP tool '%s' timed out: %s", tool_name, e)
                return {
                    "error": f"MCP tool '{tool_name}' timed out. Try a different approach or skip."
                }
            # All other exceptions propagate normally.
            raise

    tool.run_async = resilient_run_async


class ResilientMcpToolset(McpToolset):
    """McpToolset that handles both connection-time and execution-time failures.

    - Connection failures (get_tools): logs a warning and returns [] so the
      agent continues with whatever other toolsets it has.
    - Runtime timeouts (run_async): catches TimeoutError and returns a
      structured error dict so the agent can continue instead of aborting.
    - Cancellation signals are always re-raised so asyncio structured
      cancellation works correctly.
    """

    async def get_tools(
        self,
        readonly_context: Optional[ReadonlyContext] = None,
    ) -> List[BaseTool]:
        try:
            tools = await super().get_tools(readonly_context)
        except Exception as e:
            # Always re-raise genuine cancellation — never swallow those.
            if isinstance(e, asyncio.CancelledError):
                raise
            task = asyncio.current_task()
            if task is not None:
                cancelling = getattr(task, "cancelling", None)
                if cancelling is not None and cancelling() > 0:
                    raise

            prefix = getattr(self, "_tool_name_prefix", None) or getattr(self, "tool_name_prefix", "<unknown>")
            logger.warning(
                "MCP toolset '%s' skipped — server unreachable: %s", prefix, e
            )
            return []

        # Wrap every tool so runtime timeouts return an error dict instead of
        # propagating and aborting the entire task execution.
        for tool in tools:
            _wrap_tool_run_async(tool)

        return tools
