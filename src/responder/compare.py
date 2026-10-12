"""Results across repeated runs of the same incidents, and the systems compared incident by incident.

    python -m responder.compare runs/final

Expects one folder per run, named <system>-<k> (multi-1, single-2, ...), and a single rules folder since the
rules baseline gives the same answer every time.
"""
import argparse
import json
import math
from pathlib import Path

from .report import load, metrics


def load_runs(folder):
    runs = {}
    for d in sorted(Path(folder).iterdir()):
        if d.is_dir() and any(d.glob("inc*.json")):
            system = d.name.rsplit("-", 1)[0] if d.name[-1].isdigit() else d.name
            runs.setdefault(system, []).append({r["id"]: r for r in load(d)})
    return runs


def rate(records, metric):
    return sum(r["score"][metric] for r in records) / len(records) if records else 0


def spread(runs, metric, keep=lambda r: True):
    """Mean, lowest and highest rate over the runs."""
    rates = [rate([r for r in run.values() if keep(r)], metric) for run in runs]
    return {"mean": sum(rates) / len(rates), "min": min(rates), "max": max(rates), "runs": rates}


def sign_test(wins, losses):
    """Two-sided exact sign test on the incidents where the two systems disagree."""
    n, k = wins + losses, min(wins, losses)
    return 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, j) for j in range(k + 1)) / 2 ** n)


def majority(runs, metric):
    return {i: 2 * sum(run[i]["score"][metric] for run in runs) > len(runs) for i in runs[0]}


def paired(a, b, metric):
    ma, mb = majority(a, metric), majority(b, metric)
    wins = sum(ma[i] and not mb[i] for i in ma)
    losses = sum(mb[i] and not ma[i] for i in ma)
    return {"wins": wins, "losses": losses, "p": sign_test(wins, losses)}


def safety(runs):
    """Fixes proposed, and the ones that should not have been: the incident needed the source owner, or the fix
    failed the hidden check."""
    patches = [r for run in runs for r in run.values() if r["outcome"]["action"] == "patch"]
    return {"patches": len(patches),
            "should_have_escalated": sum(r["expected"]["action"] == "escalate" for r in patches),
            "failed_hidden_check": sum(not (r.get("fix_check") or {}).get("ok") for r in patches)}


def compare(runs):
    out = {}
    for system, rs in runs.items():
        records = list(rs[0].values())
        out[system] = {
            "runs": len(rs), "incidents": len(records),
            "overall": {m: spread(rs, m) for m in metrics},
            "by_split": {s: {m: spread(rs, m, lambda r, s=s: r["split"] == s) for m in metrics}
                         for s in sorted({r["split"] for r in records})},
            "by_kind": {k: {m: spread(rs, m, lambda r, k=k: r["kind"] == k) for m in metrics}
                        for k in sorted({r["kind"] for r in records})},
            "resolved_in_every_run": sum(all(run[i]["score"]["resolved"] for run in rs) for i in rs[0]),
            "tokens_per_incident": sum(r["outcome"]["tokens"] for run in rs for r in run.values()) / (len(rs) * len(records)),
            "seconds_per_incident": sum(r["seconds"] for run in rs for r in run.values()) / (len(rs) * len(records)),
            "safety": safety(rs),
        }
    if "multi" in runs and "single" in runs:
        out["multi_vs_single"] = {m: paired(runs["multi"], runs["single"], m) for m in metrics}
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    result = compare(load_runs(args.folder))
    Path(args.out or Path(args.folder) / "compare.json").write_text(json.dumps(result, indent=2))

    pct = lambda s: f"{100 * s['mean']:5.1f}% ({100 * s['min']:.0f}-{100 * s['max']:.0f})"
    systems = [s for s in ("multi", "single", "rules") if s in result]
    print(f"{'':12s}" + "".join(f"{s + ' x' + str(result[s]['runs']):>22s}" for s in systems))
    for m in metrics:
        print(f"{m.replace('_correct', ''):12s}" + "".join(f"{pct(result[s]['overall'][m]):>22s}" for s in systems))
    for split in result[systems[0]]["by_split"]:
        print(f"{split + ' res.':12s}" + "".join(f"{pct(result[s]['by_split'][split]['resolved']):>22s}"
                                                  for s in systems))
    print(f"{'every run':12s}" + "".join(f"{result[s]['resolved_in_every_run']:>22d}" for s in systems))
    print(f"{'tokens':12s}" + "".join(f"{result[s]['tokens_per_incident']:>22.0f}" for s in systems))
    for s in systems:
        f = result[s]["safety"]
        print(f"{s}: {f['patches']} fixes proposed, {f['should_have_escalated']} on incidents that needed the source "
              f"owner, {f['failed_hidden_check']} failing the hidden check")
    for m, c in result.get("multi_vs_single", {}).items():
        print(f"multi vs single on {m} (majority of runs, per incident): {c['wins']} to {c['losses']}, "
              f"sign test p = {c['p']:.3f}")


if __name__ == "__main__":
    main()
