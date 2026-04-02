"""Entry point for the LangGraph planner-executor workflow."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from graph import graph


def write_results_files(final_state: dict) -> None:
	plan = final_state.get("plan", [])
	results = final_state.get("results", [])

	payload = {
		"planner_steps": plan,
		"executor_results": results,
	}

	md_lines = [
		"# Planner-Executor Run Output",
		"",
		"## Planner Steps (JSON)",
		"",
		"```json",
		json.dumps(plan, indent=2, ensure_ascii=False),
		"```",
		"",
		"## Executor Results",
		"",
	]

	if not results:
		md_lines.append("No executor results were produced.")
	else:
		for item in results:
			step = item.get("step", "?")
			description = item.get("description", "")
			result = item.get("result", "")
			md_lines.extend(
				[
					f"### Step {step}",
					f"- Description: {description}",
					f"- Result: {result}",
					"",
				]
			)

	Path("result.md").write_text("\n".join(md_lines), encoding="utf-8")


async def main() -> None:
	goal = str(input("Enter the agent's goal: "))
	final_state = await graph.ainvoke(
		{
			"goal": goal,
			"plan": [],
			"current_step": 0,
			"results": [],
		}
	)

	print("Planner steps (JSON):\n")
	print(json.dumps(final_state.get("plan", []), indent=2, ensure_ascii=False))

	print("\nExecutor results:\n")
	for item in final_state.get("results", []):
		description = item.get("description", "")
		print(f"Step {item['step']}: {description}")
		print(f"Result: {item['result']}\n")

	write_results_files(final_state)
	print("Saved outputs to result.md")


if __name__ == "__main__":
	asyncio.run(main())
