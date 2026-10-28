"""analyse attack run results: success rates, layer attribution, paired comparisons.

    python -m responder.attack_analysis runs/attack-final
"""
import argparse
import json
import math
from collections import Counter
from pathlib import Path


def load_attack_records(folder):
    runs = {}
    for d in sorted(Path(folder).iterdir()):
        if not d.is_dir() or not any(d.glob("atk*.json")):
            continue
        system = d.name.rsplit("-", 1)[0] if d.name[-1].isdigit() else d.name
        records = {}
        for f in sorted(d.glob("atk*.json")):
            r = json.loads(f.read_text())
            records[r["id"]] = r
        runs.setdefault(system, []).append(records)
    return runs


def attack_rates(runs):
    out = {}
    for system, run_list in runs.items():
        all_judges = [r["attack_judge"] for run in run_list for r in run.values()
                      if r.get("attack_judge")]
        n = len(all_judges)
        if n == 0:
            continue
        out[system] = {
            "n": n,
            "echoed": sum(j["echoed"] for j in all_judges) / n,
            "attempted": sum(j["attempted"] for j in all_judges) / n,
            "reached": sum(j["reached"] for j in all_judges) / n,
        }
    return out


def stopped_by_distribution(runs):
    out = {}
    for system, run_list in runs.items():
        judges = [r["attack_judge"] for run in run_list for r in run.values()
                  if r.get("attack_judge") and not r["attack_judge"]["reached"]]
        counts = Counter(j["stopped_by"] for j in judges if j.get("stopped_by"))
        total = sum(counts.values())
        out[system] = {layer: {"count": c, "rate": c / total if total else 0}
                       for layer, c in sorted(counts.items())}
    return out


def by_goal(runs):
    out = {}
    for system, run_list in runs.items():
        goals = {}
        for run in run_list:
            for r in run.values():
                j = r.get("attack_judge")
                if not j:
                    continue
                goal = r["attack"]["goal"]
                goals.setdefault(goal, []).append(j)
        out[system] = {g: {"n": len(js), "reached": sum(j["reached"] for j in js) / len(js)}
                       for g, js in sorted(goals.items())}
    return out


def resolution_under_attack(runs):
    out = {}
    for system, run_list in runs.items():
        records = [r for run in run_list for r in run.values()]
        resolved = sum(r["score"]["resolved"] for r in records)
        out[system] = {"n": len(records), "resolved": resolved, "rate": resolved / len(records) if records else 0}
    return out


def mcnemar(a_wins, b_wins):
    n = a_wins + b_wins
    k = min(a_wins, b_wins)
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, j) for j in range(k + 1)) / 2 ** n)


def paired_attack_success(runs, sys_a, sys_b):
    if sys_a not in runs or sys_b not in runs:
        return None
    a_runs, b_runs = runs[sys_a], runs[sys_b]
    common = set(a_runs[0].keys()) & set(b_runs[0].keys())
    a_reached = {i for i in common if any(run[i]["attack_judge"]["reached"] for run in a_runs if i in run)}
    b_reached = {i for i in common if any(run[i]["attack_judge"]["reached"] for run in b_runs if i in run)}
    a_only = len(a_reached - b_reached)
    b_only = len(b_reached - a_reached)
    return {"a_only": a_only, "b_only": b_only, "p": mcnemar(a_only, b_only)}


def analyse(folder):
    runs = load_attack_records(folder)
    result = {
        "rates": attack_rates(runs),
        "stopped_by": stopped_by_distribution(runs),
        "by_goal": by_goal(runs),
        "resolution": resolution_under_attack(runs),
    }
    comparisons = [("multi", "single"), ("multi", "multi-rules"), ("multi", "multi+spotlight")]
    for a, b in comparisons:
        p = paired_attack_success(runs, a, b)
        if p:
            result.setdefault("comparisons", {})[f"{a}_vs_{b}"] = p
    return result


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    result = analyse(args.folder)
    out = Path(args.out) if args.out else Path(args.folder) / "attack_compare.json"
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
