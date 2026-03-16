"""
ADK Task Executor Module

This module provides a Google ADK-based multi-agent executor that replaces
the single-agent TaskExecutor. It uses a coordinator agent with specialist
sub-agents for domain-specific task handling.
"""

import asyncio
import inspect
import json
import logging
import string
import time
from collections import defaultdict, deque
from typing import Dict, List, Any, Optional

from google.adk.runners import InMemoryRunner
from google.adk.agents.run_config import RunConfig
from google.adk.sessions.session import Session
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models import LlmResponse
from google.genai import types
from langfuse import get_client
from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.mcp_tool.mcp_tool import McpTool
from google.adk.tools.agent_tool import AgentTool

from .config import Config
from .content_compression import ContentCompressor
from .coordinator import MultiAgentOrchestrator
from . import adk_config_loader

logger = logging.getLogger(__name__)
langfuse = get_client()


def _mcp_asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Custom asyncio exception handler that downgrades MCP session background errors.

    ADK's SessionContext runs MCP session initialization in a fire-and-forget
    background task. If the MCP server takes too long to start, the task raises
    TimeoutError (and a BrokenResourceError during cleanup). Because nobody
    awaits the task, asyncio logs these as "Task exception was never retrieved"
    at ERROR level.

    These errors are harmless — the ResilientMcpToolset catches the failure in
    get_tools() and returns [], and ADK creates fresh sessions on demand during
    actual execution. This handler downgrades them to WARNING so they don't
    pollute the error log.
    """
    exc = context.get("exception")
    message = context.get("message", "")

    # Identify MCP session background task errors by their source location
    is_mcp_task_error = (
        "Task exception was never retrieved" in message
        and isinstance(exc, (TimeoutError, ExceptionGroup))
    )
    # Also catch BrokenResourceError wrapped in ExceptionGroup
    if not is_mcp_task_error and isinstance(exc, ExceptionGroup):
        flat = str(exc)
        if "BrokenResourceError" in flat or "TimeoutError" in flat:
            is_mcp_task_error = True

    if is_mcp_task_error:
        task = context.get("future")
        coro_name = ""
        if task is not None:
            coro = getattr(task, "get_coro", lambda: None)()
            if coro is not None:
                coro_name = getattr(coro, "__qualname__", "")
        # Only suppress if the failing task is an MCP SessionContext task
        if "SessionContext" in coro_name or not coro_name:
            logger.warning(
                "MCP session background task failed (server slow to start or "
                "connection dropped — this is expected during preload and will "
                "not affect execution): %s", type(exc).__name__
            )
            return

    # Fall through to default handler for everything else
    loop.default_exception_handler(context)

class LangfuseTracePlugin(BasePlugin):
    """Plugin to capture Langfuse trace ID during ADK runner execution."""
    
    def __init__(self) -> None:
        super().__init__(name="langfuse_trace")
        self.trace_id = None
    
    async def after_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        """Captures the Langfuse trace ID before the runner completes.
        """
        try:
            self.trace_id = langfuse.get_current_trace_id()
            if self.trace_id:
                logger.debug(f"Captured Langfuse trace ID: {self.trace_id}")
        except Exception as e:
            logger.warning(f"Failed to capture Langfuse trace ID: {e}")
            self.trace_id = None

class CountInvocationPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="count_invocation")
        self.llm_request_count: int = 0

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: Optional[LlmResponse]
    ) -> None:
        self.llm_request_count += 1


# ---------------------------------------------------------------------------
# Inner tool call tracking for "tools" routing mode
# ---------------------------------------------------------------------------

class InnerToolCallTracker:
    """Tracks MCP tool calls made by specialist agents when running in 'tools' mode.

    Each specialist runs inside an AgentTool sub-runner.  Agent-level callbacks
    on the specialist write to this shared tracker so the executor can later
    attach the inner calls to the corresponding agent-tool execution result.

    Two-phase design:

    1. *register_coordinator_call(agent_name, call_id)* — called from
       ``_track_tool_call`` as soon as the coordinator issues an agent-tool
       function_call.  Queues the coordinator ``call_id`` per agent_name (FIFO).

    2. *register_invocation(agent_name, invocation_id)* — called from
       ``before_agent_callback`` when the specialist actually starts.  Claims
       the next pending ``call_id`` for that agent (FIFO by start order, which
       matches issue order) and stores ``call_id → invocation_id``.

    3. *pop_calls(agent_name, call_id)* — called from ``_track_tool_response``
       using the coordinator ``call_id`` (== ``response_id``).  Looks up the
       specialist ``invocation_id`` directly, so completion order does not
       matter — parallel runs that finish out of order are always attributed
       to the correct outer tool call.
    """

    def __init__(self) -> None:
        # sub_round counter per invocation_id
        self._sub_rounds: Dict[str, int] = {}
        # collected inner tool call dicts per invocation_id
        self._calls: Dict[str, List[Dict[str, Any]]] = {}
        # FIFO of coordinator call_ids per agent_name (issue order)
        self._pending_call_ids: Dict[str, deque] = {}
        # coordinator call_id → specialist invocation_id (set at start time)
        self._call_id_to_invocation: Dict[str, str] = {}
        # Token accumulators for specialist LLM calls (not visible in outer event stream)
        self.specialist_prompt_tokens: int = 0
        self.specialist_output_tokens: int = 0
        self.specialist_total_tokens: int = 0

    def reset_tokens(self) -> None:
        """Reset specialist token accumulators (called at the start of each execute())."""
        self.specialist_prompt_tokens = 0
        self.specialist_output_tokens = 0
        self.specialist_total_tokens = 0

    def register_coordinator_call(self, agent_name: str, call_id: str) -> None:
        """Called from _track_tool_call when the coordinator issues an agent-tool call."""
        self._pending_call_ids.setdefault(agent_name, deque()).append(call_id)
        logger.debug(f"[InnerTracker] pending coordinator call {call_id} for {agent_name}")

    def register_invocation(self, agent_name: str, invocation_id: str) -> None:
        """Called from before_agent_callback: links the next pending call_id to this invocation."""
        if invocation_id in self._calls:
            return  # already registered (guard against duplicate before_agent_callback)
        self._sub_rounds[invocation_id] = 0
        self._calls[invocation_id] = []
        pending = self._pending_call_ids.get(agent_name)
        if pending:
            call_id = pending.popleft()
            self._call_id_to_invocation[call_id] = invocation_id
            logger.debug(f"[InnerTracker] linked call_id={call_id} → invocation={invocation_id} for {agent_name}")
        else:
            logger.warning(f"[InnerTracker] register_invocation: no pending call_id for {agent_name}")

    def increment_sub_round(self, invocation_id: str) -> int:
        """Increment and return the sub-round counter for *invocation_id*."""
        self._sub_rounds[invocation_id] = self._sub_rounds.get(invocation_id, 0) + 1
        return self._sub_rounds[invocation_id]

    def current_sub_round(self, invocation_id: str) -> int:
        return self._sub_rounds.get(invocation_id, 0)

    def record_tool_call(self, invocation_id: str, entry: Dict[str, Any]) -> None:
        if invocation_id in self._calls:
            self._calls[invocation_id].append(entry)
        else:
            logger.warning(f"[InnerTracker] record_tool_call: unknown invocation_id {invocation_id}")

    def pop_calls(self, agent_name: str, call_id: Optional[str]) -> List[Dict[str, Any]]:
        """Return and clear inner calls for the coordinator *call_id*.

        Keyed by coordinator call_id so completions can arrive in any order.
        Falls back to the oldest pending invocation if call_id is unavailable.
        """
        invocation_id = self._call_id_to_invocation.pop(call_id, None) if call_id else None
        if invocation_id is None:
            # Fallback: should not normally happen, but drain the oldest entry
            logger.warning(f"[InnerTracker] pop_calls: no invocation for call_id={call_id}, falling back")
            for inv_id in list(self._calls):
                calls = self._calls.pop(inv_id, [])
                self._sub_rounds.pop(inv_id, None)
                return calls
            return []
        calls = self._calls.pop(invocation_id, [])
        self._sub_rounds.pop(invocation_id, None)
        return calls


def _make_specialist_before_agent_cb(tracker: InnerToolCallTracker):
    """Factory: returns a before_agent_callback that registers the invocation_id."""
    async def _before_agent_cb(callback_context: CallbackContext) -> None:
        agent_name = callback_context.agent_name
        invocation_id = callback_context.invocation_id
        tracker.register_invocation(agent_name, invocation_id)
        return None
    return _before_agent_cb


def _make_specialist_after_model_cb(tracker: InnerToolCallTracker):
    """Factory: returns an after_model_callback that increments the specialist sub-round
    and accumulates token usage from specialist LLM calls."""
    async def _after_model_cb(callback_context: CallbackContext, llm_response):
        invocation_id = callback_context.invocation_id
        sr = tracker.increment_sub_round(invocation_id)
        logger.debug(f"[InnerTracker] {callback_context.agent_name} invocation={invocation_id} sub_round → {sr}")
        # Capture token usage — specialist events are not visible in the outer runner
        # event stream, so we accumulate them here via the callback.
        if llm_response is not None:
            usage = getattr(llm_response, 'usage_metadata', None)
            if usage is not None:
                tracker.specialist_prompt_tokens += getattr(usage, 'prompt_token_count', 0) or 0
                tracker.specialist_output_tokens += getattr(usage, 'candidates_token_count', 0) or 0
                tracker.specialist_total_tokens += getattr(usage, 'total_token_count', 0) or 0
        return None  # do not modify the response
    return _after_model_cb


def _make_specialist_after_tool_cb(
    tracker: InnerToolCallTracker,
    extract_server_fn,
):
    """Factory: returns an after_tool_callback that records each MCP tool call."""
    async def _after_tool_cb(tool, args, tool_context, tool_response):
        invocation_id = tool_context.invocation_id
        agent_name = tool_context.agent_name
        tool_name = getattr(tool, 'name', str(tool))
        call_id = getattr(tool_context, 'function_call_id', None)
        server_name, _ = extract_server_fn(tool_name)

        is_success = True
        if isinstance(tool_response, dict):
            if tool_response.get('isError') or tool_response.get('error') or tool_response.get('exception'):
                is_success = False

        entry = {
            "type": "tool_execution",
            "tool": tool_name,
            "call_id": call_id,
            "server": server_name,
            "sub_round": tracker.current_sub_round(invocation_id),
            "parameters": args,
            "timestamp": time.time(),
            "success": is_success,
            "response": tool_response,
            "compressed": False,
        }
        if not is_success:
            entry["error"] = str(tool_response)

        tracker.record_tool_call(invocation_id, entry)
        logger.debug(f"[InnerTracker] {agent_name} invocation={invocation_id} → {tool_name} (sub_round={entry['sub_round']})")
        return None  # do not modify the response
    return _after_tool_cb


class ADKTaskExecutor:
    """Multi-agent task executor using Google ADK.
    
    This executor replaces the single-agent TaskExecutor with a multi-agent
    system where a coordinator routes tasks to specialist agents based on
    domain expertise. Each specialist has access to specific MCP servers.
    
    Attributes:
        server_configs: List of server configuration dictionaries
        config: Configuration for agent models
        coordinator: The coordinator agent (root of hierarchy)
        runner: ADK InMemoryRunner for execution
        
    The output interface is designed to match the original TaskExecutor
    for compatibility with the benchmark evaluation system.
    """
    
    APP_NAME = adk_config_loader.get_app_name()
    USER_ID = adk_config_loader.get_user_id()
    
    def __init__(
        self,
        server_configs: List[Dict[str, Any]],
        config: Optional[Config] = None,
        model_override: Optional[str] = None,
        required_servers: Optional[List[str]] = None,
    ):
        """Initialize the ADK task executor.
        
        Args:
            server_configs: List of server configuration dictionaries
            config: Optional Config instance for model configuration
            model_override: Optional model name to override default
            required_servers: Optional list of required server names for task
        """
        self.server_configs = server_configs
        self.config = config or Config()
        self.model_override = model_override
        self.required_servers = required_servers or []
        
        # These will be initialized in setup
        self.coordinator = None
        self.runner: Optional[InMemoryRunner] = None
        self.orchestrator: Optional[MultiAgentOrchestrator] = None
        
        # Langfuse trace plugin
        self.langfuse_plugin = LangfuseTracePlugin()
        self.count_invocation_plugin = CountInvocationPlugin()
        _compressor_model = (
            adk_config_loader.get_compression_compressor_model()
            or self.model_override
            or self.config.agent_settings.model
        )
        self.context_compressor_plugin = ContentCompressor(
            model_name=_compressor_model,
            token_threshold=adk_config_loader.get_compression_token_threshold(),
            tool_result_threshold=adk_config_loader.get_compression_tool_result_threshold(),
            hard_limit_threshold=adk_config_loader.get_compression_hard_limit_threshold(),
        )
        
        # Tracking variables (for interface compatibility)
        self.execution_results: List[Dict[str, Any]] = []
        
        # Token usage tracking
        self.total_output_tokens = 0
        self.total_prompt_tokens = 0
        self.total_tokens = 0

        # Map ADK tool name prefixes (derived from server names) to canonical server names
        self._server_prefix_map = self._build_server_prefix_map(server_configs)
        self.tools_of_required_servers = {}  # To store tools from required servers for evals
        
        # Routing mode controls whether specialists are sub_agents or AgentTools
        self.routing_mode = adk_config_loader.get_agent_routing_mode()
        # Populated during setup: maps agent-tool name -> specialist Agent (tools mode only)
        self._agent_tool_map: Dict[str, Any] = {}
        # Inner tool call tracker for tools mode (populated in setup)
        self._inner_tracker: Optional[InnerToolCallTracker] = None
        
        # Planning compliance tracking
        self._total_planned_tools = 0
        self._valid_planned_tools = 0
        
        logger.info("ADKTaskExecutor initialized")

    @staticmethod
    def _normalize_server_name(server_name: str) -> str:
        """Normalize server name using same strategy as tool prefix generation."""
        return server_name.replace(" ", "_").replace("-", "_").lower()

    def _build_server_prefix_map(self, server_configs: List[Dict[str, Any]]) -> Dict[str, str]:
        """Build mapping from ADK tool name prefix to original server name."""
        prefix_map: Dict[str, str] = {}
        for config in server_configs:
            server_name = config.get("name", "")
            if not server_name:
                continue
            safe_name = self._normalize_server_name(server_name)
            prefix_map[f"{safe_name}_"] = server_name
        return prefix_map

    def _extract_server_and_base_tool(self, tool_name: str) -> tuple[str, str]:
        """Extract server and base tool name from ADK tool identifier."""
        if not tool_name:
            return "", ""

        # ADK prefixed format support: <normalized_server>_<tool_name>
        for prefix in sorted(self._server_prefix_map.keys(), key=len, reverse=True):
            if tool_name.startswith(prefix):
                return self._server_prefix_map[prefix], tool_name[len(prefix):]

        return "", tool_name
    
    async def setup(self, server_names: Optional[List[str]] = None) -> None:
        """Set up the multi-agent hierarchy.
        
        This must be called before execute(). It creates the coordinator
        agent with specialist sub-agents based on available MCP servers.
        
        Args:
            server_names: Optional list of available server names
        """
        logger.info("Setting up ADK multi-agent system...")

        # Install custom asyncio exception handler to downgrade noisy MCP
        # session background-task errors from ERROR to WARNING level.
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(_mcp_asyncio_exception_handler)

        # Create the orchestrator and initialize agents
        self.orchestrator = MultiAgentOrchestrator(
            server_configs=self.server_configs,
            config=self.config,
            model_override=self.model_override,
        )
        
        self.coordinator = self.orchestrator.initialize(server_names)

        # In tools mode, attach callbacks to each specialist agent so that
        # inner MCP tool calls are captured in execution_results.
        if self.routing_mode == "tools":
            self._inner_tracker = InnerToolCallTracker()
            before_agent_cb = _make_specialist_before_agent_cb(self._inner_tracker)
            after_model_cb = _make_specialist_after_model_cb(self._inner_tracker)
            after_tool_cb = _make_specialist_after_tool_cb(
                self._inner_tracker,
                self._extract_server_and_base_tool,
            )
            for t in (self.coordinator.tools or []):
                if isinstance(t, AgentTool):
                    t.agent.before_agent_callback = before_agent_cb
                    t.agent.after_model_callback = after_model_cb
                    t.agent.after_tool_callback = after_tool_cb
                    logger.debug(f"Attached inner-tracking callbacks to specialist '{t.agent.name}'")

        # Eagerly preload MCP toolsets so MCP connections are established
        # during initialization instead of first user query execution.
        await self._preload_mcp_toolsets()
        
        # Create the ADK runner with Langfuse plugin
        self.runner = InMemoryRunner(
            agent=self.coordinator,
            app_name=self.APP_NAME,
            plugins=[self.langfuse_plugin, self.count_invocation_plugin, self.context_compressor_plugin],
        )
        
        # Log agent hierarchy info
        hierarchy_info = self.orchestrator.get_agent_hierarchy_info()
        logger.info(f"Agent hierarchy: {json.dumps(hierarchy_info, indent=2)}")
        
        logger.info("ADK multi-agent system setup complete")

    def iter_agents(self, agent):
        """Yields all agents in the tree (depth-first).

        Works for both routing modes:
        - sub_agents mode: recurses through agent.sub_agents
        - tools mode: recurses into the .agent of each AgentTool in agent.tools
        """
        yield agent
        # sub_agents path
        for sub in (agent.sub_agents or []):
            yield from self.iter_agents(sub)
        # tools path: recurse into AgentTool-wrapped agents
        for tool in (agent.tools or []):
            if isinstance(tool, AgentTool):
                yield from self.iter_agents(tool.agent)

    def _collect_agent_hierarchy(self) -> List[LlmAgent]:

        """Collect coordinator and all nested sub-agents.

        Returns:
            List of unique agents in the hierarchy.
        """
        collected_agents = []

        # Iterate all agents and inspect their tools
        for agent in self.iter_agents(self.coordinator):
            if isinstance(agent, LlmAgent):
                collected_agents.append(agent)
        return collected_agents

    async def _preload_mcp_toolsets(self) -> None:
        """Eagerly preload MCP toolsets for all agents and sub-agents. So that agent dosnt spent time comnntcting to mcps during execution.
        
        Also save info about tools from requreied servers for evals."""
        agents: List[LlmAgent] = self._collect_agent_hierarchy()
        if not agents:
            logger.warning("Skipping MCP preload: no agents available")
            return

        tools_of_required_servers = {}
        for agent in agents:
            agent_name = agent.name
            tools = await agent.canonical_tools()
            for tool in tools:
                if isinstance(tool, McpTool): 
                    server_name, base_tool_name = self._extract_server_and_base_tool(tool.name)
                    if server_name in self.required_servers:                
                        tools_of_required_servers[tool.name] = {
                            "name": tool.name,
                            "original_name": base_tool_name,
                            "server": server_name,
                            "description": tool.description,
                            "input_schema": tool.raw_mcp_tool.inputSchema,
                            "agent": agent_name,
                        }
        
        tool_name = "transfer_to_agent" 
        tools_of_required_servers[tool_name] = {
            "name": tool_name,
            "original_name": tool_name,
            "server": "adk_internal",
            "description": "Switch control to other agent",
            "input_schema": {"agent_name": "string"},
            "agent": agent_name,
        }
        self.tools_of_required_servers = tools_of_required_servers

        # ------------------------------------------------------------------ #
        # Inject routing-mechanism pseudo-tool so the evaluator never penalises
        # tool calls that are part of the routing infrastructure.
        # ------------------------------------------------------------------ #
        if self.routing_mode == "tools":
            # In tools mode the coordinator calls specialist agents by name
            # (e.g., "ResearcherAgent").  Register each agent-tool so the
            # evaluator recognises those names as valid.
            for tool in (self.coordinator.tools or []):
                if isinstance(tool, AgentTool):
                    agent_name = tool.agent.name
                    self._agent_tool_map[agent_name] = tool.agent
                    tools_of_required_servers[agent_name] = {
                        "name": agent_name,
                        "original_name": agent_name,
                        "server": "adk_agent_tool",
                        "description": tool.agent.description,
                        "input_schema": {"request": "string"},
                        "agent": "coordinator",
                    }
        else:
            # sub_agents mode: ADK emits transfer_to_agent function calls
            tool_name = "transfer_to_agent"
            tools_of_required_servers[tool_name] = {
                "name": tool_name,
                "original_name": tool_name,
                "server": "adk_internal",
                "description": "Switch control to other agent",
                "input_schema": {"agent_name": "string"},
                "agent": agent_name,
            }

        self.tools_of_required_servers = tools_of_required_servers
        
    async def execute(self, task: str) -> Dict[str, Any]:
        """Execute a task using the multi-agent system.
        
        This is the main entry point for task execution. The coordinator
        agent analyzes the task and routes it to appropriate specialist
        agents who have access to the relevant MCP tools.
        
        Args:
            task: Natural language description of the task to execute
            
        Returns:
            Dictionary containing:
                - solution: Final synthesized solution
                - total_rounds: Number of agent interactions
                - execution_results: List of all tool execution results
                - planning_json_compliance: Always 1.0 for ADK (handled internally)
                - total_output_tokens: Total output tokens used
                - total_prompt_tokens: Total prompt tokens used
                - total_tokens: Total tokens used
        """
        if self.runner is None:
            await self.setup()
        
        logger.info(f"Starting ADK multi-agent execution for task: \"{task}\"")
        start_time = time.time()
        
        # Reset tracking state
        self.execution_results = []
        self.total_output_tokens = 0
        self.total_prompt_tokens = 0
        self.total_tokens = 0
        # Reset specialist token accumulators for this task
        if self._inner_tracker is not None:
            self._inner_tracker.reset_tokens()
        # Reset compressor token counters for this task
        self.context_compressor_plugin.compression_prompt_tokens = 0
        self.context_compressor_plugin.compression_output_tokens = 0
        self.context_compressor_plugin.compression_total_tokens = 0
        
        # Create a new session for this task
        session = await self.runner.session_service.create_session(
            app_name=self.APP_NAME,
            user_id=self.USER_ID,
        )
        
        logger.info(f"Created session: {session.id}")
        
        # Execute the task via the coordinator
        final_response = ""
        events_collected = []
        
        # In tools mode, track coordinator rounds sequentially.
        # A new coordinator round starts each time the coordinator LLM
        # produces function_call(s) after we've seen responses for the
        # previous batch.
        _coordinator_round = 0
        _need_new_round = True  # start a new round on first function_call
        _is_tools_mode = self.routing_mode == "tools"
        
        try:
            # Create user message
            user_content = types.Content(
                role="user",
                parts=[types.Part.from_text(text=task)]
            )
            
            # Run the agent and collect events
            async for event in self.runner.run_async(
                user_id=self.USER_ID,
                session_id=session.id,
                new_message=user_content,
                run_config=RunConfig(),
            ):
                events_collected.append(event)
                
                # Process event for tracking
                self._process_event(event)
                
                # Extract final response from agent
                if event.content and event.author and event.author != "user":
                    for part in event.content.parts:
                        if hasattr(part, 'text') and part.text:
                            # Accumulate responses (last one is typically final)
                            final_response = part.text
                        
                        # Track tool calls
                        if hasattr(part, 'function_call') and part.function_call:
                            if _is_tools_mode:
                                # Coordinator round: bump on first call after responses
                                if _need_new_round:
                                    _coordinator_round += 1
                                    _need_new_round = False
                                self._track_tool_call(part.function_call, round_count=_coordinator_round)
                            else:
                                self._track_tool_call(part.function_call, round_count=self.count_invocation_plugin.llm_request_count) 
                        
                        if hasattr(part, 'function_response') and part.function_response:
                            self._track_tool_response(part.function_response)
                            if _is_tools_mode:
                                _need_new_round = True
            
        except Exception as e:
            logger.error(f"Error during ADK execution: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            final_response = f"Error during execution: {str(e)}"
        
        elapsed_time = time.time() - start_time
        logger.info(f"ADK execution completed in {elapsed_time:.2f}s with {self.count_invocation_plugin.llm_request_count} events")

        # Post-process: assign thread labels for parallel agent-tool calls
        if _is_tools_mode:
            self._assign_thread_labels()

        # Add tokens consumed by specialist agents (only visible via after_model_callback
        # in tools routing mode, not in the outer event stream).
        if self._inner_tracker is not None:
            self.total_prompt_tokens += self._inner_tracker.specialist_prompt_tokens
            self.total_output_tokens += self._inner_tracker.specialist_output_tokens
            self.total_tokens += self._inner_tracker.specialist_total_tokens
            if self._inner_tracker.specialist_total_tokens > 0:
                logger.info(
                    f"Specialist agent tokens included: "
                    f"prompt={self._inner_tracker.specialist_prompt_tokens}, "
                    f"output={self._inner_tracker.specialist_output_tokens}, "
                    f"total={self._inner_tracker.specialist_total_tokens}"
                )

        # Add tokens consumed by ContentCompressor LLM calls to the totals
        compressor_stats = self.context_compressor_plugin.get_stats()
        self.total_prompt_tokens += compressor_stats["compression_prompt_tokens"]
        self.total_output_tokens += compressor_stats["compression_output_tokens"]
        self.total_tokens += compressor_stats["compression_total_tokens"]
        if compressor_stats["compression_total_tokens"] > 0:
            logger.info(
                f"Compression LLM tokens included: prompt={compressor_stats['compression_prompt_tokens']}, "
                f"output={compressor_stats['compression_output_tokens']}, "
                f"total={compressor_stats['compression_total_tokens']}"
            )

        # Get trace ID from plugin (captured during execution)
        langfuse_trace_id = self.langfuse_plugin.trace_id
        
        # Calculate planning compliance (ADK handles this internally, so always 1.0)
        planning_json_compliance = 1.0
        
        return {
            "solution": final_response,
            "total_rounds": self.count_invocation_plugin.llm_request_count,
            "execution_results": self.execution_results,
            "planning_json_compliance": planning_json_compliance,
            "available_tools": self.tools_of_required_servers,
            "total_output_tokens": self.total_output_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_tokens": self.total_tokens,
            "adk_session_id": session.id,
            "langfuse_trace_id": langfuse_trace_id,
            "task_run_metadata": {
                "adk_session_id": session.id,
                "langfuse_trace_id": langfuse_trace_id,
                "runner": "adk"
            },
        }
    
    def _process_event(self, event) -> None:
        """Process an ADK event for tracking and logging.
        
        Args:
            event: ADK Event object
        """
        # Extract token usage if available
        if hasattr(event, 'usage_metadata') and event.usage_metadata:
            usage = event.usage_metadata
            if hasattr(usage, 'prompt_token_count'):
                self.total_prompt_tokens += usage.prompt_token_count or 0
            if hasattr(usage, 'candidates_token_count'):
                self.total_output_tokens += usage.candidates_token_count or 0
            if hasattr(usage, 'total_token_count'):
                self.total_tokens += usage.total_token_count or 0
        
        # Log event author and type
        author = getattr(event, 'author', 'unknown')
        logger.debug(f"Event from {author}")
    
    def _track_tool_call(self, function_call, round_count) -> None:
        """Track a tool call for execution results.
        
        Args:
            function_call: ADK FunctionCall object
            round_count: Current round number
        """
        tool_name = getattr(function_call, 'name', 'unknown')
        args = getattr(function_call, 'args', {})
        call_id = getattr(function_call, 'id', None)
        server_name, _ = self._extract_server_and_base_tool(tool_name)
        
        # In tools mode the coordinator calls specialist agents by their
        # ADK name, which won't match any server prefix.  Fall back to the
        # "adk_agent_tool" pseudo-server so tracking is consistent.
        if not server_name and tool_name in self._agent_tool_map:
            server_name = "adk_agent_tool"
        
        # In tools mode, register the coordinator call_id so before_agent_callback
        # can link it to the specialist's invocation_id.
        if self._inner_tracker and tool_name in self._agent_tool_map:
            self._inner_tracker.register_coordinator_call(tool_name, call_id)

        self.execution_results.append({
            "type": "tool_call",
            "tool": tool_name,
            "call_id": call_id,
            "server": server_name,
            "round": round_count,
            "parameters": args,
            "timestamp": time.time(),
            "success": False  # Will be updated when response is received
        })
            
        logger.info(f"Tool call: {tool_name}")


    
    def _track_tool_response(self, function_response) -> None:
        """Track a tool response for execution results.
        
        Args:
            function_response: ADK FunctionResponse object
        """
        tool_name = getattr(function_response, 'name', 'unknown')
        response = getattr(function_response, 'response', {})
        response_id = getattr(function_response, 'id', None)
        
        # Determine if the response indicates success or failure
        # Check for error indicators in the response
        is_success = True
        if isinstance(response, dict):
            # Check for common error indicators
            if response.get('isError') or response.get('error') or response.get('exception'):
                is_success = False
        
        # Check if Tool Result Compression was applied to this tool's result.
        # Use the response ID (== function_call_id) as the lookup key so that
        # parallel calls to the same tool don't get each other's compression info.
        compression_info = self.context_compressor_plugin.get_and_clear_compression_info(
            tool_name, call_id=response_id
        )

        # Match response to its tool call.
        # Prefer matching by call_id (exact pairing) so that multiple parallel
        # calls to the same tool are attributed correctly. Fall back to the
        # previous reversed-name scan only when no ID is available.
        for result in reversed(self.execution_results):
            if result.get("type") != "tool_call" or result.get("tool") != tool_name:
                continue
            # If both sides carry an ID, require it to match.
            if response_id and result.get("call_id") and result["call_id"] != response_id:
                continue
            result["response"] = response
            result["type"] = "tool_execution"
            result["success"] = is_success
            if not is_success:
                result["error"] = str(response)
            # Annotate with compression details when Tool Result Compression fired
            if compression_info:
                result["compressed"] = True
                result["compression_method"] = compression_info.get("method", "unknown")
                result["compression_tokens_before"] = compression_info["tokens_before"]
                result["compression_tokens_after"] = compression_info["tokens_after"]
                result["compression_tokens_saved"] = (
                    compression_info["tokens_before"] - compression_info["tokens_after"]
                )
            else:
                result["compressed"] = False
            # In tools mode, attach inner specialist tool calls.
            # Pass response_id (== coordinator call_id) so that completion order
            # does not affect attribution.
            if self._inner_tracker and tool_name in self._agent_tool_map:
                result["inner_tool_calls"] = self._inner_tracker.pop_calls(tool_name, response_id)
            break
        else:
            raise ValueError(f"No matching tool call found for response: {tool_name}")

        logger.debug(
            f"Tool response: {tool_name} (success={is_success}"
            + (f", compressed {compression_info['tokens_before']}→{compression_info['tokens_after']} tokens)" if compression_info else ")")
        )

    def _assign_thread_labels(self) -> None:
        """Assign thread labels for parallel agent-tool calls in the same coordinator round.

        For each coordinator round that contains multiple agent-tool entries,
        assigns ``thread`` = "a", "b", "c", … in appearance order.  Rounds with
        a single agent-tool call (or non-agent-tool entries) get ``thread = None``.
        """
        # Group agent-tool indices by round
        round_indices: Dict[int, List[int]] = defaultdict(list)
        for idx, entry in enumerate(self.execution_results):
            if entry.get("server") == "adk_agent_tool":
                round_indices[entry["round"]].append(idx)

        labels = list(string.ascii_lowercase)  # a-z

        for round_num, indices in round_indices.items():
            if len(indices) > 1:
                for i, idx in enumerate(indices):
                    self.execution_results[idx]["thread"] = labels[i] if i < len(labels) else str(i)
            else:
                self.execution_results[indices[0]]["thread"] = None

        # Non-agent-tool entries don't get a thread label
        for entry in self.execution_results:
            if "thread" not in entry:
                entry["thread"] = None
    
    def _serialize_state(self, state) -> str:
        """Serialize session state to string format.
        
        Args:
            state: Session state object or dict
            
        Returns:
            String representation of the state
        """
        if state is None:
            return ""
        
        # Convert to dict if it has a to_dict method
        if hasattr(state, 'to_dict'):
            state_dict = state.to_dict()
        elif isinstance(state, dict):
            state_dict = state
        else:
            return str(state)
        
        # Filter out internal ADK state keys
        filtered_state = {
            k: v for k, v in state_dict.items()
            if not k.startswith('_adk')
        }
        
        if not filtered_state:
            return ""
        
        try:
            return json.dumps(filtered_state, indent=2, default=str)
        except Exception:
            return str(filtered_state)
    
    def _log_tools_token_stats(self) -> None:
        """Log token consumption statistics for tool descriptions.
        
        This method provides compatibility with the original TaskExecutor
        logging interface.
        """
        if self.orchestrator:
            info = self.orchestrator.get_agent_hierarchy_info()
            total_tools = sum(
                s.get("tool_count", 0) 
                for s in info.get("specialists", [])
            )
            logger.info(f"Total tools across specialists: {total_tools}")
    
    async def cleanup(self) -> None:
        """Clean up resources including MCP connections and ADK runner.
        
        This ensures all MCP server connections are properly closed and
        background tasks are terminated, allowing the process to exit cleanly.
        """
        logger.info("Cleaning up ADK executor resources...")
        
        try:
            # Close MCP toolset connections for all agents
            agents = self._collect_agent_hierarchy()
            for agent in agents:
                agent_name = getattr(agent, "name", "unknown")
                tools = getattr(agent, "tools", None) or []
                
                for tool in tools:
                    # Check if this is an MCP toolset with a close method
                    if hasattr(tool, "_session_manager"):
                        try:
                            session_mgr = tool._session_manager
                            if hasattr(session_mgr, "close"):
                                logger.debug(f"Closing MCP session for agent '{agent_name}'")
                                await session_mgr.close()
                            elif hasattr(session_mgr, "cleanup"):
                                logger.debug(f"Cleaning up MCP session for agent '{agent_name}'")
                                await session_mgr.cleanup()
                        except Exception as e:
                            logger.warning(f"Error closing MCP session for agent '{agent_name}': {e}")
            
            # In tools mode, also clean up the MCP toolsets inside AgentTool
            # inner agents (they are not in coordinator.sub_agents).
            if self.routing_mode == "tools" and self.coordinator:
                for t in (self.coordinator.tools or []):
                    if isinstance(t, AgentTool):
                        inner_agent = t.agent
                        inner_name = getattr(inner_agent, "name", "unknown")
                        for inner_tool in (getattr(inner_agent, "tools", None) or []):
                            if hasattr(inner_tool, "_session_manager"):
                                try:
                                    sm = inner_tool._session_manager
                                    if hasattr(sm, "close"):
                                        await sm.close()
                                    elif hasattr(sm, "cleanup"):
                                        await sm.cleanup()
                                except Exception as e:
                                    logger.warning(f"Error closing MCP session inside AgentTool '{inner_name}': {e}")
            
            # Clear references
            self.coordinator = None
            self.runner = None
            self.orchestrator = None
            
            logger.info("ADK executor cleanup complete")
            
        except Exception as e:
            logger.error(f"Error during ADK executor cleanup: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")


async def create_adk_executor(
    server_configs: List[Dict[str, Any]],
    model_override: Optional[str] = None,
    server_names: Optional[List[str]] = None,
) -> ADKTaskExecutor:
    """Factory function to create and set up an ADK executor.
    
    Args:
        server_configs: List of server configuration dictionaries
        model_override: Optional model name override
        server_names: Optional list of available servers
        
    Returns:
        Configured ADKTaskExecutor ready for execution
    """
    config = Config()
    executor = ADKTaskExecutor(
        server_configs=server_configs,
        config=config,
        model_override=model_override,
    )
    await executor.setup(server_names)
    return executor
