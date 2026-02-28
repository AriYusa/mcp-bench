"""
Resilient MCP Toolset

A drop-in replacement for McpToolset that silently returns an empty tool list
when the MCP server is unreachable, instead of crashing the entire agent setup.
The parent's @retry_on_errors decorator still fires (one automatic retry) before
this class ever sees the exception.
"""

import asyncio
import logging
from typing import List, Optional

from google.adk.tools.base_tool import BaseTool
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

logger = logging.getLogger(__name__)


class ResilientMcpToolset(McpToolset):
    """McpToolset that silently skips if the MCP server is unreachable.

    On a connection failure, ``get_tools()`` logs a warning and returns ``[]``
    so the agent continues with whatever other toolsets it has.  Cancellation
    signals are always re-raised so asyncio structured cancellation works correctly.
    """

    async def get_tools(
        self,
        readonly_context: Optional[ReadonlyContext] = None,
    ) -> List[BaseTool]:
        try:
            return await super().get_tools(readonly_context)
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
