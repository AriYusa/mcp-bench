"""
Agent-MCP Server Mapping Configuration

This module defines the mapping between specialist agents and their assigned MCP servers.
Based on domain expertise categorization from mcp_to_agent.md.
"""

from typing import List, Dict, Set
from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """Configuration for a specialist agent."""
    name: str
    instruction: str
    mcp_servers: List[str] = field(default_factory=list)
    description: str = field(default="", init=True)

    def __post_init__(self):
        if self.mcp_servers:
            suffix = f"Has access to: {', '.join(self.mcp_servers)}"
            self.description = f"{self.description}. {suffix}" if self.description else suffix


# Agent configurations with their assigned MCP servers
AGENT_CONFIGS: Dict[str, AgentConfig] = {
    "researcher": AgentConfig(
        name="ResearcherAgent",
        description="Expert in academic research, scientific data, space/astronomy, art/history, and general reference. Handles high-level data gathering, paper trails, and museum archives.",
        instruction="""You are the Researcher agent, specializing in academic and scientific domains.
Your expertise includes:
- Academic research and paper discovery
- Space, astronomy, and NASA data
- Art, history, and museum collections
- General reference and foundational facts
- Conference and publication tracking

When answering questions:
1. Use the most appropriate tools from your available set
2. Cross-reference information when possible
3. Cite sources and provide context""",
        mcp_servers=[
            "Call for Papers",     # Conference tracking
            "Paper Search",        # Academic research
            "NASA Data",           # Space and astronomy
            "Metropolitan Museum", # Historical and art data
            "Wikipedia",           # General reference
        ]
    ),
    
    "health_bio_specialist": AgentConfig(
        name="HealthBioSpecialist",
        description="Expert in clinical data, health research, nutrition, and medical calculations. Focused on biomedical information and dietary data.",
        instruction="""You are the Health & Bio Specialist agent, focusing on clinical and nutritional domains.
Your expertise includes:
- Clinical trials and health research
- Medical calculations and formulas
- Nutrition and dietary data
- Biomedical information

When answering questions:
1. Prioritize accuracy for health-related information
2. Use appropriate medical calculators when needed
3. Provide nutritional context when relevant""",
        mcp_servers=[
            "BioMCP",              # Clinical trials and health research
            "Medical Calculator",  # Clinical formulas (medcalc)
            "FruityVice",          # Nutrition and dietary data
        ]
    ),
    
    "quant_developer": AgentConfig(
        name="QuantDeveloperAgent",
        description="Expert in mathematics, scientific computing, unit conversions, time/timezone logic, and technical documentation. A utility agent for heavy computational lifting.",
        instruction="""You are the Quant & Developer agent, specializing in computation and technical utilities.
Your expertise includes:
- Advanced mathematical calculations
- Scientific computing and analysis
- Unit conversions and measurements
- Timezone and date logic
- Project documentation and API exploration
- System configuration (NixOS)

When answering questions:
1. Use precise mathematical notation when appropriate
2. Show step-by-step calculations for complex problems
3. Convert units accurately with proper precision""",
        mcp_servers=[
            "Math MCP",            # Advanced calculations
            "Scientific Computing", # Scientific computation (scientific_computation_mcp)
            "Unit Converter",      # Measurement logic
            "Time MCP",            # Timezone and date logic
            "Context7",            # Project documentation
            "NixOS",               # System config (mcp-nixos)
            "OpenAPI Explorer",    # API testing (openapi-mcp-server)
        ],
    ),
    
    "market_analyst": AgentConfig(
        name="MarketAnalystAgent",
        description="Expert in financial data, cryptocurrency/DeFi trends, automotive market valuations, and gaming industry statistics.",
        instruction="""You are the Market & Crypto Analyst agent, focusing on financial and market data.
Your expertise includes:
- Cryptocurrency and DeFi market data
- Exchange information and trading pairs
- Automotive market trends and pricing
- Gaming industry statistics and trends

When answering questions:
1. Provide current market data when available
2. Include relevant market context
3. Note any data freshness considerations""",
        mcp_servers=[
            "DEX Paprika",         # Cryptocurrency and DeFi (dexpaprika-mcp)
            "OKX Exchange",        # Crypto exchange data (okx-mcp)
            "Car Price Evaluator", # Automotive market (car-price-mcp)
            "Game Trends",         # Gaming industry stats (game-trends-mcp)
        ]
    ),
    
    "lifestyle_guide": AgentConfig(
        name="LifestyleGuideAgent",
        description="Expert in travel, navigation, weather, outdoor activities, entertainment, and social trends. A consumer-facing agent for lifestyle queries.",
        instruction="""You are the Lifestyle & Exploration Guide agent, helping with consumer-facing queries.
Your expertise includes:
- Navigation and location-based services
- Weather forecasts and conditions
- National parks and outdoor activities
- Movie and entertainment recommendations
- Social trends and community sentiment

When answering questions:
1. Provide practical, actionable information
2. Consider user preferences and context
3. Include relevant location-based details""",
        mcp_servers=[
            "Google Maps",         # Navigation and geocoding (mcp-google-map)
            "Weather Data",        # Forecasts (weather_mcp)
            "National Parks",      # Travel and outdoors (mcp-server-nationalparks)
            "Movie Recommender",   # Entertainment (movie-recommender-mcp)
            "Reddit",              # Community sentiment (mcp-reddit)
        ]
    ),
    
    "niche_specialist": AgentConfig(
        name="NicheSpecialistAgent",
        description="Expert in specialized technical domains including security/OSINT, design resources, AI/ML models, and unconventional guidance tools.",
        instruction="""You are the Niche Specialist agent, handling specialized and technical domains.
Your expertise includes:
- Open-source intelligence (OSINT) and security research
- Design and UI resources (icons)
- AI/ML model information and deployment
- Unconventional guidance and divination tools

When answering questions:
1. Apply specialized knowledge appropriately
2. Provide detailed technical information when needed
3. Handle niche requests with appropriate expertise""",
        mcp_servers=[
            "OSINT Intelligence",  # Security and investigation (mcp-osint-server)
            "Huge Icons",          # Design and UI (hugeicons-mcp-server)
            "Hugging Face",        # AI/ML models (huggingface-mcp-server)
            "Bibliomantic",        # Divination (bibliomantic-mcp-server)
        ]
    ),
}


def get_all_agent_names() -> List[str]:
    """Get list of all agent names."""
    return list(AGENT_CONFIGS.keys())


def get_agent_config(agent_name: str) -> AgentConfig:
    """Get configuration for a specific agent."""
    if agent_name not in AGENT_CONFIGS:
        raise ValueError(f"Unknown agent: {agent_name}")
    return AGENT_CONFIGS[agent_name]


def get_server_to_agent_mapping() -> Dict[str, str]:
    """Create reverse mapping from MCP server to agent name."""
    mapping = {}
    for agent_name, config in AGENT_CONFIGS.items():
        for server in config.mcp_servers:
            mapping[server] = agent_name
    return mapping


def get_agents_for_servers(server_names: List[str]) -> Set[str]:
    """Determine which agents are needed based on the available servers."""
    server_to_agent = get_server_to_agent_mapping()
    needed_agents = set()
    
    for server in server_names:
        if server in server_to_agent:
            needed_agents.add(server_to_agent[server])
    
    return needed_agents


def filter_agent_servers(agent_name: str, available_servers: List[str]) -> List[str]:
    """Filter agent's configured servers to only those that are available."""
    config = get_agent_config(agent_name)
    return [s for s in config.mcp_servers if s in available_servers]


# Coordinator agent configuration
COORDINATOR_CONFIG = AgentConfig(
    name="CoordinatorAgent",
    description="Main orchestrating agent that routes tasks to appropriate specialist agents based on domain expertise.",
    instruction="",  # Coordinator instructions are defined separately in coordinator.py and depend on routing mode
    mcp_servers=[]  # Coordinator doesn't have direct MCP access
)
