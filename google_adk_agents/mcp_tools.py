"""
MCP Tools Wrapper for Google ADK

This module provides utilities to create McpToolset instances for Google ADK agents.
It uses Google ADK's native MCP integration via google.adk.tools.mcp_tool.
Supports both STDIO (local process) and SSE (HTTP/remote) transports.
"""

import logging
from typing import Dict, List, Any, Optional

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams, SseConnectionParams
from mcp import StdioServerParameters

logger = logging.getLogger(__name__)


def create_toolset_for_server(
    server_config: Dict[str, Any],
    tool_filter: Optional[List[str]] = None
) -> McpToolset:
    """Create a McpToolset for a single MCP server.
    
    Supports both STDIO (local process) and HTTP/SSE (remote) transports.
    
    Args:
        server_config: Server configuration dict with:
            - 'name': Server name
            - 'command': Command to run (for stdio) or ignored (for http)
            - 'env': Environment variables
            - 'cwd': Working directory (for stdio)
            - 'transport': 'stdio' (default) or 'http'
            - 'port': Port number (for http)
            - 'endpoint': Endpoint path (for http, default '/mcp')
        tool_filter: Optional list of specific tool names to include
        
    Returns:
        McpToolset instance configured for the server
    """
    server_name = server_config["name"]
    transport_type = server_config.get("transport", "stdio")
    
    if transport_type == "http":
        # Use SSE connection for HTTP-based MCP servers
        port = server_config.get("port", 3001)
        endpoint = server_config.get("endpoint", "/mcp")
        url = f"http://localhost:{port}{endpoint}"
        
        # Build headers from environment variables if specified
        headers = {}
        env_vars = server_config.get("env", [])
        if env_vars:
            import os
            for env_var in env_vars:
                value = os.getenv(env_var)
                if value:
                    headers[env_var] = value
        
        connection_params = SseConnectionParams(
            url=url,
            headers=headers if headers else None,
            timeout=30.0,
        )
        
        logger.info(f"Created SSE connection params for '{server_name}' at {url}")
    else:
        # Use STDIO connection for local process-based MCP servers
        command = server_config["command"]
        
        # Extract command and args
        if isinstance(command, list):
            cmd = command[0]
            args = command[1:] if len(command) > 1 else []
        else:
            cmd = command
            args = []
        
        # Create server parameters
        server_params = StdioServerParameters(
            command=cmd,
            args=args,
            env=server_config.get("env"),
            cwd=server_config.get("cwd")
        )
        
        connection_params = StdioConnectionParams(
            server_params=server_params,
            timeout=30.0
        )
        
        logger.info(f"Created STDIO connection params for '{server_name}'")
    
    # Create toolset with appropriate connection params
    toolset = McpToolset(
        connection_params=connection_params,
        tool_filter=tool_filter,
    )
    
    logger.info(f"Created McpToolset for server '{server_name}' using {transport_type} transport")
    return toolset


def create_toolsets_for_servers(
    server_configs: List[Dict[str, Any]],
    server_names: Optional[List[str]] = None,
    tool_filter: Optional[Dict[str, List[str]]] = None
) -> List[McpToolset]:
    """Create McpToolset instances for multiple MCP servers.
    
    Args:
        server_configs: List of server configuration dicts
        server_names: Optional list of server names to include (default: all)
        tool_filter: Optional dict mapping server names to lists of tool names
        
    Returns:
        List of McpToolset instances
    """
    toolsets = []
    
    # Filter configs if server_names specified
    if server_names:
        configs_to_use = [
            cfg for cfg in server_configs 
            if cfg["name"] in server_names
        ]
    else:
        configs_to_use = server_configs
    
    for server_config in configs_to_use:
        server_name = server_config["name"]
        
        # Get tool filter for this server if specified
        server_tool_filter = None
        if tool_filter and server_name in tool_filter:
            server_tool_filter = tool_filter[server_name]
        
        try:
            toolset = create_toolset_for_server(server_config, server_tool_filter)
            toolsets.append(toolset)
        except Exception as e:
            logger.error(f"Failed to create toolset for server '{server_name}': {e}")
    
    logger.info(f"Created {len(toolsets)} toolsets from {len(configs_to_use)} servers")
    return toolsets


def get_server_config(server_configs: List[Dict[str, Any]], server_name: str) -> Optional[Dict[str, Any]]:
    """Extract server configuration from server_configs list."""
    for config in server_configs:
        if config["name"] == server_name:
            return config
    return None


def create_mcp_tools_for_agent(
    server_configs: List[Dict[str, Any]],
    server_names: List[str],
    tool_filter: Optional[Dict[str, List[str]]] = None
) -> List[McpToolset]:
    """Create McpToolset instances for an agent's assigned servers.
    
    This function creates ADK-native McpToolset instances from server configurations.
    
    Args:
        server_configs: List of server configuration dictionaries
        server_names: List of MCP server names assigned to the agent
        tool_filter: Optional dict mapping server names to lists of tool names
        
    Returns:
        List of McpToolset instances
        
    Example:
        >>> toolsets = create_mcp_tools_for_agent(
        ...     server_configs, 
        ...     ["weather_mcp", "time-mcp"],
        ...     tool_filter={"time-mcp": ["get_current_time"]}
        ... )
        >>> agent = Agent(name="MyAgent", tools=toolsets)
    """
    # Extract relevant server configs
    relevant_configs = []
    for server_name in server_names:
        config = get_server_config(server_configs, server_name)
        if config:
            relevant_configs.append(config)
        else:
            logger.warning(f"Server '{server_name}' not found in server_configs")
    
    return create_toolsets_for_servers(relevant_configs, tool_filter=tool_filter)