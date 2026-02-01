"""
Google ADK Multi-Agent System for MCP-Bench

This package provides a multi-agent system using Google ADK (Agent Development Kit)
to replace the single-agent TaskExecutor with a coordinator-specialist architecture.

The system consists of:
- Coordinator Agent: Routes tasks to appropriate specialists
- 6 Specialist Agents:
  1. ResearcherAgent: Academic, NASA, museums, Wikipedia
  2. HealthBioSpecialist: Clinical, medical, nutrition
  3. QuantDeveloperAgent: Math, computing, time, APIs
  4. MarketAnalystAgent: Crypto, finance, automotive, gaming
  5. LifestyleGuideAgent: Maps, weather, parks, entertainment
  6. NicheSpecialistAgent: OSINT, design, AI/ML, divination

Usage:
    from google_adk_agents import ADKTaskExecutor, create_adk_executor
    
    executor = await create_adk_executor(server_configs)
    result = await executor.execute("Your task description")
"""

from .config import Config
from .agent_mcp_mapping import (
    AGENT_CONFIGS,
    COORDINATOR_CONFIG,
    get_all_agent_names,
    get_agent_config,
    get_server_to_agent_mapping,
    get_agents_for_servers,
    filter_agent_servers,
)
from .mcp_tools import (
    create_mcp_tools_for_agent,
    create_toolset_for_server,
    create_toolsets_for_servers,
)
from .coordinator import (
    create_coordinator_agent,
    MultiAgentOrchestrator,
)
from .adk_executor import ADKTaskExecutor, create_adk_executor

__all__ = [
    # Configuration
    "Config",
    "AGENT_CONFIGS",
    "COORDINATOR_CONFIG",
    
    # Mapping functions
    "get_all_agent_names",
    "get_agent_config",
    "get_server_to_agent_mapping",
    "get_agents_for_servers",
    "filter_agent_servers",
    
    # Tool utilities
    "create_mcp_tools_for_agent",
    "create_toolset_for_server",
    "create_toolsets_for_servers",
    
    # Coordinator
    "create_coordinator_agent",
    "MultiAgentOrchestrator",
    
    # Main executor
    "ADKTaskExecutor",
    "create_adk_executor",
]
