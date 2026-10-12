"""Result figures for the readme, in a light and a dark version.

    python -m responder.figures runs/final --out docs

Bars are the mean over repeated runs; whiskers span the lowest and highest run.
"""
import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from .compare import compare, load_runs  # noqa: E402

themes = {
    # checked with the dataviz palette validator in both modes (3 slots, all pairs)
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
              "base": "#c3c2b7", "series": ["#2a78d6", "#eb6834", "#1baf7a"], "rest": "#c3c2b7"},
    "dark": {"surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
             "base": "#383835", "series": ["#3987e5", "#d95926", "#199e70"], "rest": "#5a5955"},
}
names = {"multi": "Multi-agent responder", "single": "Single agent", "rules": "dbt alone (no AI)"}
kinds = {"schema_drift": "Column renamed\nor retyped", "null_spike": "Values missing",
         "duplicates": "Duplicate rows", "late_data": "Late data"}

plt.rcParams.update({"font.family": ["Segoe UI", "Arial", "DejaVu Sans"], "font.size": 10})


def style(ax, t):
    ax.set_facecolor(t["surface"])
    for side in ("top", "right", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(t["base"])
    ax.tick_params(colors=t["muted"], length=0)
    ax.xaxis.grid(True, color=t["grid"], linewidth=1)
    ax.set_axisbelow(True)
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])


def headline(results, t, path):
    """Resolved rate and root-cause accuracy per system, the responder in the accent, baselines in gray."""
    systems = [s for s in ("multi", "single", "rules") if s in results]
    fig, axes = plt.subplots(1, 2, figsize=(10, 2.9), facecolor=t["surface"], sharey=True)
    for ax, (metric, title) in zip(axes, [("resolved", "Incidents fully resolved"),
                                          ("root_node_correct", "Found where it started")]):
        style(ax, t)
        for i, s in enumerate(systems):
            m = results[s]["overall"][metric]
            y = len(systems) - 1 - i
            color = t["series"][0] if s == "multi" else t["rest"]
            ax.barh(y, 100 * m["mean"], height=0.34, color=color, zorder=2)
            lo, hi = 100 * m["min"], 100 * m["max"]
            if hi > lo:
                ax.plot([lo, hi], [y, y], color=t["ink2"], linewidth=1.2, zorder=3, solid_capstyle="butt")
            ax.text(min(hi, 100) + 1.5, y, f"{100 * m['mean']:.0f}%", va="center", ha="left",
                    color=t["ink"] if s == "multi" else t["ink2"], fontsize=10,
                    fontweight="bold" if s == "multi" else "normal")
        ax.set_yticks(range(len(systems)))
        ax.set_yticklabels([names[s] for s in reversed(systems)], color=t["ink"])
        ax.set_title(title, loc="left", color=t["ink"], fontsize=11, fontweight="bold", pad=10)
    n, k = results["multi"]["incidents"], results["multi"]["runs"]
    fig.text(0.01, 0.01, f"{n} seeded incidents, {k} runs per AI system. Bars are the mean, whiskers the lowest "
             "and highest run. dbt alone gives the same answer every run.",
             color=t["muted"], fontsize=8.5)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(path, dpi=180, facecolor=t["surface"])
    plt.close(fig)


def by_kind(results, t, path):
    """Resolved rate by incident kind, three systems side by side."""
    systems = [s for s in ("multi", "single", "rules") if s in results]
    order = [k for k in kinds if k in results[systems[0]]["by_kind"]]
    fig, ax = plt.subplots(figsize=(10, 4.2), facecolor=t["surface"])
    style(ax, t)
    h = 0.2
    for j, s in enumerate(systems):
        ys = [len(order) - 1 - i + (1 - j) * (h + 0.03) for i in range(len(order))]
        vals = [100 * results[s]["by_kind"][k]["resolved"]["mean"] for k in order]
        ax.barh(ys, vals, height=h, color=t["series"][j], label=names[s], zorder=2)
        for y, v in zip(ys, vals):
            if v == 0:  # an empty bar would look like missing data
                ax.text(0.8, y, "0%", va="center", ha="left", color=t["muted"], fontsize=8.5)
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels([kinds[k] for k in reversed(order)], color=t["ink"])
    ax.set_title("Incidents fully resolved, by kind (mean of runs)", loc="left", color=t["ink"], fontsize=11, fontweight="bold",
                 pad=28)
    leg = ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3, frameon=False, fontsize=9.5,
                    handlelength=1.2, borderaxespad=0.2)
    for text in leg.get_texts():
        text.set_color(t["ink2"])
    fig.tight_layout()
    fig.savefig(path, dpi=180, facecolor=t["surface"])
    plt.close(fig)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    p.add_argument("--out", default="docs")
    args = p.parse_args(argv)
    results = compare(load_runs(args.folder))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for mode, t in themes.items():
        suffix = "" if mode == "light" else "-dark"
        headline(results, t, out / f"results{suffix}.png")
        by_kind(results, t, out / f"by_kind{suffix}.png")
    print("wrote", sorted(str(f) for f in out.glob("*.png")))


if __name__ == "__main__":
    main()
