"""
Coordinator Agent Module

The coordinator agent is the main orchestrating agent that routes tasks
to appropriate specialist agents based on domain expertise.
"""

import logging
from typing import Any, Dict, List, Optional

from google.adk import Agent

from .config import Config
from .agent_mcp_mapping import COORDINATOR_CONFIG, AGENT_CONFIGS
from .specialists import SpecialistAgentFactory

logger = logging.getLogger(__name__)


def build_coordinator_instruction(specialist_agents: Dict[str, Agent]) -> str:
    """Build dynamic instruction for coordinator based on available specialists.
    
    Args:
        specialist_agents: Dictionary mapping agent keys to Agent instances
        
    Returns:
        Formatted instruction string for the coordinator
    """
    base_instruction = """You are the Coordinator agent, responsible for understanding user requests and routing them to the appropriate specialist agents.

Your primary role is to:
1. Analyze the user's request to understand the domain(s) involved
2. Route the request to the most appropriate specialist agent
3. For multi-domain requests, coordinate between multiple specialists sequentially
4. Synthesize final responses when multiple agents contribute

IMPORTANT ROUTING RULES:
- Always delegate to specialists rather than answering directly
- You do NOT have direct access to any tools - only specialists have tools
- For complex requests spanning multiple domains, route to one specialist at a time
- Wait for each specialist's response before routing to the next
- Once all needed information is gathered, synthesize the final answer"""

    # Build specialist descriptions
    specialist_lines = ["\n\nAVAILABLE SPECIALIST AGENTS:"]
    
    for agent_key, agent in specialist_agents.items():
        config = AGENT_CONFIGS.get(agent_key)
        if config:
            specialist_lines.append(f"\n**{config.name}**")
            specialist_lines.append(f"  Description: {config.description}")
            specialist_lines.append(f"  Assigned servers: {', '.join(config.mcp_servers)}")
    
    specialist_section = "\n".join(specialist_lines)
    
    routing_guidance = """

ROUTING GUIDANCE BY DOMAIN:
- Academic/Research/NASA/Museums/Wikipedia → ResearcherAgent
- Health/Medical/Nutrition/Clinical → HealthBioSpecialist
- Math/Computing/Units/Time/APIs/NixOS → QuantDeveloperAgent
- Crypto/DeFi/Cars/Gaming → MarketAnalystAgent
- Maps/Weather/Parks/Movies/Reddit → LifestyleGuideAgent
- OSINT/Icons/AI-Models/Divination → NicheSpecialistAgent

RESPONSE FORMAT:
When you have gathered all necessary information from specialists, provide a clear, comprehensive answer that synthesizes their contributions. Always cite which specialist(s) provided the information."""

    return base_instruction + specialist_section + routing_guidance


def create_coordinator_agent(
    server_configs: List[Dict[str, Any]],
    config: Optional[Config] = None,
    model_override: Optional[str] = None,
    server_names: Optional[List[str]] = None,
) -> Agent:
    """Create the coordinator agent with its specialist sub-agents.
    
    This function creates the complete multi-agent hierarchy:
    - Coordinator (parent) - routes requests to specialists
    - Specialist agents (sub-agents) - each with domain-specific MCP tools
    
    Args:
        server_configs: List of server configuration dictionaries
        config: Optional Config instance for model configuration
        model_override: Optional model name to override default
        server_names: Optional list of available server names
        
    Returns:
        Configured coordinator Agent with specialist sub-agents
    """
    config = config or Config()
    
    # Create specialist agents factory
    factory = SpecialistAgentFactory(
        server_configs=server_configs,
        config=config,
        model_override=model_override,
    )
    
    # Create all relevant specialist agents
    specialist_agents = factory.create_all_relevant_agents(server_names)
    
    if not specialist_agents:
        logger.warning("No specialist agents created. Coordinator will have no sub-agents.")
    
    # Build dynamic instruction based on available specialists
    coordinator_instruction = build_coordinator_instruction(specialist_agents)
    
    # Get list of specialist agents for sub_agents parameter
    sub_agents = factory.get_agents_list()
    
    # Create the coordinator agent
    coordinator = Agent(
        name=COORDINATOR_CONFIG.name,
        model=config.get_model_for_agent(model_override),
        description=COORDINATOR_CONFIG.description,
        instruction=coordinator_instruction,
        sub_agents=sub_agents,
        # Coordinator doesn't have direct tools - only routes to specialists
        tools=[],
    )
    
    logger.info(
        f"Created coordinator agent '{COORDINATOR_CONFIG.name}' with "
        f"{len(sub_agents)} specialist sub-agents"
    )
    
    # Log agent hierarchy
    agent_summary = factory.get_agent_server_summary()
    for agent_key, servers in agent_summary.items():
        logger.info(f"  - {agent_key}: {servers}")
    
    return coordinator


class MultiAgentOrchestrator:
    """Orchestrates the multi-agent system for task execution.
    
    This class manages the coordinator and its specialist agents,
    providing a high-level interface for task execution.
    """
    
    def __init__(
        self,
        server_configs: List[Dict[str, Any]],
        config: Optional[Config] = None,
        model_override: Optional[str] = None,
    ):
        """Initialize the multi-agent orchestrator.
        
        Args:
            server_configs: List of server configuration dictionaries
            config: Optional Config instance for model configuration
            model_override: Optional model name to override default
        """
        self.server_configs = server_configs
        self.config = config or Config()
        self.model_override = model_override
        self.coordinator: Optional[Agent] = None
        self._factory: Optional[SpecialistAgentFactory] = None
    
    def initialize(self, server_names: Optional[List[str]] = None) -> Agent:
        """Initialize the multi-agent hierarchy.
        
        Args:
            server_names: Optional list of available server names
            
        Returns:
            The coordinator agent (root of the hierarchy)
        """
        self.coordinator = create_coordinator_agent(
            server_configs=self.server_configs,
            config=self.config,
            model_override=self.model_override,
            server_names=server_names,
        )
        
        return self.coordinator
    
    def get_coordinator(self) -> Optional[Agent]:
        """Get the coordinator agent.
        
        Returns:
            Coordinator Agent instance or None if not initialized
        """
        return self.coordinator
    
    def get_agent_hierarchy_info(self) -> Dict:
        """Get information about the agent hierarchy.
        
        Returns:
            Dictionary with hierarchy information
        """
        if not self.coordinator:
            return {"error": "Orchestrator not initialized"}
        
        info = {
            "coordinator": self.coordinator.name,
            "specialist_count": len(self.coordinator.sub_agents) if self.coordinator.sub_agents else 0,
            "specialists": []
        }
        
        if self.coordinator.sub_agents:
            for agent in self.coordinator.sub_agents:
                specialist_info = {
                    "name": agent.name,
                    "description": agent.description,
                    "tool_count": len(agent.tools) if agent.tools else 0,
                }
                info["specialists"].append(specialist_info)
        
        return info
