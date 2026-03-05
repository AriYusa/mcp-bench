"""
Coordinator Agent Module

The coordinator agent is the main orchestrating agent that routes tasks
to appropriate specialist agents based on domain expertise.
"""

import logging
from typing import Any, Dict, List, Optional

from google.adk import Agent
from google.adk.tools.agent_tool import AgentTool

from .config import Config
from .agent_mcp_mapping import COORDINATOR_CONFIG, AGENT_CONFIGS
from .specialists import SpecialistAgentFactory
from . import adk_config_loader

logger = logging.getLogger(__name__)


def build_coordinator_instruction(specialist_agents: Dict[str, Agent], routing_mode: str = "sub_agents") -> str:
    """Build dynamic instruction for coordinator based on available specialists.
    
    Args:
        specialist_agents: Dictionary mapping agent keys to Agent instances
        
    Returns:
        Formatted instruction string for the coordinator
    """
    if routing_mode == "tools":
        routing_rule = (
            "- Always delegate to specialists rather than answering directly\n"
            "- For multi-domain requests, you can call several specialists at the same time, if their sub-tasks are independent\n"
            "- For several subtasks of the same domain that can be handled by the same specialist, call that specialist once with all relevant subtasks\n"
            "- Once all needed information is gathered, synthesize the final answer"
        )
    else:
        routing_rule = (
            "- Always delegate to specialists rather than answering directly\n"
            "- For complex requests spanning multiple domains, you might need several specialists\n"
            "- Once all needed information is gathered, synthesize the final answer"
        )

    base_instruction = f"""You are the Coordinator agent, responsible for understanding user requests and routing them to the appropriate specialist agents.

Your primary role is to:
1. Analyze the user's request to understand the domain(s) involved
2. Route the request to the most appropriate specialist agent
3. For multi-domain requests, coordinate between multiple specialists sequentially
4. Synthesize final responses when multiple agents contribute

IMPORTANT ROUTING RULES:
{routing_rule}"""

    # Build specialist descriptions
    specialist_lines = ["\n\nAVAILABLE SPECIALIST AGENTS:"]
    
    for agent_key, agent in specialist_agents.items():
        config = AGENT_CONFIGS.get(agent_key)
        if config:
            specialist_lines.append(f"\n**{config.name}**")
            specialist_lines.append(f"  Description: {config.description}")
            specialist_lines.append(f"  Assigned servers: {', '.join(config.mcp_servers)}")
    
    specialist_section = "\n".join(specialist_lines)
    
    routing_guidance = f"""

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
    routing_mode: Optional[str] = None,
) -> Agent:
    """Create the coordinator agent with its specialist sub-agents or agent-tools.
    
    This function creates the complete multi-agent hierarchy:
    - Coordinator (parent) - routes requests to specialists
    - Specialist agents with domain-specific MCP tools, wired as either:
        sub_agents: classic ADK routing via transfer_to_agent
        tools:      each specialist wrapped with AgentTool, called directly
    
    Args:
        server_configs: List of server configuration dictionaries
        config: Optional Config instance for model configuration
        model_override: Optional model name to override default
        server_names: Optional list of available server names
        routing_mode: Override for agent routing mode ('sub_agents' or 'tools').
            Defaults to the value in adk_benchmark_config.yaml.
        
    Returns:
        Configured coordinator Agent
    """
    config = config or Config()
    if routing_mode is None:
        routing_mode = adk_config_loader.get_agent_routing_mode()
    
    logger.info(f"Building coordinator with routing_mode='{routing_mode}'")
    
    # Create specialist agents factory
    factory = SpecialistAgentFactory(
        server_configs=server_configs,
        config=config,
        model_override=model_override,
        routing_mode=routing_mode,
    )
    
    # Create all relevant specialist agents
    specialist_agents = factory.create_all_relevant_agents(server_names)
    
    if not specialist_agents:
        logger.warning("No specialist agents created. Coordinator will have no sub-agents.")
    
    # Build dynamic instruction based on available specialists and routing mode
    coordinator_instruction = build_coordinator_instruction(specialist_agents, routing_mode)
    
    if routing_mode == "tools":
        # Wrap each specialist as an AgentTool so the coordinator calls it
        # as a regular function-call tool instead of transferring to it.
        agent_tools = [AgentTool(agent=specialist) for specialist in factory.get_agents_list()]
        coordinator = Agent(
            name=COORDINATOR_CONFIG.name,
            model=config.get_model_for_agent(model_override),
            description=COORDINATOR_CONFIG.description,
            instruction=coordinator_instruction,
            tools=agent_tools,
            sub_agents=[],
        )
        logger.info(
            f"Created coordinator agent '{COORDINATOR_CONFIG.name}' with "
            f"{len(agent_tools)} specialist agent-tools (routing_mode='tools')"
        )
    else:
        # sub_agents mode: classic ADK sub-agent transfer
        sub_agents = factory.get_agents_list()
        coordinator = Agent(
            name=COORDINATOR_CONFIG.name,
            model=config.get_model_for_agent(model_override),
            description=COORDINATOR_CONFIG.description,
            instruction=coordinator_instruction,
            sub_agents=sub_agents,
            tools=[],
        )
        logger.info(
            f"Created coordinator agent '{COORDINATOR_CONFIG.name}' with "
            f"{len(sub_agents)} specialist sub-agents (routing_mode='sub_agents')"
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
        routing_mode: Optional[str] = None,
    ):
        """Initialize the multi-agent orchestrator.
        
        Args:
            server_configs: List of server configuration dictionaries
            config: Optional Config instance for model configuration
            model_override: Optional model name to override default
            routing_mode: Override for specialist routing mode.
                Defaults to adk_benchmark_config.yaml value.
        """
        self.server_configs = server_configs
        self.config = config or Config()
        self.model_override = model_override
        self.routing_mode = routing_mode or adk_config_loader.get_agent_routing_mode()
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
            routing_mode=self.routing_mode,
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
            "routing_mode": self.routing_mode,
        }

        if self.routing_mode == "tools":
            # Specialists are stored as AgentTool objects in coordinator.tools
            agent_tools = [
                t for t in (self.coordinator.tools or [])
                if isinstance(t, AgentTool)
            ]
            info["specialist_count"] = len(agent_tools)
            info["specialists"] = [
                {
                    "name": at.agent.name,
                    "description": at.agent.description,
                    "tool_count": len(at.agent.tools) if at.agent.tools else 0,
                }
                for at in agent_tools
            ]
        else:
            info["specialist_count"] = len(self.coordinator.sub_agents) if self.coordinator.sub_agents else 0
            info["specialists"] = []
            if self.coordinator.sub_agents:
                for agent in self.coordinator.sub_agents:
                    info["specialists"].append({
                        "name": agent.name,
                        "description": agent.description,
                        "tool_count": len(agent.tools) if agent.tools else 0,
                    })
        
        return info
