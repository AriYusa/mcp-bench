"""ADK-specific configuration loader.

Loads google_adk_agents/adk_benchmark_config.yaml and exposes
typed accessor functions so both adk_executor.py and
adk_benchmark_runner.py can read ADK-specific values without
duplicating YAML parsing logic.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_CONFIG_PATH = Path(__file__).parent / "adk_benchmark_config.yaml"
_config: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    global _config
    if _config is None:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
                _config = yaml.safe_load(fh) or {}
        else:
            _config = {}
    return _config


def get(key_path: str, default: Any = None) -> Any:
    """Retrieve a value via dot-separated key path."""
    keys = key_path.split(".")
    node = _load()
    for k in keys:
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            return default
    return node


# ---------------------------------------------------------------------------
# content_compression
# ---------------------------------------------------------------------------

def get_compression_token_threshold() -> int:
    return get("content_compression.token_threshold", 100_000)


def get_compression_tool_result_threshold() -> int:
    return get("content_compression.tool_result_threshold", 10_000)


def get_compression_hard_limit_threshold() -> int:
    return get("content_compression.hard_limit_threshold", 180_000)


def get_compression_compressor_model() -> Optional[str]:
    return get("content_compression.compressor_model", None)


# ---------------------------------------------------------------------------
# adk_execution
# ---------------------------------------------------------------------------

def get_app_name() -> str:
    return get("adk_execution.app_name", "mcp_bench_adk")


def get_user_id() -> str:
    return get("adk_execution.user_id", "benchmark_user")


def is_preload_mcp_toolsets_enabled() -> bool:
    return get("adk_execution.preload_mcp_toolsets", True)


def get_max_llm_invocations() -> Optional[int]:
    return get("adk_execution.max_llm_invocations", None)


def get_agent_routing_mode() -> str:
    """Return the specialist routing mode: 'sub_agents' (default) or 'tools'."""
    value = get("adk_execution.agent_routing_mode", "sub_agents")
    if value not in ("sub_agents", "tools"):
        raise ValueError(
            f"Invalid agent_routing_mode '{value}'. Must be 'sub_agents' or 'tools'."
        )
    return value


# ---------------------------------------------------------------------------
# adk_models
# ---------------------------------------------------------------------------

def get_default_model() -> str:
    return get("adk_models.default_model", "anthropic/claude-sonnet-4-6")


def get_judge_model() -> str:
    return get("adk_models.judge_model", "anthropic/claude-sonnet-4-6")

def get_available_models() -> Dict[str, Any]:
    return get("adk_models.available_models", {
        "gemini-2.0-flash-exp": {"display_name": "Gemini 2.0 Flash Experimental"},
        "gemini-1.5-pro": {"display_name": "Gemini 1.5 Pro"},
        "gemini-1.5-flash": {"display_name": "Gemini 1.5 Flash"},
        "anthropic/claude-sonnet-4-6": {"display_name": "Claude Sonnet 4.6"},
    })


# ---------------------------------------------------------------------------
# adk_mcp
# ---------------------------------------------------------------------------

def is_add_server_prefix_enabled() -> bool:
    return get("adk_mcp.add_server_prefix", True)


def get_servers_catalog_path() -> str:
    return get("adk_mcp.servers_catalog_path", "mcp_servers_info.json")


def get_resident_servers() -> List[str]:
    return get("adk_mcp.resident_servers", ["Time MCP"])


# ---------------------------------------------------------------------------
# adk_benchmark
# ---------------------------------------------------------------------------

def is_dependency_analysis_ref_enabled() -> bool:
    return get("adk_benchmark.enable_dependency_analysis_ref_for_eval", True)


# ---------------------------------------------------------------------------
# benchmark (copied from root benchmark_config.yaml)
# ---------------------------------------------------------------------------

def get_tasks_file() -> Optional[str]:
    return get("benchmark.tasks_file", None)


def get_all_task_files() -> List[str]:
    return get("benchmark.all_task_files", [
        "./tasks/mcpbench_tasks_single_runner_format.json",
        "./tasks/mcpbench_tasks_multi_2server_runner_format.json",
        "./tasks/mcpbench_tasks_multi_3server_runner_format.json",
    ])


def get_distraction_servers_count() -> int:
    return get("benchmark.distraction_servers_default", 10)


def is_judge_stability_enabled() -> bool:
    return get("benchmark.enable_judge_stability", True)


def is_problematic_tools_filter_enabled() -> bool:
    return get("benchmark.filter_problematic_tools", True)


def is_concurrent_summarization_enabled() -> bool:
    return get("benchmark.concurrent_summarization", True)


def use_fuzzy_descriptions() -> bool:
    return get("benchmark.use_fuzzy_descriptions", True)


def is_concrete_description_ref_enabled() -> bool:
    return get("benchmark.enable_concrete_description_ref_for_eval", True)


# ---------------------------------------------------------------------------
# execution (copied from root benchmark_config.yaml)
# ---------------------------------------------------------------------------

def get_task_timeout() -> int:
    return get("execution.task_timeout", 5000)


def get_max_retries() -> int:
    return get("execution.task_retry_max", 3)


def get_retry_delay() -> int:
    return get("execution.retry_delay", 5)


def get_problematic_tools() -> List[str]:
    return get("execution.problematic_tools", [])


# ---------------------------------------------------------------------------
# mcp (copied from root benchmark_config.yaml)
# ---------------------------------------------------------------------------

def get_default_port() -> int:
    return get("mcp.default_port", 3001)


# ---------------------------------------------------------------------------
# evaluation (copied from root benchmark_config.yaml)
# ---------------------------------------------------------------------------

def get_judge_stability_runs() -> int:
    return get("evaluation.judge_stability_runs", 5)
