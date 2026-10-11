"""A human-readable report for every incident, fixed or escalated.

Nothing the responder does is final: a fix is a proposal for someone to review and merge, and an escalation is
a ticket for the owner of the source. This report is what that person reads. It is built from what the agents
recorded, so it costs no extra model calls.
"""
import difflib
import json
from pathlib import Path


def downstream(project, node):
    """Every model that depends on a node, from dbt's manifest."""
    path = Path(project) / "target" / "manifest.json"
    if not path.exists() or not node:
        return []
    m = json.loads(path.read_text())
    kind, _, rest = node.partition(".")
    name = rest.split(".")[-1]
    start = [i for i in list(m["nodes"]) + list(m["sources"])
             if i.startswith(kind + ".") and i.split(".")[-1] == name]
    seen, todo = set(), list(start)
    while todo:
        for child in m["child_map"].get(todo.pop(), []):
            if child.startswith("model.") and child not in seen:
                seen.add(child)
                todo.append(child)
    return sorted(c.split(".")[-1] for c in seen)


def evidence(steps):
    for s in reversed(steps):
        out = s.get("output")
        if isinstance(out, dict) and out.get("evidence"):
            return out["evidence"]
    return None


def profiles(steps):
    out = []
    for s in steps:
        if s.get("tool") == "profile_column":
            try:
                out.append(json.loads(s["result"]))
            except (ValueError, TypeError):
                pass
    return out


def queries(steps):
    return [s["args"]["query"] for s in steps if s.get("tool") == "run_sql" and "query" in s.get("args", {})]


def render(incident_id, alarms, outcome, steps, project=None):
    action, root = outcome.get("action"), outcome.get("root_node") or "unknown"
    patch = outcome.get("patch")
    review = next((s["output"] for s in reversed(steps) if s.get("agent") == "review"), None)
    affected = downstream(project, root) if project else []
    source = root.split(".")[-1] if root.startswith("source.") else None

    if action == "patch":
        status = "Fix proposed. Needs a human review before it is merged."
    elif outcome.get("escalated_after_failed_fix"):
        status = "Escalated. A fix was attempted but did not pass review, so nothing was changed."
    else:
        status = f"Escalated to the owner of {'raw.' + source if source else root}. Nothing was changed."

    lines = [f"# Incident {incident_id}", "", f"**Status:** {status}", "", "## What broke", ""]
    for a in alarms or []:
        first = (a.get("message") or "").strip().splitlines()
        detail = f" ({a['failing_rows']} rows)" if a.get("failing_rows") else ""
        lines.append(f"- `{a['node']}` {a['status']}{detail}" + (f": {first[0][:200]}" if first else ""))
    if not alarms:
        lines.append("- no alarm details were recorded")

    lines += ["", "## Diagnosis", "",
              f"- **Started at:** `{root}`" + (f", column `{outcome['root_column']}`" if outcome.get("root_column") else ""),
              f"- **Cause:** {'data arriving from upstream' if outcome.get('cause') == 'upstream_data' else 'a change in a model'}"]
    if affected:
        lines.append(f"- **Downstream models affected:** {', '.join(f'`{a}`' for a in affected)}")
    if evidence(steps):
        lines += ["", "**Evidence**", "", evidence(steps)]
    for p in profiles(steps):
        verdict = "converts without loss" if p.get("lossless") else "would lose data: " + "; ".join(p.get("issues", []))
        lines += ["", f"**Column profile** `{p.get('column')}` ({p.get('current_type')} to {p.get('target_type')}): "
                      f"{verdict}"]
    qs = queries(steps)
    if qs:
        lines += ["", f"<details><summary>{len(qs)} queries run during the investigation</summary>", ""]
        lines += [f"```sql\n{q.strip()}\n```" for q in qs]
        lines += ["", "</details>"]

    lines += ["", "## What was done", ""]
    if action == "patch" and patch:
        lines.append(f"Proposed a change to `{patch['file']}`. {patch.get('explanation', '')}".strip())
        if patch.get("original") is not None:
            diff = difflib.unified_diff(patch["original"].splitlines(), patch["sql"].splitlines(),
                                        f"a/{patch['file']}", f"b/{patch['file']}", lineterm="")
            lines += ["", "```diff", *diff, "```"]
        dry = patch.get("dry_run") or {}
        if dry:
            lines.append("")
            lines.append("- **Tested in a copy of the warehouse:** " +
                         ("every check passed." if dry.get("passes") else "it did not pass every check."))
        if review:
            lines.append(f"- **Reviewer:** {'approved' if review.get('approved') else 'rejected'}. {review.get('reasons', '')}")
    else:
        if review and outcome.get("escalated_after_failed_fix"):
            lines.append(f"A fix was proposed and rejected: {review.get('reasons', '')}")
            lines.append("")
        lines.append("No change was made to the warehouse. The data has to be corrected where it comes from.")

    lines += ["", "## Please verify", ""]
    if action == "patch":
        lines += ["- [ ] The diff changes only what the diagnosis describes.",
                  "- [ ] No rows are dropped and no values are filled in, except exact duplicates.",
                  "- [ ] `dbt build` passes on your branch before merging."]
        if source:
            lines.append(f"- [ ] The owner of `raw.{source}` confirms the upstream change is intended.")
        if affected:
            lines.append(f"- [ ] Spot-check the affected models: {', '.join(affected)}.")
    else:
        lines += [f"- [ ] Confirm the problem with the owner of `{'raw.' + source if source else root}`.",
                  "- [ ] Re-run `dbt build` once the source is corrected; the alarms should clear."]
        if affected:
            lines.append(f"- [ ] Until then, treat these models as unreliable: {', '.join(affected)}.")
    return "\n".join(lines) + "\n"
