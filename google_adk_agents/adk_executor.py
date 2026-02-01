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

from .config import Config
from .coordinator import create_coordinator_agent, MultiAgentOrchestrator

logger = logging.getLogger(__name__)


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
        concurrent_summarization: bool = False,  # For interface compatibility
    ):
        """Initialize the ADK task executor.
        
        Args:
            server_configs: List of server configuration dictionaries
            config: Optional Config instance for model configuration
            model_override: Optional model name to override default
            concurrent_summarization: Ignored, kept for interface compatibility
        """
        self.server_configs = server_configs
        self.config = config or Config()
        self.model_override = model_override
        self.concurrent_summarization = concurrent_summarization
        
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
        
        # Planning compliance tracking
        self._total_planned_tools = 0
        self._valid_planned_tools = 0
        
        logger.info("ADKTaskExecutor initialized")
    
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
        
        # Calculate planning compliance (ADK handles this internally, so always 1.0)
        planning_json_compliance = 1.0
        
        return {
            "solution": final_response,
            "total_rounds": len(self.execution_results),
            "execution_results": self.execution_results,
            "planning_json_compliance": planning_json_compliance,
            "accumulated_information": self.accumulated_information,
            "accumulated_information_uncompressed": self.accumulated_information_uncompressed,
            "total_output_tokens": self.total_output_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_tokens": self.total_tokens,
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
        
        self.execution_results.append({
            "type": "tool_call",
            "tool": tool_name,
            "parameters": args,
            "timestamp": time.time(),
        })
        
        logger.info(f"Tool call: {tool_name}")
    
    def _track_tool_response(self, function_response) -> None:
        """Track a tool response for execution results.
        
        Args:
            function_response: ADK FunctionResponse object
        """
        tool_name = getattr(function_response, 'name', 'unknown')
        response = getattr(function_response, 'response', {})
        
        # Append response to the last tool call if it matches
        for result in reversed(self.execution_results):
            if result.get("type") == "tool_call" and result.get("tool") == tool_name:
                result["response"] = response
                result["type"] = "tool_execution"
                break
        else:
            # No matching call found, add as separate result
            self.execution_results.append({
                "type": "tool_response",
                "tool": tool_name,
                "response": response,
                "timestamp": time.time(),
            })
        
        logger.debug(f"Tool response: {tool_name}")
    
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
