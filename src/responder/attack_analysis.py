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


def wilson(k, n, z=1.96):
    if n == 0:
        return 0, 0, 1
    p = k / n
    denom = 1 + z ** 2 / n
    centre = (p + z ** 2 / (2 * n)) / denom
    half = z * math.sqrt((p * (1 - p) + z ** 2 / (4 * n)) / n) / denom
    return p, max(0, centre - half), min(1, centre + half)


def attack_rates(runs):
    out = {}
    for system, run_list in runs.items():
        all_judges = [r["attack_judge"] for run in run_list for r in run.values()
                      if r.get("attack_judge")]
        n = len(all_judges)
        if n == 0:
            continue
        entry = {"n": n}
        for metric in ("echoed", "attempted", "reached"):
            k = sum(j[metric] for j in all_judges)
            rate, lo, hi = wilson(k, n)
            entry[metric] = {"k": k, "n": n, "rate": rate, "ci_lo": lo, "ci_hi": hi}
        out[system] = entry
    return out


def exposure_conditional_rates(runs):
    out = {}
    for system, run_list in runs.items():
        exposed = [r["attack_judge"] for run in run_list for r in run.values()
                   if r.get("attack_judge") and r["attack_judge"]["echoed"]]
        n = len(exposed)
        if n == 0:
            continue
        entry = {"n": n}
        for metric in ("attempted", "reached"):
            k = sum(j[metric] for j in exposed)
            rate, lo, hi = wilson(k, n)
            entry[metric] = {"k": k, "n": n, "rate": rate, "ci_lo": lo, "ci_hi": hi}
        out[system] = entry
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
        for g, js in goals.items():
            n = len(js)
            k = sum(j["reached"] for j in js)
            rate, lo, hi = wilson(k, n)
            goals[g] = {"k": k, "n": n, "rate": rate, "ci_lo": lo, "ci_hi": hi}
        out[system] = dict(sorted(goals.items()))
    return out


def resolution_under_attack(runs):
    out = {}
    for system, run_list in runs.items():
        records = [r for run in run_list for r in run.values()]
        k = sum(r["score"]["resolved"] for r in records)
        n = len(records)
        rate, lo, hi = wilson(k, n)
        out[system] = {"k": k, "n": n, "rate": rate, "ci_lo": lo, "ci_hi": hi}
    return out


def paired_resolution(attack_runs, clean_folder):
    """compare resolution of each attack with its base incident from clean runs."""
    if not Path(clean_folder).is_dir():
        return None
    from .compare import load_runs
    clean = load_runs(clean_folder)
    out = {}
    for system, run_list in attack_runs.items():
        clean_key = system.replace("+spotlight", "")
        if clean_key not in clean:
            continue
        pairs = []
        for run in run_list:
            for r in run.values():
                base_id = r.get("attack", {}).get("base_incident_id")
                if not base_id:
                    continue
                attack_resolved = r["score"]["resolved"]
                clean_resolved = any(
                    crun.get(base_id, {}).get("score", {}).get("resolved")
                    for crun in clean[clean_key]
                )
                pairs.append({"attack_id": r["id"], "base_id": base_id,
                              "attack_resolved": attack_resolved, "clean_resolved": clean_resolved})
        n = len(pairs)
        attack_k = sum(p["attack_resolved"] for p in pairs)
        clean_k = sum(p["clean_resolved"] for p in pairs)
        degraded = sum(p["clean_resolved"] and not p["attack_resolved"] for p in pairs)
        improved = sum(not p["clean_resolved"] and p["attack_resolved"] for p in pairs)
        out[system] = {
            "n": n, "attack_resolved": attack_k, "clean_resolved": clean_k,
            "degraded": degraded, "improved": improved,
            "p": mcnemar(improved, degraded),
        }
    return out


def mcnemar(a_wins, b_wins):
    n = a_wins + b_wins
    k = min(a_wins, b_wins)
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, j) for j in range(k + 1)) / 2 ** n)


def paired_attack_success(runs, sys_a, sys_b):
    """mcnemar on majority-vote per attack across runs, not treating runs as independent."""
    if sys_a not in runs or sys_b not in runs:
        return None
    a_runs, b_runs = runs[sys_a], runs[sys_b]
    common = set(a_runs[0].keys()) & set(b_runs[0].keys())
    a_majority = {i for i in common
                  if 2 * sum(run[i]["attack_judge"]["reached"] for run in a_runs if i in run) > len(a_runs)}
    b_majority = {i for i in common
                  if 2 * sum(run[i]["attack_judge"]["reached"] for run in b_runs if i in run) > len(b_runs)}
    a_only = len(a_majority - b_majority)
    b_only = len(b_majority - a_majority)
    return {"a_only": a_only, "b_only": b_only, "p": mcnemar(a_only, b_only)}


def analyse(folder, clean_folder=None):
    runs = load_attack_records(folder)
    result = {
        "rates": attack_rates(runs),
        "exposure_conditional": exposure_conditional_rates(runs),
        "stopped_by": stopped_by_distribution(runs),
        "by_goal": by_goal(runs),
        "resolution": resolution_under_attack(runs),
    }
    if clean_folder:
        pr = paired_resolution(runs, clean_folder)
        if pr:
            result["paired_resolution"] = pr
    comparisons = [("multi", "single"), ("multi", "multi-rules"), ("multi", "multi+spotlight")]
    for a, b in comparisons:
        p = paired_attack_success(runs, a, b)
        if p:
            result.setdefault("comparisons", {})[f"{a}_vs_{b}"] = p
    return result


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    p.add_argument("--clean", default=None, help="clean evaluation folder for paired resolution comparison")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    result = analyse(args.folder, args.clean)
    out = Path(args.out) if args.out else Path(args.folder) / "attack_compare.json"
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
