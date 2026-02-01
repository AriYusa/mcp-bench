"""
Base Specialist Agent Module

Provides the base functionality for creating specialist agents with MCP tool access.
"""

import logging
from typing import Dict, List, Any, Optional

from google.adk import Agent

from ..config import Config
from ..agent_mcp_mapping import get_agent_config, filter_agent_servers
from ..mcp_tools import create_mcp_tools_for_agent

logger = logging.getLogger(__name__)


def create_specialist_agent(
    agent_key: str,
    server_configs: List[Dict[str, Any]],
    available_servers: List[str],
    config: Optional[Config] = None,
    model_override: Optional[str] = None,
) -> Optional[Agent]:
    """Create a specialist agent with its assigned MCP tools.
    
    This function creates an ADK Agent instance for a specialist domain,
    configuring it with the appropriate MCP tools from the available servers.
    
    Args:
        agent_key: Key identifying the agent type (e.g., "researcher", "health_bio_specialist")
        server_configs: List of server configuration dictionaries
        available_servers: List of currently available MCP server names
        config: Optional Config instance for model configuration
        model_override: Optional model name to override default
        
    Returns:
        Configured ADK Agent instance, or None if no tools are available
    """
    config = config or Config()
    
    # Get agent configuration
    try:
        agent_config = get_agent_config(agent_key)
    except ValueError as e:
        logger.error(f"Unknown agent key: {agent_key}")
        return None
    
    # Filter to only available servers
    agent_servers = filter_agent_servers(agent_key, available_servers)
    
    if not agent_servers:
        logger.warning(f"No available servers for agent '{agent_key}'. Skipping creation.")
        return None
    
    # Create McpToolset instances for the agent's servers
    toolsets = create_mcp_tools_for_agent(server_configs, agent_servers)
    
    if not toolsets:
        logger.warning(f"No toolsets available for agent '{agent_key}' from servers {agent_servers}. Skipping creation.")
        return None
    
    # Create the ADK agent
    agent = Agent(
        name=agent_config.name,
        model=config.get_model_for_agent(model_override),
        description=agent_config.description,
        instruction=agent_config.instruction,
        tools=toolsets,  # Pass McpToolset instances
        # Allow transfer to parent (coordinator) and peers (other specialists)
        disallow_transfer_to_parent=False,
        disallow_transfer_to_peers=True,
    )
    
    logger.info(
        f"Created specialist agent '{agent_config.name}' with {len(toolsets)} toolsets "
        f"from {len(agent_servers)} servers: {agent_servers}"
    )
    
    return agent
