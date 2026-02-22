#!/usr/bin/env python3
"""
Entry point script for MCP Benchmark Runner.

This script provides a convenient way to run the benchmark system
without needing to use module syntax.
"""
import sys
from pathlib import Path
from langfuse import get_client
from openinference.instrumentation.google_adk import GoogleADKInstrumentor
from dotenv  import load_dotenv

load_dotenv()

# Add project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

langfuse = get_client()
GoogleADKInstrumentor().instrument()

if __name__ == "__main__":
    import asyncio
    
    # Import and run the main function from benchmark runner
    try:
        from benchmark.runner import main
        asyncio.run(main())
    except ImportError as e:
        print(f"Import error: {e}")
        print("Please ensure all dependencies are installed and you're running from the project root.")
        sys.exit(1)