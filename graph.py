"""LangGraph workflow for the planner-executor quiz.

This module keeps the control flow equivalent to the original LangChain
version:
1. plan from the user goal
2. execute exactly one step per iteration
3. loop until the plan is exhausted
"""

from __future__ import annotations

import importlib
import json
import os
import re
from typing import Any, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

try:
	from langchain_groq import ChatGroq
except Exception:  # pragma: no cover - fallback for environments without Groq
	ChatGroq = None


class AgentState(TypedDict):
	goal: str
	plan: list
	current_step: int
	results: list


PLAN_SYSTEM = """Break the user goal into an ordered JSON list of steps.
Each step MUST follow this EXACT schema:
  {"step": int, "description": str, "tool": str or null, "args": dict or null}

Available tools:
  - fetch_wikipedia(topic: str)
  - fetch_data_source(source: str)
  - get_weather(city: str)

Use null for tool/args on synthesis or writing steps.
IMPORTANT:
- The FINAL step MUST always be a concise summary (1–2 lines) of all gathered results.
Return ONLY a valid JSON array. No markdown, no explanation."""


TOOL_ARG_MAP = {
	"fetch_wikipedia": "topic",
	"fetch_data_source": "source",
	"get_weather": "city",
	"search_web": "query",
	"search_news": "query",
	"get_current_weather": "city",
}


TOOL_NAME_ALIASES = {
	"fetch_wikipedia": "search_web",
	"fetch_data_source": "search_web",
	"get_weather": "get_current_weather",
}


_runtime: dict[str, Any] = {"llm": None, "tools_map": None}


def safe_args(tool_name: str, raw_args: dict[str, Any] | None) -> dict[str, Any]:
	"""Remap hallucinated argument names to the expected parameter name."""

	raw_args = raw_args or {}
	expected = TOOL_ARG_MAP.get(tool_name)
	if not expected or expected in raw_args:
		return raw_args
	first_val = next(iter(raw_args.values()), tool_name)
	return {expected: str(first_val)}


def _extract_text(content: Any) -> str:
	if isinstance(content, str):
		return content
	if isinstance(content, list) and content:
		first_item = content[0]
		if isinstance(first_item, dict):
			return first_item.get("text", "")
		return str(first_item)
	return str(content)


def _strip_json_fences(text: str) -> str:
	return re.sub(r"```json|```", "", text).strip()


def _parse_plan(raw_content: Any) -> list[dict[str, Any]]:
	text = _strip_json_fences(_extract_text(raw_content))
	plan = json.loads(text)
	if not isinstance(plan, list):
		raise ValueError("Planner response must be a JSON array.")
	return plan


def _build_context(results: list[dict[str, Any]]) -> str:
	return "\n".join(f"Step {item['step']}: {item['result']}" for item in results)


def _resolve_tool_name(tool_name: str, tools_map: dict[str, Any]) -> str | None:
	if tool_name in tools_map:
		return tool_name
	alias = TOOL_NAME_ALIASES.get(tool_name)
	if alias and alias in tools_map:
		return alias
	return None


async def _create_llm():
	load_dotenv()
	groq_model = os.getenv("GROQ_MODEL", "llama3-8b-8192")

	if ChatGroq is not None:
		return ChatGroq(
			model=groq_model,
			temperature=0,
			groq_api_key=os.getenv("GROQ_API_KEY"),
		)

	raise ImportError(
		"langchain_groq is not installed. Install langchain-groq or provide a compatible LLM."
	)


async def _load_tools_from_repo() -> dict[str, Any]:
	"""Load MCP tools either from an existing helper or from local servers."""

	helper_module_names = ("MCP_code", "mcp_code", "tools_helper")
	print("[MCP] Loading tools...")
	for module_name in helper_module_names:
		try:
			module = importlib.import_module(module_name)
			get_tools = getattr(module, "get_mcp_tools", None)
			if get_tools is None:
				continue
			tools, tools_map = await get_tools(["data", "weather", "search", "math"])
			if tools_map:
				print(f"[MCP] Loaded tools via {module_name}: {sorted(tools_map.keys())}")
				return tools_map
		except Exception:
			continue

	try:
		from langchain_mcp_adapters.client import MultiServerMCPClient
	except Exception as exc:  # pragma: no cover - dependency issue
		raise ImportError("langchain_mcp_adapters is required to load MCP tools.") from exc

	import sys

	server_config: dict[str, dict[str, Any]] = {
		"math": {
			"command": sys.executable,
			"args": [os.path.join("Tools", "math_server.py")],
			"transport": "stdio",
		},
		"search": {
			"command": sys.executable,
			"args": [os.path.join("Tools", "search_server.py")],
			"transport": "stdio",
		},
	}

	server_config["weather"] = {
		"url": os.getenv("WEATHER_MCP_URL", "http://localhost:8000/mcp"),
		"transport": "streamable_http",
	}

	if os.path.exists(os.path.join("Tools", "data_server.py")):
		server_config["data"] = {
			"command": sys.executable,
			"args": [os.path.join("Tools", "data_server.py")],
			"transport": "stdio",
		}

	client = MultiServerMCPClient(server_config)
	tools = []
	connected_servers = []
	failed_servers = []
	for server_name in server_config:
		try:
			server_tools = await client.get_tools(server_name=server_name)
			tools.extend(server_tools)
			connected_servers.append(server_name)
		except Exception:
			failed_servers.append(server_name)
			continue

	tools_map = {tool.name: tool for tool in tools}
	print(f"[MCP] Connected servers: {connected_servers}")
	if failed_servers:
		print(f"[MCP] Skipped/unavailable servers: {failed_servers}")
	print(f"[MCP] Loaded tools: {sorted(tools_map.keys())}")
	return tools_map


async def get_runtime() -> tuple[Any, dict[str, Any]]:
	if _runtime["llm"] is None:
		_runtime["llm"] = await _create_llm()
	if _runtime["tools_map"] is None:
		_runtime["tools_map"] = await _load_tools_from_repo()
	return _runtime["llm"], _runtime["tools_map"]


async def planner_node(state: AgentState) -> dict[str, Any]:
	llm, _ = await get_runtime()
	response = await llm.ainvoke(
		[
			HumanMessage(content=PLAN_SYSTEM),
			HumanMessage(content=state["goal"]),
		]
	)
	plan = _parse_plan(response.content)
	return {
		"plan": plan,
		"current_step": 0,
		"results": [],
	}


async def executor_node(state: AgentState) -> dict[str, Any]:
	llm, tools_map = await get_runtime()

	if state["current_step"] >= len(state["plan"]):
		return {}

	step = state["plan"][state["current_step"]]
	tool_name = step.get("tool")
	result: Any

	resolved_tool_name = _resolve_tool_name(tool_name, tools_map) if tool_name else None

	if resolved_tool_name:
		corrected_args = safe_args(resolved_tool_name, step.get("args") or {})
		result = await tools_map[resolved_tool_name].ainvoke(corrected_args)
	else:
		context = _build_context(state["results"])
		synthesis_prompt = step.get("description", "")
		if context:
			synthesis_prompt = f"{synthesis_prompt}\n\nContext:\n{context}"
		response = await llm.ainvoke([HumanMessage(content=synthesis_prompt)])
		result = response.content

	updated_results = state["results"] + [
		{
			"step": step.get("step", state["current_step"] + 1),
			"description": step.get("description", ""),
			"result": str(result),
		}
	]

	return {
		"results": updated_results,
		"current_step": state["current_step"] + 1,
	}


def route_after_executor(state: AgentState) -> str:
	return "executor_node" if state["current_step"] < len(state["plan"]) else "end"


def build_graph():
	workflow = StateGraph(AgentState)
	workflow.add_node("planner_node", planner_node)
	workflow.add_node("executor_node", executor_node)
	workflow.add_edge(START, "planner_node")
	workflow.add_edge("planner_node", "executor_node")
	workflow.add_conditional_edges(
		"executor_node",
		route_after_executor,
		{
			"executor_node": "executor_node",
			"end": END,
		},
	)
	return workflow.compile()


graph = build_graph()
