"""Run the responder on incidents and score it against the answer key.

    python -m responder.run --ids inc025 --out runs/dev
"""
import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from langchain_mcp_adapters.client import MultiServerMCPClient

from . import incident_report, workspace
from .agents import build_graph, build_single_agent
from .incidents import catalog
from .llm import make_llm, tracing_callbacks
from .tools import problems

root = Path(__file__).resolve().parents[2]


def node_name(node):
    # model.tpch_warehouse.stg_orders and model.stg_orders are the same model
    parts = (node or "").lower().split(".")
    if parts[0] == "model":
        return "model." + parts[-1]
    if parts[0] == "source":
        return "source.raw." + parts[-1]
    return ".".join(parts)


def score(inc, outcome, fix_check):
    root_ok = node_name(outcome.get("root_node")) == node_name(inc.root_node)
    columns = {c.lower() for c in [inc.root_column or "", *inc.also_accept]}
    return {
        "root_node_correct": root_ok,
        "root_column_correct": root_ok and (outcome.get("root_column") or "").lower() in columns,
        "cause_correct": outcome.get("cause") == inc.cause,
        "action_correct": outcome.get("action") == inc.action,
        # a patch incident only counts as resolved if the agents' fix passes the hidden check
        "resolved": (inc.action == "escalate" and outcome.get("action") == "escalate")
                    or (inc.action == "patch" and bool(fix_check and fix_check["ok"])),
    }


def rules_baseline(work):
    # what dbt alone tells the on-call: the model behind the first alarm. no diagnosis, no fix, so escalate.
    res = json.loads((work / "target" / "run_results.json").read_text())["results"]
    manifest = json.loads((work / "target" / "manifest.json").read_text())
    bad = next(r["unique_id"] for r in res if r["status"] in ("fail", "error"))
    if bad.startswith("test."):
        models = [n for n in manifest["nodes"][bad]["depends_on"]["nodes"] if n.startswith("model.")]
        bad = models[0] if models else bad
    return {"steps": [{"agent": "rules", "output": bad}], "outcome": {
        "action": "escalate", "root_node": "model." + bad.split(".")[-1], "root_column": None,
        "cause": "upstream_data", "patch": None, "escalated_after_failed_fix": False, "tokens": 0}}


async def run_one(inc, out_dir, llm, kit, golden, system="multi"):
    # the working copy of the warehouse is scratch, keep it off the results folder (on colab that is drive)
    scratch = Path(tempfile.gettempdir()) / "responder-work" / f"{out_dir.parent.name}-{out_dir.name}" / inc.id
    work = workspace.prepare(inc, root / "warehouse", kit / "source.duckdb", scratch)
    workspace.build(work)
    alarms = problems(work).get("problems", [])
    t0 = time.time()
    if system == "rules":
        state = rules_baseline(work)
    else:
        client = MultiServerMCPClient({"warehouse": {
            "command": sys.executable, "transport": "stdio",
            "args": ["-m", "responder.mcp_server", "--project", str(work)]}})
        tools = await client.get_tools()
        graph = build_graph(llm, tools) if system == "multi" else build_single_agent(llm, tools)
        state = await graph.ainvoke({}, config={
            "callbacks": tracing_callbacks(),
            "metadata": {"langfuse_session_id": f"{out_dir.name}-{inc.id}",
                         "langfuse_tags": [system, inc.kind, inc.split]},
            "recursion_limit": 40})
    seconds = time.time() - t0
    outcome = state["outcome"]
    patch = outcome.get("patch")
    if patch and patch.get("original") is None and (work / patch["file"]).is_file():
        patch["original"] = (work / patch["file"]).read_text()  # for the diff in the report
    report = incident_report.render(inc.id, alarms, outcome, state["steps"], work)

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
    (out_dir / f"{inc.id}.md").write_text(report, encoding="utf-8")
    return record


async def main_async(args):
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    kit = root / "kit"
    golden = json.loads((kit / "golden.json").read_text())
    llm = None if args.system == "rules" else make_llm(args.provider, args.model, args.temperature)
    incs = [i for i in catalog() if (not args.ids or i.id in args.ids.split(","))
            and (not args.split or i.split == args.split)
            and not (out_dir / f"{i.id}.json").exists()]  # resume: finished incidents are skipped
    gate = asyncio.Semaphore(args.workers)

    async def one(inc):
        async with gate:
            try:
                r = await run_one(inc, out_dir, llm, kit, golden, args.system)
            except Exception as e:  # one broken incident shouldn't stop the batch, it gets rerun next time
                print(f"{inc.id} failed to run: {type(e).__name__}: {str(e)[:200]}")
                return
        s = r["score"]
        print(f"{inc.id} {inc.kind:12s} expected {inc.action:8s} got {r['outcome']['action']:8s} "
              f"root={'ok' if s['root_node_correct'] else 'x'} resolved={'ok' if s['resolved'] else 'x'} "
              f"tokens={r['outcome']['tokens']} {r['seconds']}s", flush=True)

    await asyncio.gather(*(one(i) for i in incs))


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--ids", default=None)
    p.add_argument("--split", default=None, choices=["dev", "holdout"])
    p.add_argument("--system", default="multi", choices=["multi", "single", "rules"])
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--temperature", type=float, default=0)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--out", default="runs/dev")
    asyncio.run(main_async(p.parse_args(argv)))


if __name__ == "__main__":
    main()
