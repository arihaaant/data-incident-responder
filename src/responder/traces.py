"""Compact Langfuse traces of a run into one small file per incident run.

    python -m responder.traces langfuse-export.json --runs runs/final --out results/final/traces

A Langfuse export repeats the whole conversation in every model call, so it runs to hundreds of MB. Each
compact trace keeps the timeline: every model call with its timing, tokens and what it decided, and every tool
call with its arguments and result. The prompts themselves are in agents.py.
"""
import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

session_name = re.compile(r"(multi|single|multi-rules)-\d+-(?:inc|atk)\d{3}")
agent_nodes = {"triage", "investigate", "fix", "review", "report", "solve"}
max_text = 2500


def when(ts):
    return datetime.fromisoformat(ts)


def text_of(output):
    """Tool results come back as mcp content blocks; keep the text."""
    try:
        blocks = json.loads(output)
    except (TypeError, ValueError):
        return output
    if isinstance(blocks, list):
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    return output


def clip(s):
    s = s if isinstance(s, str) else json.dumps(s)
    return s if len(s) <= max_text else s[:max_text] + " ...[clipped]"


def decision(output):
    """What the model said or which tools it called."""
    try:
        msg = json.loads(output)
    except (TypeError, ValueError):
        return {"text": clip(output or "")}
    out = {}
    if msg.get("content"):
        out["text"] = clip(msg["content"])
    if msg.get("tool_calls"):
        out["calls"] = [{"name": c["name"].split(".")[-1], "args": c.get("args", {})} for c in msg["tool_calls"]]
    return out


def compact(observations):
    root = next(o for o in observations if not o["parentObservationId"])
    t0 = when(root["startTime"])
    events = []
    for o in sorted(observations, key=lambda o: o["startTime"]):
        node = (o.get("metadata") or {}).get("langgraph_node", "")
        start = round((when(o["startTime"]) - t0).total_seconds(), 2)
        seconds = round((o["latencyMs"] or 0) / 1000, 2)
        if o["type"] == "GENERATION":
            u = o.get("usageDetails") or {}
            events.append({"at": start, "seconds": seconds, "agent": node, "kind": "model",
                           "tokens": {"in": u.get("input", 0), "out": u.get("output", 0),
                                      "reasoning": u.get("output_reasoning", 0)}, **decision(o["output"])})
        elif o["type"] == "TOOL":
            try:
                args = json.loads(o["input"])
            except (TypeError, ValueError):
                args = o["input"]
            events.append({"at": start, "seconds": seconds, "agent": node, "kind": "tool", "name": o["name"],
                           "args": args, "result": clip(text_of(o["output"]))})
        elif o["type"] == "CHAIN" and o["name"] in agent_nodes and o["name"] == node:
            events.append({"at": start, "seconds": seconds, "agent": node, "kind": "agent"})
    return {"session": root["sessionId"], "seconds": round(root["latencyMs"] / 1000, 1), "events": events}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("export")
    p.add_argument("--runs", default="runs/final")
    p.add_argument("--out", default="results/final/traces")
    args = p.parse_args(argv)
    by_trace = defaultdict(list)
    for o in json.load(open(args.export, encoding="utf-8")):
        by_trace[o["traceId"]].append(o)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    index = []
    for observations in by_trace.values():
        root = next((o for o in observations if not o["parentObservationId"]), None)
        if not root or not session_name.fullmatch(root["sessionId"] or ""):
            continue
        trace = compact(observations)
        run, incident = trace["session"].rsplit("-", 1)
        record = Path(args.runs) / run / f"{incident}.json"
        if record.exists():
            r = json.loads(record.read_text())
            trace.update(incident=incident, run=run, kind=r["kind"], split=r["split"], expected=r["expected"],
                         outcome={k: r["outcome"].get(k) for k in ("action", "root_node", "cause")},
                         score=r["score"], patch=r.get("patch") and {k: r["patch"].get(k) for k in
                                                                     ("file", "sql", "original")},
                         fix_check=r.get("fix_check"))
        (out / f"{trace['session']}.json").write_text(json.dumps(trace, separators=(",", ":")))
        index.append({k: trace.get(k) for k in ("session", "run", "incident", "kind", "split", "seconds")} |
                     {"resolved": trace.get("score", {}).get("resolved"),
                      "root": trace.get("score", {}).get("root_node_correct"),
                      "action": trace.get("outcome", {}).get("action"),
                      "expected": trace.get("expected", {}).get("action"),
                      "tokens": sum(e["tokens"]["in"] + e["tokens"]["out"] for e in trace["events"]
                                    if e["kind"] == "model")})
    index.sort(key=lambda i: i["session"])
    (out / "index.json").write_text(json.dumps(index, separators=(",", ":")))
    print(f"{len(index)} traces written to {out}")


if __name__ == "__main__":
    main()
