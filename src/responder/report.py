"""Summarize a folder of runs.

    python -m responder.report runs/dev
"""
import argparse
import json
import math
from pathlib import Path

metrics = ["root_node_correct", "cause_correct", "action_correct", "resolved"]


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def load(folder):
    return [json.loads(p.read_text()) for p in sorted(Path(folder).glob("inc*.json"))]


def summarize(records):
    n = len(records)
    out = {"n": n}
    for m in metrics:
        k = sum(r["score"][m] for r in records)
        lo, hi = wilson(k, n)
        out[m] = {"k": k, "rate": k / n if n else 0, "ci": [round(lo, 3), round(hi, 3)]}
    patch = [r for r in records if r["expected"]["action"] == "patch"]
    out["fix_pass_rate"] = {"k": sum(r["score"]["resolved"] for r in patch), "n": len(patch)}
    out["tokens_per_incident"] = round(sum(r["outcome"]["tokens"] for r in records) / n) if n else 0
    out["seconds_per_incident"] = round(sum(r["seconds"] for r in records) / n, 1) if n else 0
    return out


def table(records, key):
    groups = {}
    for r in records:
        groups.setdefault(r[key], []).append(r)
    return {g: summarize(rs) for g, rs in sorted(groups.items())}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    args = p.parse_args(argv)
    records = load(args.folder)
    report = {"overall": summarize(records), "by_kind": table(records, "kind"), "by_split": table(records, "split")}
    (Path(args.folder) / "report.json").write_text(json.dumps(report, indent=2))

    pct = lambda d: f"{100 * d['rate']:5.1f}%"
    print(f"{'':16s} {'n':>3s} {'root':>7s} {'cause':>7s} {'action':>7s} {'resolved':>9s} {'tokens':>7s}")
    rows = [("overall", report["overall"])] + list(report["by_kind"].items()) + list(report["by_split"].items())
    for name, s in rows:
        print(f"{name:16s} {s['n']:3d} {pct(s['root_node_correct']):>7s} {pct(s['cause_correct']):>7s} "
              f"{pct(s['action_correct']):>7s} {pct(s['resolved']):>9s} {s['tokens_per_incident']:7d}")
    o = report["overall"]
    lo, hi = o["resolved"]["ci"]
    print(f"\nresolved {o['resolved']['k']}/{o['n']} (95% CI {100 * lo:.0f} to {100 * hi:.0f}%), "
          f"fixes passing the hidden check {o['fix_pass_rate']['k']}/{o['fix_pass_rate']['n']}")


if __name__ == "__main__":
    main()
