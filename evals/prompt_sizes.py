"""How much static text each agent sends the model on every call.

    uv run python -m evals.prompt_sizes            # table
    uv run python -m evals.prompt_sizes --json     # for comparing two runs

instruction  the assembled system instruction (memory left out: it is data)
tools        the tools it holds and their declarations (name + description +
             parameter schema, as JSON). MCP tools are counted by name only, from
             project.yaml — their schemas come from the server at run time.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch


def sizes() -> dict[str, dict]:
    from meeseek import engine
    from meeseek.config import cfg
    from meeseek.core.turn import TurnState
    from meeseek.runtime.skills import discover_skills

    mcp = {s.agent_name: [t for srv in cfg.mcp_servers if s.name in (srv.agents or []) for t in (srv.tool_filter or [f"({srv.name})"])]
           for s in discover_skills() if s.use_mcp}
    with patch("meeseek.context.memory.memory_instruction", lambda *a, **k: ""):
        out = {}
        for agent in engine.agents():
            t = TurnState(session="sizes", turn="t", watched=True, started=0.0, history=[], agent=agent.name)
            decl = sum(len(json.dumps({"name": s.name, "description": s.description, "parameters": s.parameters},
                                      ensure_ascii=False)) for s in agent.tools)
            out[agent.name] = {"instruction": len(agent.system(t)), "tools": len(agent.tools),
                               "tool_chars": decl, "mcp_tools": len(mcp.get(agent.name, []))}
    return out


def main() -> int:
    data = sizes()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=1))
        return 0
    print(f"{'agent':24} {'instruction':>11} {'tools':>6} {'tool chars':>10} {'mcp tools':>9}")
    for name, d in data.items():
        print(f"{name:24} {d['instruction']:>11} {d['tools']:>6} {d['tool_chars']:>10} {d['mcp_tools']:>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
