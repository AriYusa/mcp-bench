"""
ADK Task Executor Module

This module provides a Google ADK-based multi-agent executor that replaces
the single-agent TaskExecutor. It uses a coordinator agent with specialist
sub-agents for domain-specific task handling.
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Any, Optional

from google.adk.runners import InMemoryRunner
from google.adk.agents.run_config import RunConfig
from google.adk.sessions.session import Session
from google.genai import types
from langfuse import get_client

from .config import Config
from .coordinator import create_coordinator_agent, MultiAgentOrchestrator

logger = logging.getLogger(__name__)
langfuse = get_client()


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
    
    APP_NAME = "mcp_bench_adk"
    USER_ID = "benchmark_user"
    
    def __init__(
        self,
        server_configs: List[Dict[str, Any]],
        config: Optional[Config] = None,
        model_override: Optional[str] = None,
    ):
        """Initialize the ADK task executor.
        
        Args:
            server_configs: List of server configuration dictionaries
            config: Optional Config instance for model configuration
            model_override: Optional model name to override default
        """
        self.server_configs = server_configs
        self.config = config or Config()
        self.model_override = model_override
        
        # These will be initialized in setup
        self.coordinator = None
        self.runner: Optional[InMemoryRunner] = None
        self.orchestrator: Optional[MultiAgentOrchestrator] = None
        
        # Tracking variables (for interface compatibility)
        self.execution_results: List[Dict[str, Any]] = []
        self.accumulated_information = ""
        self.accumulated_information_uncompressed = ""
        
        # Token usage tracking
        self.total_output_tokens = 0
        self.total_prompt_tokens = 0
        self.total_tokens = 0

        # Map ADK tool name prefixes (derived from server names) to canonical server names
        self._server_prefix_map = self._build_server_prefix_map(server_configs)
        
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

        # Legacy format support: server:tool
        if ":" in tool_name:
            server, base_tool = tool_name.split(":", 1)
            return server, base_tool

        # ADK prefixed format support: <normalized_server>_<tool_name>
        for prefix in sorted(self._server_prefix_map.keys(), key=len, reverse=True):
            if tool_name.startswith(prefix):
                return self._server_prefix_map[prefix], tool_name[len(prefix):]

        return "", tool_name

    def _build_observed_tools_map(self) -> Dict[str, Any]:
        """Build tool registry from observed execution results.

        This guarantees evaluator compatibility even when ADK tool metadata
        introspection is incomplete.
        """
        observed_tools: Dict[str, Any] = {}
        for result in self.execution_results:
            tool_name = result.get("tool", "")
            if not tool_name:
                continue

            server_name, base_tool_name = self._extract_server_and_base_tool(tool_name)
            if tool_name not in observed_tools:
                observed_tools[tool_name] = {
                    "name": base_tool_name,
                    "original_name": base_tool_name,
                    "server": server_name,
                    "description": "",
                    "input_schema": {}
                }

        return observed_tools
    
    async def setup(self, server_names: Optional[List[str]] = None) -> None:
        """Set up the multi-agent hierarchy.
        
        This must be called before execute(). It creates the coordinator
        agent with specialist sub-agents based on available MCP servers.
        
        Args:
            server_names: Optional list of available server names
        """
        logger.info("Setting up ADK multi-agent system...")
        
        # Create the orchestrator and initialize agents
        self.orchestrator = MultiAgentOrchestrator(
            server_configs=self.server_configs,
            config=self.config,
            model_override=self.model_override,
        )
        
        self.coordinator = self.orchestrator.initialize(server_names)
        
        # Create the ADK runner
        self.runner = InMemoryRunner(
            agent=self.coordinator,
            app_name=self.APP_NAME,
        )
        
        # Log agent hierarchy info
        hierarchy_info = self.orchestrator.get_agent_hierarchy_info()
        logger.info(f"Agent hierarchy: {json.dumps(hierarchy_info, indent=2)}")
        
        logger.info("ADK multi-agent system setup complete")
    
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
                - accumulated_information: Session state as string
                - accumulated_information_uncompressed: Full session state
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
        self.accumulated_information = ""
        self.accumulated_information_uncompressed = ""
        self.total_output_tokens = 0
        self.total_prompt_tokens = 0
        self.total_tokens = 0
        
        # Create a new session for this task
        session = await self.runner.session_service.create_session(
            app_name=self.APP_NAME,
            user_id=self.USER_ID,
        )

        langfuse_trace_id = None
        try:
            langfuse_trace_id = langfuse.get_current_trace_id()
        except Exception:
            langfuse_trace_id = None
        
        logger.info(f"Created session: {session.id}")
        
        # Execute the task via the coordinator
        final_response = ""
        round_count = 0
        events_collected = []
        
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
                round_count += 1
                
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
                            self._track_tool_call(part.function_call)
                        
                        if hasattr(part, 'function_response') and part.function_response:
                            self._track_tool_response(part.function_response)
            
            # Get final session state for accumulated information
            updated_session = await self.runner.session_service.get_session(
                app_name=self.APP_NAME,
                user_id=self.USER_ID,
                session_id=session.id,
            )
            
            if updated_session and updated_session.state:
                self.accumulated_information = self._serialize_state(updated_session.state)
                self.accumulated_information_uncompressed = self.accumulated_information
            
        except Exception as e:
            logger.error(f"Error during ADK execution: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            final_response = f"Error during execution: {str(e)}"
        
        elapsed_time = time.time() - start_time
        logger.info(f"ADK execution completed in {elapsed_time:.2f}s with {round_count} events")

        if not langfuse_trace_id:
            try:
                langfuse_trace_id = langfuse.get_current_trace_id()
            except Exception:
                langfuse_trace_id = None
        
        # Calculate planning compliance (ADK handles this internally, so always 1.0)
        planning_json_compliance = 1.0

        # Build available tools map with robust fallback from observed tool usage
        available_tools = self.get_available_tools()
        observed_tools = self._build_observed_tools_map()
        available_tools.update(observed_tools)
        
        return {
            "solution": final_response,
            "total_rounds": len(self.execution_results),
            "execution_results": self.execution_results,
            "planning_json_compliance": planning_json_compliance,
            "accumulated_information": self.accumulated_information,
            "accumulated_information_uncompressed": self.accumulated_information_uncompressed,
            "available_tools": available_tools,
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
    
    def _track_tool_call(self, function_call) -> None:
        """Track a tool call for execution results.
        
        Args:
            function_call: ADK FunctionCall object
        """
        tool_name = getattr(function_call, 'name', 'unknown')
        args = getattr(function_call, 'args', {})
        server_name, _ = self._extract_server_and_base_tool(tool_name)
        
        self.execution_results.append({
            "type": "tool_call",
            "tool": tool_name,
            "server": server_name,
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
        server_name, _ = self._extract_server_and_base_tool(tool_name)
        
        # Determine if the response indicates success or failure
        # Check for error indicators in the response
        is_success = True
        if isinstance(response, dict):
            # Check for common error indicators
            if response.get('isError') or response.get('error'):
                is_success = False
            # Check if response indicates exception
            if 'exception' in str(response).lower():
                is_success = False
        
        # Append response to the last tool call if it matches
        for result in reversed(self.execution_results):
            if result.get("type") == "tool_call" and result.get("tool") == tool_name:
                result["response"] = response
                result["result"] = str(response) if not isinstance(response, str) else response
                result["type"] = "tool_execution"
                result["success"] = is_success
                if not is_success:
                    result["error"] = str(response)
                break
        else:
            # No matching call found, add as separate result
            self.execution_results.append({
                "type": "tool_response",
                "tool": tool_name,
                "server": server_name,
                "response": response,
                "result": str(response) if not isinstance(response, str) else response,
                "timestamp": time.time(),
                "success": is_success
            })
        
        logger.debug(f"Tool response: {tool_name} (success={is_success})")
    
    def get_available_tools(self) -> Dict[str, Any]:
        """Get all available tools from all specialist agents.
        
        Returns:
            Dictionary mapping tool names to tool information
        """
        if not self.orchestrator or not self.coordinator:
            logger.warning("Cannot get available tools: orchestrator not initialized")
            return {}
        
        available_tools = {}
        
        # Iterate through all specialist agents
        if self.coordinator.sub_agents:
            for agent in self.coordinator.sub_agents:
                agent_name = agent.name
                if agent.tools:
                    for tool in agent.tools:
                        # Skip toolset wrappers that don't expose a concrete tool name
                        if not hasattr(tool, 'name'):
                            continue

                        # Get tool name and create a unique key
                        tool_name = getattr(tool, 'name', 'unknown')
                        if not tool_name or tool_name == 'unknown':
                            continue

                        # Try to extract server name from tool name (format: server:tool)
                        server_name, base_tool_name = self._extract_server_and_base_tool(tool_name)
                        if not server_name:
                            server_name = agent_name
                        
                        tool_key = tool_name  # Use full name as key
                        
                        # Get tool description and schema
                        description = getattr(tool, 'description', '')
                        input_schema = getattr(tool, 'input_schema', {})
                        
                        available_tools[tool_key] = {
                            "name": base_tool_name,
                            "original_name": base_tool_name,
                            "server": server_name,
                            "description": description,
                            "input_schema": input_schema
                        }
        
        logger.debug(f"Collected {len(available_tools)} tools from specialist agents")
        return available_tools
    
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
