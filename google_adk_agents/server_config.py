"""Server configuration loading utilities."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def get_server_configs(server_names: list[str] = None) -> list[dict]:
    """Load server configurations from commands.json.
    
    Args:
        server_names: Optional list of server names to load. If None, loads all.
        
    Returns:
        List of server configuration dictionaries
    """
    # Load commands.json directly
    commands_json_path = Path("mcp_servers/commands.json")
    with open(commands_json_path, 'r') as f:
        local_commands = json.load(f)
    
    # Load API keys directly
    api_keys = {}
    api_key_path = Path("mcp_servers/api_key")
    if api_key_path.exists():
        with open(api_key_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and '=' in line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    api_keys[key.strip()] = value.strip()
    
    configs = []
    available_servers = list(local_commands.keys())
    
    if server_names:
        # Filter to only requested servers
        for name in server_names:
            if name in local_commands:
                cmd_config = local_commands[name]
                config = build_server_config(name, cmd_config, api_keys)
                if config:
                    configs.append(config)
            else:
                logger.warning(f"Server '{name}' not found in commands.json")
                logger.info(f"Available servers: {available_servers}")
    else:
        # Load all servers
        for name, cmd_config in local_commands.items():
            config = build_server_config(name, cmd_config, api_keys)
            if config:
                configs.append(config)
    
    return configs


def build_server_config(name: str, cmd_config: dict, api_keys: dict) -> dict:
    """Build a server configuration dictionary.
    
    Args:
        name: Server name
        cmd_config: Command configuration from commands.json
        api_keys: Dictionary of API keys
        
    Returns:
        Server configuration dictionary
    """
    # Parse the command
    cmd_str = cmd_config.get("cmd", "")
    cmd_parts = cmd_str.split()
    
    # Get working directory
    cwd = cmd_config.get("cwd", ".")
    if cwd.startswith("../"):
        cwd = str(Path("mcp_servers") / cwd[3:])
    cwd = str(Path(cwd).resolve())
    
    # Get environment variables
    env = {}
    for var in cmd_config.get("env", []):
        if var in api_keys:
            env[var] = api_keys[var]
        elif var in os.environ:
            env[var] = os.environ[var]
    
    config = {
        "name": name,
        "command": cmd_parts,
        "cwd": cwd,
        "env": env if env else None,
        "transport": cmd_config.get("transport", "stdio"),
    }
    
    # Add HTTP-specific config
    if config["transport"] == "http":
        config["port"] = cmd_config.get("port")
        config["endpoint"] = cmd_config.get("endpoint", "/mcp")
    
    return config
