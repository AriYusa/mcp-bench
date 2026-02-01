#!/usr/bin/env python3
"""
Standalone ADK Agent Runner

Run the ADK multi-agent system interactively without the benchmark framework.
This script allows you to chat with the ADK agent directly.

Usage:
    python run_adk_agent.py                     # Interactive mode with all servers
    python run_adk_agent.py --servers Weather Wikipedia  # Specific servers only
    python run_adk_agent.py --model gemini-2.0-flash     # Override model
    python run_adk_agent.py --task "What is the weather in Tokyo?"  # Single task
"""

import asyncio
import argparse
import logging
import sys
from pathlib import Path

# Configure logging BEFORE any other imports to prevent libraries from setting DEBUG level
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True  # Override any existing configuration
)
logger = logging.getLogger(__name__)

# Suppress noisy loggers from third-party libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("anyio").setLevel(logging.WARNING)

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv()

import json
import os

from mcp_modules.server_manager import MultiServerManager
from google_adk_agents import ADKTaskExecutor, Config

from langfuse import get_client
from openinference.instrumentation.google_adk import GoogleADKInstrumentor


langfuse = get_client()
# Verify connection
if langfuse.auth_check():
    logger.info("Langfuse client is authenticated and ready!")
else:
    logger.info("Authentication failed. Please check your credentials and host.")
GoogleADKInstrumentor().instrument()

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


async def run_interactive(executor: ADKTaskExecutor):
    """Run interactive chat session with the ADK agent.
    
    Args:
        executor: Initialized ADKTaskExecutor
    """
    print("\n" + "="*60)
    print("🤖 ADK Multi-Agent System Ready!")
    print("="*60)
    print("Type your questions or tasks. Type 'quit' or 'exit' to stop.")
    print("Type 'info' to see agent hierarchy.")
    print("-"*60 + "\n")
    
    while True:
        try:
            user_input = input("You: ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() in ['quit', 'exit', 'q']:
                print("\nGoodbye! 👋")
                break
            
            if user_input.lower() == 'info':
                executor.log_agent_info()
                continue
            
            print("\n🔄 Processing...\n")
            
            result = await executor.execute(user_input)
            
            print("-"*40)
            print("🤖 Agent Response:")
            print("-"*40)
            print(result.get("solution", "No response generated"))
            print("-"*40)
            print(f"📊 Stats: {result.get('total_rounds', 0)} rounds, "
                  f"{result.get('total_tokens', 0)} tokens")
            print()
            
        except KeyboardInterrupt:
            print("\n\nInterrupted. Goodbye! 👋")
            break
        except Exception as e:
            logger.error(f"Error during execution: {e}")
            print(f"\n❌ Error: {e}\n")


async def run_single_task(executor: ADKTaskExecutor, task: str):
    """Run a single task and print the result.
    
    Args:
        executor: Initialized ADKTaskExecutor
        task: Task description to execute
    """
    print(f"\n🔄 Executing task: {task}\n")
    
    result = await executor.execute(task)
    
    print("="*60)
    print("🤖 Agent Response:")
    print("="*60)
    print(result.get("solution", "No response generated"))
    print("="*60)
    print(f"\n📊 Stats:")
    print(f"  - Rounds: {result.get('total_rounds', 0)}")
    print(f"  - Prompt tokens: {result.get('total_prompt_tokens', 0)}")
    print(f"  - Output tokens: {result.get('total_output_tokens', 0)}")
    print(f"  - Total tokens: {result.get('total_tokens', 0)}")
    
    if result.get("execution_results"):
        print(f"  - Tool calls: {len(result['execution_results'])}")


async def main():
    parser = argparse.ArgumentParser(
        description="Run ADK Multi-Agent System standalone"
    )
    parser.add_argument(
        "--servers", 
        nargs="+",
        help="Specific MCP servers to use (default: all available)"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override the default model (e.g., gemini-2.0-flash, gpt-4o)"
    )
    parser.add_argument(
        "--task",
        type=str,
        default=None,
        help="Single task to execute (non-interactive mode)"
    )
    parser.add_argument(
        "--list-servers",
        action="store_true",
        help="List all available servers and exit"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # List servers if requested
    if args.list_servers:
        commands_json_path = Path("mcp_servers/commands.json")
        with open(commands_json_path, 'r') as f:
            local_commands = json.load(f)
        print("\n📦 Available MCP Servers:")
        print("-"*40)
        for name in sorted(local_commands.keys()):
            print(f"  • {name}")
        print("-"*40)
        print(f"Total: {len(local_commands)} servers")
        return
    
    # Load server configurations
    print("\n🔧 Loading server configurations...")
    server_configs = get_server_configs(args.servers)
    
    if not server_configs:
        print("❌ No server configurations loaded!")
        return
    
    print(f"✅ Loaded {len(server_configs)} server(s):")
    for cfg in server_configs:
        print(f"   • {cfg['name']}")
    
    # # Create server manager
    # print("\n🔌 Connecting to MCP servers...")
    # server_manager = MultiServerManager(server_configs)
    
    try:
        # await server_manager.connect_all_servers()
        # print(f"✅ Connected! Discovered {len(server_manager.all_tools)} tools")
        
        # Create ADK executor
        print("\n🚀 Initializing ADK Multi-Agent System...")
        executor = ADKTaskExecutor(
            server_configs=server_configs,
            model_override=args.model,
        )
        
        server_names = [cfg["name"] for cfg in server_configs]
        await executor.setup(server_names)
        print("✅ ADK system ready!")
        
        # Run in appropriate mode
        if args.task:
            await run_single_task(executor, args.task)
        else:
            await run_interactive(executor)
            
    except Exception as e:
        logger.error(f"Failed to start: {e}")
        raise
    # finally:
    #     # Cleanup
    #     print("\n🧹 Cleaning up...")
    #     await server_manager.close_all_connections()
    #     print("✅ Done!")


if __name__ == "__main__":
    asyncio.run(main())
