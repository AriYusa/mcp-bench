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
import json
import logging
import os
import random
import sys
import time
import traceback
from datetime import datetime
from typing import Dict, List, Any, Optional
from langfuse import get_client, observe

# Add parent directory to Python path to resolve imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google_adk_agents.adk_executor import ADKTaskExecutor
from benchmark.evaluator import TaskEvaluator
from benchmark.results_aggregator import ResultsAggregator
from benchmark.results_formatter import ResultsFormatter
from utils.local_server_config import LocalServerConfigLoader
import config.config_loader as config_loader
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
        formatter: Results formatter instance
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
        formatter: Optional[ResultsFormatter] = None,
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
            formatter: Injected results formatter
            judge_provider: Injected judge LLM provider
        """
        # Use config file defaults if not explicitly provided
        self.tasks_file = tasks_file or config_loader.get_tasks_file()
        
        # Use injected dependencies or create defaults
        self.local_config_loader = local_config_loader or LocalServerConfigLoader()
        self._judge_provider = judge_provider  # Store injected judge provider
        
        # Use config file defaults for feature flags
        self.enable_distraction_servers = enable_distraction_servers if enable_distraction_servers is not None else True
        self.distraction_count = distraction_count if distraction_count is not None else config_loader.get_distraction_servers_count()
        self.enable_judge_stability = enable_judge_stability if enable_judge_stability is not None else config_loader.is_judge_stability_enabled()
        self.filter_problematic_tools = filter_problematic_tools if filter_problematic_tools is not None else config_loader.is_problematic_tools_filter_enabled()
        self.concurrent_summarization = concurrent_summarization if concurrent_summarization is not None else config_loader.is_concurrent_summarization_enabled()
        self.use_fuzzy_descriptions = use_fuzzy_descriptions if use_fuzzy_descriptions is not None else config_loader.use_fuzzy_descriptions()
        self.enable_concrete_description_ref = config_loader.is_concrete_description_ref_enabled()
        self.commands_config = None
        
        # Track current cumulative metrics for error handling
        self.last_cumulative_metrics = {}
        
        # Initialize results handling components (use injected or create defaults)
        self.aggregator = aggregator or ResultsAggregator()
        self.formatter = formatter or ResultsFormatter()
        
        # ADK-specific: List of available model names (ADK handles model configs internally)
        self.model_configs = {
            "gemini-2.0-flash-exp": {"display_name": "Gemini 2.0 Flash Experimental"},
            "gemini-1.5-pro": {"display_name": "Gemini 1.5 Pro"},
            "gemini-1.5-flash": {"display_name": "Gemini 1.5 Flash"},
            "anthropic/claude-sonnet-4-5-20250929": {"display_name": "Claude Sonnet 4.5"},
        }
        
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
                    # Handle both 'task' (single) and 'tasks' (array) formats
                    if 'task' in server_group:
                        # Single task format
                        flattened_tasks.append({
                            'server_name': server_name,
                            'task': server_group['task']
                        })
                    elif 'tasks' in server_group:
                        # Multiple tasks format
                        for task in server_group.get('tasks', []):
                            flattened_tasks.append({
                                'server_name': server_name,
                                'task': task
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
                config['port'] = server_config.get('port', config_loader.get_default_port())
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
            count = config_loader.get_distraction_servers_count()
        
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
            max_retries = config_loader.get_max_retries()
        if timeout_seconds is None:
            timeout_seconds = config_loader.get_task_timeout()
        
        # Initialize judge provider once for this task execution
        if not hasattr(self, '_judge_provider') or self._judge_provider is None:
            self._judge_provider = LLMProvider("anthropic/claude-sonnet-4-5-20250929")
        
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
            
            try:
                logger.info(f"[ADK] Attempt {attempt + 1}/{max_retries} for task {task_id}")
                
                # Create ADK executor with server configs
                # ADK handles connection lifecycle internally - no ConnectionManager needed
                executor = ADKTaskExecutor(
                    server_configs=all_server_configs,
                    model_override=model_name,  # Pass model name directly
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
                        await asyncio.sleep(config_loader.get_retry_delay())
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
                    await asyncio.sleep(config_loader.get_retry_delay())
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
        
        # Determine which description to use
        description_type = "fuzzy" if self.use_fuzzy_descriptions else "detailed"
        ref_info = ""
        
        if self.use_fuzzy_descriptions:
            task_description = task_data.get('fuzzy_description', task_data.get('description', ''))
            if self.enable_concrete_description_ref:
                # Append concrete reference at end
                concrete_desc = task_data.get('description', '')
                if concrete_desc and concrete_desc != task_description:
                    task_description += f"\n\nReference (for context): {concrete_desc}"
                    ref_info = " (with concrete ref)"
        else:
            task_description = task_data.get('description', '')
        
        return {
            'task_id': task_id,
            'server_name': server_name,
            'task_description': task_description,
            'task_data': task_data,
            'description_type': description_type,
            'ref_info': ref_info,
        }
    
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
            'accumulated_information': result_data.get('accumulated_information', ''),
            'token_usage': {
                'prompt_tokens': result_data.get('token_usage', {}).get('prompt_tokens', result_data.get('total_prompt_tokens', 0)),
                'completion_tokens': result_data.get('token_usage', {}).get('completion_tokens', result_data.get('total_output_tokens', 0)),
                'total_tokens': result_data.get('token_usage', {}).get('total_tokens', result_data.get('total_tokens', 0)),
            },
            'task_run_metadata': {
                'adk_session_id': result_data.get('adk_session_id') or base_result.get('task_run_metadata', {}).get('adk_session_id'),
                'sdk_session_id': result_data.get('sdk_session_id') or result_data.get('adk_session_id') or base_result.get('task_run_metadata', {}).get('sdk_session_id') or base_result.get('task_run_metadata', {}).get('adk_session_id'),
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
            
            # Run comprehensive evaluation
            evaluation = await evaluator.evaluate(
                task=task_description,
                execution_results=result_data.get('execution_results', []),
                final_solution=result_data.get('solution', ''),
                total_rounds=result_data.get('total_rounds', 0),
                available_tools=result_data.get('available_tools', {}),
                planning_json_compliance=result_data.get('planning_json_compliance', 1.0),
                accumulated_information=result_data.get('accumulated_information', ''),
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

                for i, task_info in enumerate(tasks, 1):
                    task_id = task_info.get('task', task_info).get('task_id', f'task_{i}')
                    logger.info(f"\n[ADK] Task {i}/{len(tasks)} (Overall: {completed_tasks_all_models + 1}/{total_tasks_all_models})")
                    logger.info(f"[ADK] Task ID: {task_id}")
                    
                    # Execute single task (no LLM provider needed for ADK)
                    result = await self.execute_single_task_with_model(
                        task_info, 
                        servers_info, 
                        model_name
                    )
                    
                    model_results.append(result)
                    completed_tasks_all_models += 1
                    
                    if result['status'] == 'completed':
                        completed_tasks += 1
                    else:
                        failed_tasks += 1
                    
                    # Log progress
                    success_rate = (completed_tasks / (completed_tasks + failed_tasks)) * 100 if (completed_tasks + failed_tasks) > 0 else 0
                    logger.info(f"[ADK] Progress: {completed_tasks}/{len(tasks)} successful ({success_rate:.1f}% success rate)")
                
                # Aggregate results for this model
                logger.info(f"\n[ADK] Aggregating results for model {model_name}...")
                aggregated_results = self.aggregator.aggregate_model_results(model_results)
                all_model_task_results[model_name] = model_results
                all_model_metrics[model_name] = aggregated_results
                
                # Update cumulative metrics (store current model's results)
                self.last_cumulative_metrics = aggregated_results.copy()
                
                # Display current results for this model
                self.formatter.format_current_metrics(
                    model_name, 
                    completed_tasks, 
                    len(tasks), 
                    self.last_cumulative_metrics, 
                    self.tasks_file
                )

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
        all_task_files = config_loader.get_all_task_files()
        
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
                        'error': str(e)
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
