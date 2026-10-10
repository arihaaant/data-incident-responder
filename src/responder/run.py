"""Run the responder on incidents and score it against the answer key.

    python -m responder.run --ids inc025 --out runs/dev
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient

from . import workspace
from .agents import build_graph
from .incidents import catalog
from .llm import make_llm, tracing_callbacks

root = Path(__file__).resolve().parents[2]


def score(inc, outcome, fix_check):
    root_ok = outcome.get("root_node") == inc.root_node
    return {
        "root_node_correct": root_ok,
        "root_column_correct": root_ok and (outcome.get("root_column") or "").lower() == (inc.root_column or "").lower(),
        "cause_correct": outcome.get("cause") == inc.cause,
        "action_correct": outcome.get("action") == inc.action,
        # a patch incident only counts as resolved if the agents' fix passes the hidden check
        "resolved": (inc.action == "escalate" and outcome.get("action") == "escalate")
                    or (inc.action == "patch" and bool(fix_check and fix_check["ok"])),
    }


async def run_one(inc, out_dir, llm, kit, golden):
    work = workspace.prepare(inc, root / "warehouse", kit / "source.duckdb", out_dir / "work" / inc.id)
    workspace.build(work)
    client = MultiServerMCPClient({"warehouse": {
        "command": sys.executable, "transport": "stdio",
        "args": ["-m", "responder.mcp_server", "--project", str(work)]}})
    tools = await client.get_tools()
    graph = build_graph(llm, tools)
    t0 = time.time()
    state = await graph.ainvoke({}, config={
        "callbacks": tracing_callbacks(),
        "metadata": {"langfuse_session_id": f"{out_dir.name}-{inc.id}", "langfuse_tags": [inc.kind, inc.split]},
        "recursion_limit": 40})
    seconds = time.time() - t0
    outcome = state["outcome"]

    fix_check = None
    if outcome["action"] == "patch" and outcome["patch"]:
        path = work / outcome["patch"]["file"]
        if path.resolve().is_relative_to(work.resolve()) and path.exists():
            path.write_text(outcome["patch"]["sql"])
            fix_check = workspace.check_fix(work, golden)
    record = {"id": inc.id, "kind": inc.kind, "split": inc.split, "expected": {
        "root_node": inc.root_node, "root_column": inc.root_column, "cause": inc.cause, "action": inc.action},
        "outcome": {k: v for k, v in outcome.items() if k != "patch"}, "patch": outcome["patch"],
        "fix_check": fix_check, "score": score(inc, outcome, fix_check), "seconds": round(seconds, 1),
        "steps": state["steps"]}
    (out_dir / f"{inc.id}.json").write_text(json.dumps(record, indent=2, default=str))
    return record


async def main_async(args):
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    kit = root / "kit"
    golden = json.loads((kit / "golden.json").read_text())
    llm = make_llm(args.provider, args.model)
    incs = [i for i in catalog() if (not args.ids or i.id in args.ids.split(","))
            and (not args.split or i.split == args.split)]
    for inc in incs:
        if (out_dir / f"{inc.id}.json").exists():
            continue  # resume: finished incidents are skipped
        r = await run_one(inc, out_dir, llm, kit, golden)
        s = r["score"]
        print(f"{inc.id} {inc.kind:12s} expected {inc.action:8s} got {r['outcome']['action']:8s} "
              f"root={'ok' if s['root_node_correct'] else 'x'} resolved={'ok' if s['resolved'] else 'x'} "
              f"tokens={r['outcome']['tokens']} {r['seconds']}s")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--ids", default=None)
    p.add_argument("--split", default=None, choices=["dev", "holdout"])
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--out", default="runs/dev")
    asyncio.run(main_async(p.parse_args(argv)))


if __name__ == "__main__":
    main()
