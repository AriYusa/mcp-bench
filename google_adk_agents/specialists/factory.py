"""
Specialist Agent Factory

Factory for creating and managing specialist agents based on available MCP servers.
"""

import logging
from typing import Any, Dict, List, Optional

from google.adk import Agent

from ..config import Config
from ..agent_mcp_mapping import (
    get_all_agent_names,
    get_agents_for_servers,
    filter_agent_servers,
)
from .base import create_specialist_agent

logger = logging.getLogger(__name__)


class SpecialistAgentFactory:
    """Factory for creating specialist agents based on available MCP servers.
    
    This factory creates only the specialist agents that have relevant MCP servers
    available, optimizing the agent hierarchy for the current task context.
    """
    
    def __init__(
        self,
        server_configs: List[Dict[str, Any]],
        config: Optional[Config] = None,
        model_override: Optional[str] = None,
    ):
        """Initialize the specialist agent factory.
        
        Args:
            server_configs: List of server configuration dictionaries
            config: Optional Config instance for model configuration
            model_override: Optional model name to override default
        """
        self.server_configs = server_configs
        self.config = config or Config()
        self.model_override = model_override
        self._agents: Dict[str, Agent] = {}
        self._available_servers: List[str] = []
    
    def _discover_available_servers(self) -> List[str]:
        """Discover available servers from the server configs.
        
        Returns:
            List of available server names
        """
        return [config["name"] for config in self.server_configs]
    
    def create_all_relevant_agents(
        self,
        server_names: Optional[List[str]] = None
    ) -> Dict[str, Agent]:
        """Create all specialist agents that have relevant MCP servers.
        
        Args:
            server_names: Optional list of server names to consider.
                         If None, discovers from server_manager.
                         
        Returns:
            Dictionary mapping agent keys to Agent instances
        """
        if server_names is None:
            server_names = self._discover_available_servers()
        
        self._available_servers = server_names
        logger.info(f"Creating specialist agents for {len(server_names)} available servers")
        
        # Determine which agents are needed
        needed_agents = get_agents_for_servers(server_names)
        logger.info(f"Agents needed for available servers: {needed_agents}")
        
        # Create each needed agent
        created_agents = {}
        for agent_key in needed_agents:
            agent = create_specialist_agent(
                agent_key=agent_key,
                server_configs=self.server_configs,
                available_servers=server_names,
                config=self.config,
                model_override=self.model_override,
            )
            
            if agent is not None:
                created_agents[agent_key] = agent
                self._agents[agent_key] = agent
        
        logger.info(f"Created {len(created_agents)} specialist agents: {list(created_agents.keys())}")
        return created_agents
    
    def create_specific_agents(
        self,
        agent_keys: List[str],
        server_names: Optional[List[str]] = None
    ) -> Dict[str, Agent]:
        """Create specific specialist agents by their keys.
        
        Args:
            agent_keys: List of agent keys to create
            server_names: Optional list of available server names
            
        Returns:
            Dictionary mapping agent keys to Agent instances
        """
        if server_names is None:
            server_names = self._discover_available_servers()
        
        self._available_servers = server_names
        
        created_agents = {}
        for agent_key in agent_keys:
            if agent_key not in get_all_agent_names():
                logger.warning(f"Unknown agent key: {agent_key}")
                continue
            
            agent = create_specialist_agent(
                agent_key=agent_key,
                server_configs=self.server_configs,
                available_servers=server_names,
                config=self.config,
                model_override=self.model_override,
            )
            
            if agent is not None:
                created_agents[agent_key] = agent
                self._agents[agent_key] = agent
        
        return created_agents
    
    def get_agent(self, agent_key: str) -> Optional[Agent]:
        """Get a previously created agent by key.
        
        Args:
            agent_key: Key identifying the agent
            
        Returns:
            Agent instance or None if not created
        """
        return self._agents.get(agent_key)
    
    def get_all_agents(self) -> Dict[str, Agent]:
        """Get all created agents.
        
        Returns:
            Dictionary mapping agent keys to Agent instances
        """
        return self._agents.copy()
    
    def get_agents_list(self) -> List[Agent]:
        """Get list of all created agents.
        
        Returns:
            List of Agent instances
        """
        return list(self._agents.values())
    
    def get_available_servers(self) -> List[str]:
        """Get the list of available servers.
        
        Returns:
            List of server names
        """
        return self._available_servers.copy()
    
    def get_agent_server_summary(self) -> Dict[str, List[str]]:
        """Get summary of which servers each agent uses.
        
        Returns:
            Dictionary mapping agent keys to their assigned server names
        """
        summary = {}
        for agent_key in self._agents.keys():
            assigned_servers = filter_agent_servers(agent_key, self._available_servers)
            summary[agent_key] = assigned_servers
        return summary
