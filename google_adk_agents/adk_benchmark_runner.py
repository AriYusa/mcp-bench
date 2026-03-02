#!/usr/bin/env python3
"""ADK-based Benchmark Runner for MCP-Bench.

This module provides a simplified benchmark runner that uses Google ADK's
multi-agent system (ADKTaskExecutor) instead of the single-agent TaskExecutor.
ADK handles MCP server connections and LLM provider management internally,
eliminating the need for ConnectionManager and LLMProvider.

Classes:
    ADKBenchmarkRunner: Benchmark orchestrator using ADK multi-agent system
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
from langfuse import get_client

# Add parent directory to Python path to resolve imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google_adk_agents.adk_executor import ADKTaskExecutor
from benchmark.evaluator import TaskEvaluator
from benchmark.results_aggregator import ResultsAggregator
from utils.local_server_config import LocalServerConfigLoader
import google_adk_agents.adk_config_loader as adk_config_loader
from llm.provider import LLMProvider

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

langfuse = get_client()

class ADKBenchmarkRunner:
    """ADK-based benchmark runner for testing multiple LLM models.
    
    This runner uses Google ADK's multi-agent system for task execution,
    eliminating the need for ConnectionManager (ADK handles connections)
    and LLMProvider (ADK uses its own model configuration).
    
    Key differences from BenchmarkRunner:
    - No ConnectionManager - ADK manages server connections internally
    - No LLMProvider - passes model name string to ADKTaskExecutor
    - Simplified execution flow with ADK's built-in agent orchestration
    
    Attributes:
        tasks_file: Path to the main tasks JSON file
        local_config_loader: Loader for local server configurations
        enable_distraction_servers: Whether to include distraction servers
        distraction_count: Number of distraction servers to include
        enable_judge_stability: Whether to enable judge stability checks
        filter_problematic_tools: Whether to filter known problematic tools
        concurrent_summarization: Whether to summarize results concurrently
        use_fuzzy_descriptions: Whether to use fuzzy task descriptions
        aggregator: Results aggregator instance
    """
    
    def __init__(
        self, 
        tasks_file: Optional[str] = None,
        enable_distraction_servers: Optional[bool] = None, 
        distraction_count: Optional[int] = None, 
        enable_judge_stability: Optional[bool] = None, 
        filter_problematic_tools: Optional[bool] = None, 
        concurrent_summarization: Optional[bool] = None, 
        use_fuzzy_descriptions: Optional[bool] = None,
        # Dependency injection parameters
        local_config_loader: Optional[LocalServerConfigLoader] = None,
        aggregator: Optional[ResultsAggregator] = None,
        judge_provider: Optional[Any] = None
    ) -> None:
        """Initialize ADK benchmark runner.
        
        Args:
            tasks_file: Path to tasks JSON file
            enable_distraction_servers: Whether to include distraction servers
            distraction_count: Number of distraction servers
            enable_judge_stability: Enable judge stability checks
            filter_problematic_tools: Filter known problematic tools
            concurrent_summarization: Summarize results concurrently
            use_fuzzy_descriptions: Use fuzzy task descriptions
            local_config_loader: Injected config loader
            aggregator: Injected results aggregator
            judge_provider: Injected judge LLM provider
        """
        # Use config file defaults if not explicitly provided
        self.tasks_file = tasks_file or adk_config_loader.get_tasks_file()
        
        # Use injected dependencies or create defaults
        self.local_config_loader = local_config_loader or LocalServerConfigLoader()
        self._judge_provider = judge_provider  # Store injected judge provider
        
        # Use config file defaults for feature flags
        self.enable_distraction_servers = enable_distraction_servers if enable_distraction_servers is not None else True
        self.distraction_count = distraction_count if distraction_count is not None else adk_config_loader.get_distraction_servers_count()
        self.enable_judge_stability = enable_judge_stability if enable_judge_stability is not None else adk_config_loader.is_judge_stability_enabled()
        self.filter_problematic_tools = filter_problematic_tools if filter_problematic_tools is not None else adk_config_loader.is_problematic_tools_filter_enabled()
        self.concurrent_summarization = concurrent_summarization if concurrent_summarization is not None else adk_config_loader.is_concurrent_summarization_enabled()
        self.use_fuzzy_descriptions = use_fuzzy_descriptions if use_fuzzy_descriptions is not None else adk_config_loader.use_fuzzy_descriptions()
        self.enable_concrete_description_ref = adk_config_loader.is_concrete_description_ref_enabled()
        self.commands_config = None
        
        # Track current cumulative metrics for error handling
        self.last_cumulative_metrics = {}

        # Catalog loaded lazily from mcp_servers_info.json for reliable available_tools
        self._mcp_servers_catalog: Optional[Dict[str, Any]] = None
        self._mcp_servers_catalog_path = adk_config_loader.get_servers_catalog_path()
        
        # Initialize results handling components (use injected or create defaults)
        self.aggregator = aggregator or ResultsAggregator()
        
        # ADK-specific: List of available model names (ADK handles model configs internally)
        self.model_configs = adk_config_loader.get_available_models()

        # Per-task results directory (lazy, set on first call to _get_results_dir)
        self._results_dir: Optional[Path] = None
        
    async def load_tasks(self) -> List[Dict[str, Any]]:
        """Load benchmark tasks from JSON file.
        
        Loads and flattens tasks from various JSON formats including
        server_tasks, multi-server tasks, and combination-based tasks.
        
        Returns:
            List of task dictionaries containing task information
            
        Raises:
            FileNotFoundError: If the tasks file doesn't exist
            json.JSONDecodeError: If the file contains invalid JSON
        """
        logger.info(f"Loading tasks from {self.tasks_file}")
        
        try:
            with open(self.tasks_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if 'server_tasks' in data:
                # Flatten nested task structure
                flattened_tasks = []
                for server_group in data['server_tasks']:
                    server_name = server_group.get('server_name', '')
                    required_servers = server_group.get('servers', [])
                    # Handle both 'task' (single) and 'tasks' (array) formats
                    if 'task' in server_group:
                        # Single task format
                        flattened_tasks.append({
                            'server_name': server_name,
                            'task': server_group['task'],
                            'required_servers': required_servers
                        })
                    elif 'tasks' in server_group:
                        # Multiple tasks format
                        for task in server_group.get('tasks', []):
                            flattened_tasks.append({
                                'server_name': server_name,
                                'task': task,
                                'required_servers': required_servers
                            })
                tasks = flattened_tasks
            elif 'tasks' in data:
                # Multi-server task format (converted from multiserver generation)
                tasks = data['tasks']
            elif 'combinations' in data:
                # Handle combination-based task format
                flattened_tasks = []
                for combination in data['combinations']:
                    combination_name = combination.get('combination_name', 'Unknown')
                    servers = combination.get('servers', [])
                    server_name = '+'.join(servers) if servers else combination_name
                    
                    for task in combination.get('generated_tasks', []):
                        flattened_tasks.append({
                            'server_name': server_name,
                            'task': task
                        })
                tasks = flattened_tasks
            else:
                tasks = data
                
            logger.info(f"Loaded {len(tasks)} tasks from file")
            return tasks
            
        except Exception as e:
            logger.error(f"ERROR in loading tasks: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            raise
    
    async def load_server_configs(self) -> Dict[str, Any]:
        """Load local MCP server configurations."""
        logger.info(f"Loading local server configurations")
        
        try:
            # Return local_commands directly as it's already in the right format
            servers = self.local_config_loader.local_commands
            logger.info(f"Loaded configurations for {len(servers)} local servers")
            return servers
            
        except Exception as e:
            logger.error(f"ERROR in loading server configurations: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            raise
    
    def map_server_name_to_config(self, server_name: str, servers_info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Map single server name to actual server configuration.
        
        Server name should be the local server name (e.g., "National Parks", "DEX Paprika")
        Multi-server combinations should be handled by the caller.
        """
        
        # Direct lookup for local servers
        if server_name in servers_info:
            server_config = servers_info[server_name]
            cmd_parts = server_config.get('cmd', '').split()
            
            if not cmd_parts:
                logger.warning(f"Empty command for server: {server_name}")
                return None
            
            # Use cwd path directly from commands.json
            cwd_path = server_config.get('cwd', '')
            if cwd_path.startswith('../'):
                # Handle relative path, convert to absolute path
                actual_cwd = f"mcp_servers/{cwd_path[3:]}"
            else:
                actual_cwd = cwd_path
            
            # Build environment variables
            env = {}
            for env_var in server_config.get('env', []):
                if env_var in self.local_config_loader.api_keys:
                    env[env_var] = self.local_config_loader.api_keys[env_var]
            
            # Build base configuration
            config = {
                'name': server_name,
                'command': cmd_parts,
                'env': env,
                'cwd': actual_cwd
            }
            
            # Add HTTP configuration if this is an HTTP server
            if server_config.get('transport') == 'http':
                config['transport'] = 'http'
                config['port'] = server_config.get('port', adk_config_loader.get_default_port())
                config['endpoint'] = server_config.get('endpoint', '/mcp')
            
            return config
        
        # Log available servers for debugging
        logger.warning(f"No configuration found for server: {server_name}")
        logger.debug(f"Available servers: {list(servers_info.keys())}")
        return None
    
    async def load_commands_config(self) -> Dict[str, Any]:
        """Load MCP server commands configuration from commands.json."""
        commands_file = "mcp_servers/commands.json"
        logger.info(f"Loading commands configuration from {commands_file}")
        
        try:
            with open(commands_file, 'r', encoding='utf-8') as f:
                commands_config = json.load(f)
            
            logger.info(f"Loaded commands for {len(commands_config)} servers")
            return commands_config
            
        except Exception as e:
            logger.error(f"ERROR in loading commands configuration: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return {}
    
    def select_random_distraction_servers(self, excluded_server_names: List[str], commands_config: Dict[str, Any], count: int = None) -> List[Dict[str, Any]]:
        """Select random distraction servers excluding the specified servers."""
        
        if count is None:
            count = adk_config_loader.get_distraction_servers_count()
        
        if not commands_config:
            return []
        
        # Get all available server names excluding the already included servers
        available_servers = [name for name in commands_config.keys() if name not in excluded_server_names]
        
        # Randomly select up to 'count' servers
        selected_count = min(count, len(available_servers))
        selected_servers = random.sample(available_servers, selected_count)
        
        # Convert to server config format using the same method as target servers
        distraction_configs = []
        for server_name in selected_servers:
            # Use the existing mapping method to ensure consistent format
            # Create a temporary servers_info dict with the single server
            temp_servers_info = {server_name: commands_config[server_name]}
            distraction_config = self.map_server_name_to_config(server_name, temp_servers_info)
            if distraction_config:
                distraction_configs.append(distraction_config)
            else:
                logger.warning(f"Failed to create config for distraction server: {server_name}")
        
        logger.info(f"Selected {len(distraction_configs)} distraction servers: {[s['name'] for s in distraction_configs]}")
        return distraction_configs
    
    async def execute_single_task_with_model(
        self, 
        task_info: Dict[str, Any], 
        servers_info: Dict[str, Any], 
        model_name: str, 
        max_retries: Optional[int] = None, 
        timeout_seconds: Optional[int] = None
    ) -> Dict[str, Any]:
        """Execute a single task with ADK multi-agent system.
        
        This is the simplified ADK version that doesn't need ConnectionManager
        or LLMProvider - ADK handles both internally.
        
        Args:
            task_info: Dictionary containing task details (id, description, etc.)
            servers_info: Dictionary of available server configurations
            model_name: Name of the LLM model to use (e.g., "gemini-2.0-flash-exp")
            max_retries: Maximum retry attempts (uses config default if None)
            timeout_seconds: Execution timeout (uses config default if None)
            
        Returns:
            Dictionary with execution results including status, result/error,
            execution time, and optional judge score
        """
        
        # Set default values from config
        if max_retries is None:
            max_retries = adk_config_loader.get_max_retries()
        if timeout_seconds is None:
            timeout_seconds = adk_config_loader.get_task_timeout()
        
        # Initialize judge provider once for this task execution
        if not hasattr(self, '_judge_provider') or self._judge_provider is None:
            self._judge_provider = LLMProvider(adk_config_loader.get_judge_model())
        
        # Step 1: Prepare task execution information
        task_execution_info = await self._prepare_task_execution(task_info)
        logger.info(f"[ADK] Executing task {task_execution_info['task_id']} with model {model_name} using {task_execution_info['description_type']} description{task_execution_info['ref_info']}")
        
        # Step 2: Prepare server configurations
        server_config_result = await self._prepare_server_configs(task_execution_info['server_name'], servers_info, task_execution_info['task_data'])
        if server_config_result['status'] == 'failed':
            return {
                'task_id': task_execution_info['task_id'],
                'server_name': task_execution_info['server_name'],
                'model_name': model_name,
                'status': 'failed',
                'error': server_config_result['error'],
                'execution_time': 0,
                'task_run_metadata': {
                    'adk_session_id': None,
                    'langfuse_trace_id': None,
                    'runner': 'adk'
                }
            }
        
        all_server_configs = server_config_result['all_server_configs']
        
        # Step 3: Execute task with ADK (with retry mechanism)
        task_id = task_execution_info['task_id']
        task_description = task_execution_info['task_description']
        
        execution_result = None
        for attempt in range(max_retries):
            start_time = time.time()
            executor = None
            
            try:
                logger.info(f"[ADK] Attempt {attempt + 1}/{max_retries} for task {task_id}")
                
                # Create ADK executor with server configs
                # ADK handles connection lifecycle internally - no ConnectionManager needed
                executor = ADKTaskExecutor(
                    server_configs=all_server_configs,
                    model_override=model_name,  # Pass model name directly
                    required_servers=task_execution_info.get('required_servers', []),
                )
                
                # Setup ADK multi-agent system
                server_names = [config['name'] for config in all_server_configs]
                await executor.setup(server_names)
                
                # Execute task with timeout
                task_execution_start_time = time.time()
                execution_start_time = time.time()
                
                try:
                    logger.info(f"[ADK] Running multi-agent execution for task {task_id}")
                    result = await asyncio.wait_for(
                        executor.execute(task_description),
                        timeout=timeout_seconds
                    )
                    execution_time = time.time() - execution_start_time
                    logger.info(f"[ADK] Task execution completed in {execution_time:.2f}s")
                    
                    if not isinstance(result, dict):
                        logger.error(f"[ADK] execute returned {type(result)} instead of dict: {result}")
                        raise TypeError(f"execute returned {type(result)}, expected dict")
                    
                    # Available tools are now included in the result from executor
                    # No need to override - the executor's get_available_tools() is already called
                    
                    # Successful execution, prepare return result
                    execution_result = {
                        'status': 'completed',
                        'result': result,
                        'execution_time': time.time() - start_time,
                        'agent_execution_time': time.time() - execution_start_time,
                        'task_execution_start_time': task_execution_start_time,
                        'task_run_metadata': result.get('task_run_metadata', {})
                    }
                    
                    # Break out of retry loop
                    break
                    
                except asyncio.TimeoutError:
                    logger.warning(f"[ADK] Task {task_id} timed out after {timeout_seconds} seconds on attempt {attempt + 1}")
                    
                    if attempt < max_retries - 1:
                        logger.info(f"[ADK] Will retry task {task_id}...")
                        await asyncio.sleep(adk_config_loader.get_retry_delay())
                        continue
                    else:
                        return {
                            'task_id': task_id,
                            'server_name': task_execution_info['server_name'],
                            'model_name': model_name,
                            'status': 'failed',
                            'error': f'Task timed out after {max_retries} attempts',
                            'execution_time': timeout_seconds * max_retries,
                            'task_run_metadata': {
                                'adk_session_id': None,
                                'langfuse_trace_id': None,
                                'runner': 'adk'
                            }
                        }
                        
            except Exception as e:
                logger.error(f"[ADK] Error executing task {task_id} with model {model_name} on attempt {attempt + 1}: {e}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                
                if attempt < max_retries - 1:
                    logger.info(f"[ADK] Will retry task {task_id} due to error...")
                    await asyncio.sleep(adk_config_loader.get_retry_delay())
                    continue
                else:
                    return {
                        'task_id': task_id,
                        'server_name': task_execution_info['server_name'],
                        'model_name': model_name,
                        'status': 'failed',
                        'error': str(e),
                        'execution_time': time.time() - start_time,
                        'task_run_metadata': {
                            'adk_session_id': None,
                            'langfuse_trace_id': None,
                            'runner': 'adk'
                        }
                    }
            
            finally:
                # Always cleanup the executor after each attempt
                if executor is not None:
                    try:
                        await executor.cleanup()
                    except Exception as cleanup_error:
                        logger.warning(f"Error during executor cleanup: {cleanup_error}")
        
        if execution_result is None:
            raise RuntimeError(f"Task {task_id} execution completed retry loop without returning - this is a bug")
        
        # Step 4: Evaluate task result and format output
        final_result = await self._evaluate_task_result(
            task_execution_info, execution_result, model_name, task_execution_info['server_name'])
        
        return final_result
    
    async def _prepare_task_execution(self, task_info: Dict[str, Any]) -> Dict[str, Any]:
        """Prepare task execution information from task_info."""
        task_data = task_info.get('task', task_info)
        task_id = task_data.get('task_id', 'unknown')
        server_name = task_info.get('server_name', task_data.get('server_name', 'unknown'))
        required_servers = task_info.get('required_servers', [])
        
        # Determine which description to use
        description_type = "fuzzy" if self.use_fuzzy_descriptions else "detailed"
        ref_info = ""
        
        # Support both 'task_description' (new format) and 'description' (legacy)
        _detailed_desc = task_data.get('task_description', task_data.get('description', ''))
        if self.use_fuzzy_descriptions:
            task_description = task_data.get('fuzzy_description', _detailed_desc)
            if self.enable_concrete_description_ref:
                # Append concrete reference at end
                concrete_desc = _detailed_desc
                if concrete_desc and concrete_desc != task_description:
                    task_description += f"\n\nReference (for context): {concrete_desc}"
                    ref_info = " (with concrete ref)"
        else:
            task_description = _detailed_desc
        
        return {
            'task_id': task_id,
            'server_name': server_name,
            'task_description': task_description,
            'task_data': task_data,
            'description_type': description_type,
            'ref_info': ref_info,
            'required_servers': required_servers,
        }
    
    def _load_mcp_servers_catalog(self) -> Dict[str, Any]:
        """Load mcp_servers_info.json catalog (lazy, cached).

        Returns the ``servers`` sub-dict so callers can do
        ``catalog[server_name]["tools"]`` directly.
        """
        if self._mcp_servers_catalog is None:
            try:
                with open(self._mcp_servers_catalog_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._mcp_servers_catalog = data.get('servers', {})
                logger.info(
                    f"Loaded MCP servers catalog with {len(self._mcp_servers_catalog)} servers "
                    f"from {self._mcp_servers_catalog_path}"
                )
            except Exception as e:
                logger.warning(
                    f"Could not load MCP servers catalog from "
                    f"{self._mcp_servers_catalog_path}: {e}"
                )
                self._mcp_servers_catalog = {}
        return self._mcp_servers_catalog

    def _build_available_tools_from_catalog(self, required_servers: List[str]) -> Dict[str, Any]:
        """Build available_tools dict from the mcp_servers_info.json catalog.

        This is more reliable than using the live-preloaded tools dict from
        ``ADKTaskExecutor``, which can be empty when ``ResilientMcpToolset``
        silently returns ``[]`` on a transient connection error at setup time
        while the server becomes available later during actual execution.

        The prefixing convention mirrors ``mcp_tools.create_toolset_for_server``:
        ``<server_name_lowercased_and_underscored>_<tool_name>``.

        ``transfer_to_agent`` (ADK internal routing) is always included.

        Args:
            required_servers: List of server names the task requires.

        Returns:
            Dict mapping prefixed tool names to tool-metadata dicts compatible
            with the evaluator's ``available_tools`` format.
        """
        catalog = self._load_mcp_servers_catalog()
        available_tools: Dict[str, Any] = {}

        for server_name in required_servers:
            server_data = catalog.get(server_name)
            if server_data is None:
                logger.warning(
                    f"Server '{server_name}' not found in MCP servers catalog — "
                    "valid_tool_name_rate may be inaccurate for this task"
                )
                continue

            tools = server_data.get('tools', {})
            prefix = server_name.replace(' ', '_').replace('-', '_').lower() + '_'

            for tool_name, tool_info in tools.items():
                prefixed_name = prefix + tool_name
                available_tools[prefixed_name] = {
                    'name': prefixed_name,
                    'original_name': tool_name,
                    'server': server_name,
                    'description': tool_info.get('description', ''),
                    'input_schema': tool_info.get('input_schema', {}),
                    'agent': 'specialist',
                }

            logger.debug(
                f"Catalog: added {len(tools)} tools from '{server_name}' (prefix: '{prefix}')"
            )

        # Always include the ADK-internal routing tool
        available_tools['transfer_to_agent'] = {
            'name': 'transfer_to_agent',
            'original_name': 'transfer_to_agent',
            'server': 'adk_internal',
            'description': 'Switch control to other agent',
            'input_schema': {'agent_name': 'string'},
            'agent': 'coordinator',
        }

        return available_tools

    async def _prepare_server_configs(self, server_name: str, servers_info: Dict[str, Any], task_data: Dict[str, Any] = None) -> Dict[str, Any]:
        """Prepare server configurations for task execution."""
        # Handle multi-server tasks (server names separated by '+')
        if '+' in server_name:
            server_names = [s.strip() for s in server_name.split('+')]
            logger.info(f"Multi-server task detected: {server_names}")
        else:
            server_names = [server_name]
        
        # Map each server name to its configuration
        required_server_configs = []
        for srv_name in server_names:
            config = self.map_server_name_to_config(srv_name, servers_info)
            if config:
                required_server_configs.append(config)
            else:
                error_msg = f"No configuration found for required server: {srv_name}"
                logger.error(error_msg)
                return {'status': 'failed', 'error': error_msg}
        
        # Add resident servers (like Time MCP)
        resident_server_configs = self._prepare_distraction_servers([], task_data)  # Pass empty list to get residents only
        
        # Add distraction servers if enabled
        distraction_server_configs = []
        if self.enable_distraction_servers and self.distraction_count > 0:
            existing_server_names = [config['name'] for config in required_server_configs + resident_server_configs]
            distraction_server_configs = self._prepare_distraction_servers(existing_server_names, task_data)
        
        # Combine all server configurations
        all_server_configs = required_server_configs + resident_server_configs + distraction_server_configs
        
        logger.info(f"Total servers for task: {len(all_server_configs)} "
                   f"(Required: {len(required_server_configs)}, "
                   f"Resident: {len(resident_server_configs)}, "
                   f"Distraction: {len(distraction_server_configs)})")
        
        return {
            'status': 'completed',
            'all_server_configs': all_server_configs,
            'required_count': len(required_server_configs),
            'resident_count': len(resident_server_configs),
            'distraction_count': len(distraction_server_configs),
        }
    
    def _prepare_distraction_servers(self, existing_server_names: List[str], task_data: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """Prepare distraction servers for task execution.
        
        Note: commands_config must be loaded before calling this method.
        """
        # Ensure commands_config is loaded
        if self.commands_config is None:
            logger.warning("Commands config not loaded - skipping distraction servers")
            return []
        
        # Always add Time MCP as a resident server (unless it's already included)
        resident_configs = []
        if "Time MCP" not in existing_server_names and "Time MCP" in self.commands_config:
            temp_servers_info = {"Time MCP": self.commands_config["Time MCP"]}
            time_config = self.map_server_name_to_config("Time MCP", temp_servers_info)
            if time_config:
                resident_configs.append(time_config)
                logger.info("Added Time MCP as resident server")
        
        # If existing_server_names is empty, we're just getting resident servers
        if not existing_server_names:
            return resident_configs
        
        # Add resident servers to excluded list for distraction selection
        all_excluded_names = existing_server_names + [config['name'] for config in resident_configs]
        
        # Select distraction servers
        if self.enable_distraction_servers and self.distraction_count > 0:
            distraction_configs = self.select_random_distraction_servers(
                all_excluded_names, 
                self.commands_config, 
                self.distraction_count
            )
            return resident_configs + distraction_configs
        
        return resident_configs
    
    async def _evaluate_task_result(self, task_execution_info: Dict[str, Any], execution_result: Dict[str, Any], 
                                  model_name: str, server_name: str) -> Dict[str, Any]:
        """Evaluate task execution result using judge LLM."""
        task_data = task_execution_info['task_data']
        task_id = task_execution_info['task_id']
        
        # Prepare base result
        base_result = {
            'task_id': task_id,
            'server_name': server_name,
            'model_name': model_name,
            'status': execution_result['status'],
            'execution_time': execution_result['execution_time'],
            'task_run_metadata': execution_result.get('task_run_metadata', {}),
        }
        
        if execution_result['status'] == 'failed':
            base_result['error'] = execution_result.get('error', 'Unknown error')
            return base_result
        
        # Extract execution details
        result_data = execution_result['result']
        base_result.update({
            'solution': result_data.get('solution', ''),
            'total_rounds': result_data.get('total_rounds', 0),
            'execution_results': result_data.get('execution_results', []),
            'planning_json_compliance': result_data.get('planning_json_compliance', 1.0),
            'token_usage': {
                'prompt_tokens': result_data.get('token_usage', {}).get('prompt_tokens', result_data.get('total_prompt_tokens', 0)),
                'completion_tokens': result_data.get('token_usage', {}).get('completion_tokens', result_data.get('total_output_tokens', 0)),
                'total_tokens': result_data.get('token_usage', {}).get('total_tokens', result_data.get('total_tokens', 0)),
            },
            'task_run_metadata': {
                'adk_session_id': result_data.get('adk_session_id') or base_result.get('task_run_metadata', {}).get('adk_session_id'),
                'langfuse_trace_id': result_data.get('langfuse_trace_id') or base_result.get('task_run_metadata', {}).get('langfuse_trace_id'),
                'runner': 'adk',
            },
            'agent_execution_time': execution_result.get('agent_execution_time', 0),
        })
        
        # Always run evaluation to get metrics
        evaluator = TaskEvaluator(self._judge_provider, enable_judge_stability=self.enable_judge_stability)
        
        try:
            eval_start_time = time.time()
            logger.info(f"[ADK] Starting evaluation for task {task_id}")
            
            # Get task description (use concrete for evaluation if available)
            task_description = task_data.get('description', '')
            concrete_task_description = task_data.get('description', '')
            dependency_analysis = task_data.get('dependency_analysis', None)
            
            # Build available_tools from the static catalog (reliable even when
            # ResilientMcpToolset returned [] during setup due to a transient
            # connection error).  Fall back to the live-preloaded dict for any
            # server not present in the catalog.
            catalog_tools = self._build_available_tools_from_catalog(
                task_execution_info.get('required_servers', [])
            )
            live_tools = result_data.get('available_tools', {})
            # Merge: live tools first so catalog entries can override; catalog
            # wins because it was built from a successful offline discovery run.
            available_tools_for_eval = {**live_tools, **catalog_tools}

            # Run comprehensive evaluation
            evaluation = await evaluator.evaluate(
                task=task_description,
                execution_results=result_data.get('execution_results', []),
                final_solution=result_data.get('solution', ''),
                total_rounds=result_data.get('total_rounds', 0),
                available_tools=available_tools_for_eval,
                planning_json_compliance=result_data.get('planning_json_compliance', 1.0),
                concrete_task_description=concrete_task_description,
                dependency_analysis=dependency_analysis
            )
            
            eval_time = time.time() - eval_start_time
            logger.info(f"[ADK] Evaluation completed in {eval_time:.2f}s")
            
            if evaluation is None:
                logger.error(f"Evaluation returned None for task {task_id}")
                raise RuntimeError("Evaluation returned None")
            
            base_result['evaluation'] = evaluation
            base_result['evaluation_time'] = eval_time
                    
        except Exception as e:
            logger.error(f"Error during comprehensive evaluation: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            raise RuntimeError(f"Evaluation failed: {e}") from e
        
        return base_result
    
    # ------------------------------------------------------------------
    # Per-task result persistence helpers
    # ------------------------------------------------------------------

    def _compute_config_hash(self) -> str:
        """Return a short SHA-256 hex digest of the ADK config YAML file.

        The hash is used as the results directory name so that runs with
        different configs are stored in separate directories.
        """
        config_path = Path(__file__).parent / "adk_benchmark_config.yaml"
        if config_path.exists():
            raw = config_path.read_bytes()
        else:
            raw = b""
        return hashlib.sha256(raw).hexdigest()[:12]

    def _get_results_dir(self) -> Path:
        """Return (and create) the per-config results directory.

        Directory layout:  results/<config_hash>/
        """
        if self._results_dir is None:
            config_hash = self._compute_config_hash()
            # Place results/ next to the workspace root (two levels up from
            # this file which lives inside google_adk_agents/).
            workspace_root = Path(__file__).parent.parent
            self._results_dir = workspace_root / "results" / config_hash
            self._results_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[ADK] Results directory: {self._results_dir}")
        return self._results_dir

    @staticmethod
    def _task_result_filename(model_name: str, task_id: str) -> str:
        """Build a safe filename for a single task result."""
        safe_model = model_name.replace("/", "_").replace("\\", "_")
        safe_task = task_id.replace("/", "_").replace("\\", "_")
        return f"{safe_model}__{safe_task}.json"

    def _task_result_path(self, model_name: str, task_id: str) -> Path:
        """Full path for a single task result file."""
        return self._get_results_dir() / self._task_result_filename(model_name, task_id)

    def _task_result_exists(self, model_name: str, task_id: str) -> bool:
        """Return True if the result file for this task+model already exists."""
        return self._task_result_path(model_name, task_id).exists()

    def _save_task_result(self, model_name: str, task_id: str, result: Dict[str, Any]) -> None:
        """Persist a single task result to its own JSON file."""
        path = self._task_result_path(model_name, task_id)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2, default=str)
            logger.debug(f"[ADK] Task result saved: {path.name}")
        except Exception as exc:
            logger.warning(f"[ADK] Failed to save task result {path.name}: {exc}")

    def _load_task_result(self, model_name: str, task_id: str) -> Optional[Dict[str, Any]]:
        """Load an existing task result from disk (returns None on error)."""
        path = self._task_result_path(model_name, task_id)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            logger.warning(f"[ADK] Could not load cached result {path.name}: {exc}")
            return None

    # ------------------------------------------------------------------

    def _build_run_config_snapshot(self) -> Dict[str, Any]:
        """Build a snapshot of all effective configuration for this run.

        Stored under ``run_metadata.config_snapshot`` in every result file so
        results are fully self-describing and reproducible.
        """
        return {
            # --- ADK model settings ---
            'judge_model': adk_config_loader.get_judge_model(),
            'default_agent_model': adk_config_loader.get_default_model(),
            'compressor_model': (
                adk_config_loader.get_compression_compressor_model() or '<inherits agent model>'
            ),
            # --- ContentCompressor thresholds ---
            'compression_token_threshold': adk_config_loader.get_compression_token_threshold(),
            'compression_tool_result_threshold': adk_config_loader.get_compression_tool_result_threshold(),
            'compression_hard_limit_threshold': adk_config_loader.get_compression_hard_limit_threshold(),
            # --- Execution settings ---
            'task_timeout': adk_config_loader.get_task_timeout(),
            'max_retries': adk_config_loader.get_max_retries(),
            'retry_delay': adk_config_loader.get_retry_delay(),
            # --- Benchmark feature flags ---
            'use_fuzzy_descriptions': self.use_fuzzy_descriptions,
            'enable_concrete_description_ref': self.enable_concrete_description_ref,
            'enable_dependency_analysis_ref': adk_config_loader.is_dependency_analysis_ref_enabled(),
            'enable_judge_stability': self.enable_judge_stability,
            'judge_stability_runs': adk_config_loader.get_judge_stability_runs(),
            'filter_problematic_tools': self.filter_problematic_tools,
            'concurrent_summarization': self.concurrent_summarization,
            'enable_distraction_servers': self.enable_distraction_servers,
            'distraction_count': self.distraction_count,
            # --- MCP ---
            'resident_servers': adk_config_loader.get_resident_servers(),
            'servers_catalog_path': adk_config_loader.get_servers_catalog_path(),
        }

    async def _initialize_benchmark(self, selected_models: List[str] = None, task_limit: int = None) -> Dict[str, Any]:
        """Initialize benchmark by loading tasks and server configs."""
        try:
            # Load tasks and server configurations
            tasks = await self.load_tasks()
            servers_info = await self.load_server_configs()
            
            # Load commands config if not already loaded (for distraction servers)
            if self.commands_config is None:
                self.commands_config = await self.load_commands_config()
            
            # Apply task limit if specified
            if task_limit:
                tasks = tasks[:task_limit]
                logger.info(f"Limited to {len(tasks)} tasks")
            
            # Filter models if specified
            if selected_models:
                available_models = {k: v for k, v in self.model_configs.items() if k in selected_models}
                if not available_models:
                    logger.error(f"None of the selected models are available: {selected_models}")
                    return {'status': 'failed'}
            else:
                available_models = self.model_configs
            
            return {
                'status': 'completed',
                'tasks': tasks,
                'servers_info': servers_info,
                'available_models': available_models,
            }
            
        except Exception as e:
            logger.error(f"Error initializing benchmark: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return {'status': 'failed'}
    
    async def _run_single_file_benchmark_core(self, selected_models: List[str] = None, 
                                             task_limit: int = None) -> Dict[str, Any]:
        """Run benchmark across multiple models for current task file"""
        logger.info("[ADK] Starting multi-agent benchmark execution")
        
        # Step 1: Initialize benchmark
        init_result = await self._initialize_benchmark(selected_models, task_limit)
        if init_result['status'] == 'failed':
            return {}
        
        tasks = init_result['tasks']
        servers_info = init_result['servers_info']
        available_models = init_result['available_models']
        all_model_task_results: Dict[str, List[Dict[str, Any]]] = {}
        all_model_metrics: Dict[str, Dict[str, Any]] = {}

        # Ensure per-task results directory is ready
        results_dir = self._get_results_dir()
        logger.info(f"[ADK] Per-task results will be stored in: {results_dir}")

        # Calculate total tasks across all models for overall progress
        total_tasks_all_models = len(available_models) * len(tasks)
        completed_tasks_all_models = 0
        
        # Step 2: Test each model
        for model_idx, (model_name, model_config) in enumerate(available_models.items(), 1):
            logger.info(f"\n{'='*60}")
            logger.info(f"[ADK] Testing model: {model_name}")
            logger.info(f"{'='*60}")
            
            try:
                model_results = []
                completed_tasks = 0
                failed_tasks = 0
                skipped_tasks = 0

                for i, task_info in enumerate(tasks, 1):
                    task_id = task_info.get('task', task_info).get('task_id', f'task_{i}')
                    logger.info(f"\n[ADK] Task {i}/{len(tasks)} (Overall: {completed_tasks_all_models + 1}/{total_tasks_all_models})")
                    logger.info(f"[ADK] Task ID: {task_id}")

                    # --- Skip if result already exists on disk ---
                    if self._task_result_exists(model_name, task_id):
                        cached = self._load_task_result(model_name, task_id)
                        if cached is not None:
                            logger.info(f"[ADK] Skipping {task_id} — result already exists in {results_dir.name}")
                            model_results.append(cached)
                            completed_tasks_all_models += 1
                            skipped_tasks += 1
                            if cached.get('status') == 'completed':
                                completed_tasks += 1
                            else:
                                failed_tasks += 1
                            continue

                    # Execute single task (no LLM provider needed for ADK)
                    result = await self.execute_single_task_with_model(
                        task_info, 
                        servers_info, 
                        model_name
                    )

                    # Persist result immediately (each task is its own checkpoint)
                    self._save_task_result(model_name, task_id, result)

                    model_results.append(result)
                    completed_tasks_all_models += 1
                    
                    if result['status'] == 'completed':
                        completed_tasks += 1
                    else:
                        failed_tasks += 1
                    
                    # Log progress
                    success_rate = (completed_tasks / (completed_tasks + failed_tasks)) * 100 if (completed_tasks + failed_tasks) > 0 else 0
                    logger.info(f"[ADK] Progress: {completed_tasks}/{len(tasks)} successful, {skipped_tasks} skipped ({success_rate:.1f}% success rate)")
                
                # Aggregate results for this model
                logger.info(f"\n[ADK] Aggregating results for model {model_name}...")
                aggregated_results = self.aggregator.aggregate_model_results(model_results)
                all_model_task_results[model_name] = model_results
                all_model_metrics[model_name] = aggregated_results
                
                # Update cumulative metrics (store current model's results)
                self.last_cumulative_metrics = aggregated_results.copy()

            except Exception as e:
                logger.error(f"[ADK] Error testing model {model_name}: {e}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
        
        # Step 3: Return final cumulative metrics
        logger.info(f"[ADK] MCP-Bench benchmark completed: {len(available_models)} models tested")
        result_payload: Dict[str, Any] = {
            'task_file': self.tasks_file,
            'run_metadata': {
                'generated_at': datetime.utcnow().isoformat() + 'Z',
                'runner': 'adk',
                'models_tested': list(available_models.keys()),
                'tasks_per_model': len(tasks),
                'config_snapshot': self._build_run_config_snapshot(),
            },
            'final_metrics': all_model_metrics,
            'task_run_results': all_model_task_results,
        }

        if len(all_model_metrics) == 1:
            single_model_metrics = next(iter(all_model_metrics.values()))
            result_payload.update(single_model_metrics)

        return result_payload
    
    async def run_benchmark(self, selected_models: List[str] = None, task_limit: int = None) -> Dict[str, Any]:
        """Run ADK benchmark - either single file or all files based on configuration.
        
        Args:
            selected_models: List of model names to test (None = all)
            task_limit: Maximum number of tasks to run (None = all)
            
        Returns:
            Dictionary containing aggregated benchmark results
        """
        # Determine which task files to run
        if hasattr(self, '_force_single_file') and self._force_single_file:
            # This is called from run_single_file_benchmark, use current task file
            return await self._run_single_file_benchmark_core(selected_models, task_limit)
        
        # Check if user specified specific task file(s)
        all_task_files = adk_config_loader.get_all_task_files()
        
        # Check if user specified comma-separated task files
        if self.tasks_file and ',' in self.tasks_file:
            # User specified multiple task files via comma separation
            user_task_files = [f.strip() for f in self.tasks_file.split(',')]
            logger.info(f"[ADK] Running benchmark for user-specified task files: {user_task_files}")
            all_task_files = user_task_files
        elif self.tasks_file:
            # User specified a custom single task file, run only that file
            logger.info(f"[ADK] Running benchmark for specified task file: {self.tasks_file}")
            return await self._run_single_file_benchmark_core(selected_models, task_limit)
        
        # Default behavior: run all task files
        logger.info("[ADK] Running comprehensive benchmark across all task files")
        
        # Store results for all files
        all_files_metrics = {}
        
        for task_file in all_task_files:
            logger.info(f"\n{'='*100}")
            logger.info(f"[ADK] Starting benchmark for: {os.path.basename(task_file)}")
            logger.info(f"{'='*100}")
            
            try:
                # Temporarily set the task file
                original_task_file = self.tasks_file
                self.tasks_file = task_file
                self._force_single_file = True
                
                # Run benchmark for this file
                file_metrics = await self._run_single_file_benchmark_core(selected_models, task_limit)
                
                # Store the metrics for this file
                all_files_metrics[task_file] = file_metrics
                
            except Exception as e:
                logger.error(f"[ADK] Error running benchmark for {task_file}: {e}")
                logger.error(f"Full traceback: {traceback.format_exc()}")
                # Save last cumulative metrics even on failure
                all_files_metrics[task_file] = {
                    'task_file': task_file,
                    'run_metadata': {
                        'generated_at': datetime.utcnow().isoformat() + 'Z',
                        'runner': 'adk',
                        'error': str(e),
                        'config_snapshot': self._build_run_config_snapshot(),
                    },
                    'final_metrics': self.last_cumulative_metrics.copy(),
                    'task_run_results': {}
                }
                
            finally:
                # Restore original settings
                self.tasks_file = original_task_file
                if hasattr(self, '_force_single_file'):
                    delattr(self, '_force_single_file')
        
        # Return metrics for all files
        return all_files_metrics
    
    async def run_single_file_benchmark(self, task_file: str, selected_models: List[str], task_limit: int = None) -> Dict[str, Any]:
        """Run benchmark for a single task file and return final metrics.
        
        Args:
            task_file: Path to the specific task file to run
            selected_models: List of model names to test
            task_limit: Maximum number of tasks to run (None = all)
            
        Returns:
            Dictionary containing aggregated benchmark results
        """
        logger.info(f"\n{'='*100}")
        logger.info(f"[ADK] Running single file benchmark for: {os.path.basename(task_file)}")
        logger.info(f"{'='*100}")
        
        # Store original task file
        original_task_file = self.tasks_file
        
        try:
            # Temporarily set the task file
            self.tasks_file = task_file
            self._force_single_file = True
            
            # Run benchmark for this file
            return await self._run_single_file_benchmark_core(selected_models, task_limit)
            
        finally:
            # Restore original settings
            self.tasks_file = original_task_file
            if hasattr(self, '_force_single_file'):
                delattr(self, '_force_single_file')
    
    async def save_results(self, results: Dict[str, Any], output_file: str = None) -> str:
        """Save benchmark results to JSON file.
        
        Args:
            results: Dictionary containing benchmark results
            output_file: Optional output filename (auto-generated if None)
            
        Returns:
            Path to the saved results file
        """
        if output_file is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = f"benchmark_results_adk_{timestamp}.json"
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        
        logger.info(f"[ADK] Results saved to {output_file}")
        return output_file
    
    async def _save_incremental_results(
        self, 
        output_file: str, 
        current_model: str, 
        current_model_results: List[Dict[str, Any]],
        tasks: List[Dict[str, Any]],
        available_models: Dict[str, Any],
        all_model_task_results: Dict[str, List[Dict[str, Any]]],
        all_model_metrics: Dict[str, Dict[str, Any]]
    ) -> None:
        """Save incremental results after each task to prevent data loss.
        
        Args:
            output_file: Path to incremental results file
            current_model: Name of the model being tested
            current_model_results: Results for current model so far
            tasks: All tasks being tested
            available_models: All models being tested
            all_model_task_results: All task results across all models
            all_model_metrics: Aggregated metrics for completed models
        """
        try:
            # Aggregate current model's results so far
            current_aggregated = self.aggregator.aggregate_model_results(current_model_results)
            
            # Build incremental results payload
            incremental_data = {
                'task_file': self.tasks_file,
                'run_metadata': {
                    'generated_at': datetime.utcnow().isoformat() + 'Z',
                    'runner': 'adk',
                    'status': 'in_progress',
                    'current_model': current_model,
                    'models_tested': list(available_models.keys()),
                    'tasks_per_model': len(tasks),
                    'completed_tasks_current_model': len(current_model_results),
                    'total_tasks_current_model': len(tasks),
                    'config_snapshot': self._build_run_config_snapshot(),
                },
                'current_model_progress': {
                    current_model: current_aggregated
                },
                'completed_models_metrics': all_model_metrics,
                'current_model_task_results': {
                    current_model: current_model_results
                },
                'all_completed_task_results': all_model_task_results,
            }
            
            # Write to file
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(incremental_data, f, indent=2, default=str)
            
            logger.debug(f"[ADK] Incremental results saved to {output_file}")
            
        except Exception as e:
            logger.warning(f"[ADK] Failed to save incremental results: {e}")
            # Don't fail the benchmark if incremental save fails
