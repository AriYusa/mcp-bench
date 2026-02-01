"""
Specialist Agents Package

This package contains specialist agents for the multi-agent MCP-Bench system.
Each specialist agent handles a specific domain of MCP tools.
"""

from .base import create_specialist_agent
from .factory import SpecialistAgentFactory

__all__ = [
    "create_specialist_agent",
    "SpecialistAgentFactory",
]
