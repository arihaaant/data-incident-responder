"""Build the incident kit: a clean baseline, then every incident checked to really break something,
and every patchable incident checked to be fixable by a reference patch.

    python -m responder.kit --out kit
"""
import argparse
import json
import re
import time
from pathlib import Path

from . import workspace
from .incidents import attack_catalog, catalog, retypes
from .warehouse import build_raw

project_src = Path(__file__).resolve().parents[2] / "warehouse"


def reference_fix(incident, project):
    # a known-good patch, only used to prove each patch incident is solvable and that check_fix accepts it
    project = Path(project)
    if incident.edit:
        path = project / incident.edit["file"]
        path.write_text(path.read_text().replace(incident.edit["new"], incident.edit["old"], 1))
        return
    table = incident.root_node.split(".")[-1]
    staging = project / "models" / "staging" / f"{workspace_staging[table]}.sql"
    text = staging.read_text()
    if incident.kind == "schema_drift" and "renamed" in incident.description:
        new = re.search(r"renamed to (\w+)", incident.description).group(1)
        text = re.sub(rf"\b{incident.root_column}\b", new, text)
    elif incident.kind == "schema_drift":
        fix = next(f for t, c, _, _, f in retypes if t == table and c == incident.root_column)
        text = re.sub(rf"\b{incident.root_column}\b", fix.format(c=incident.root_column), text)
    elif incident.kind == "duplicates":
        src = f"{{{{ source('raw', '{table}') }}}}"
        text = text.replace(src, f"(select distinct * from {src})")
    staging.write_text(text)


workspace_staging = {"orders": "stg_orders", "customer": "stg_customers", "lineitem": "stg_lineitem",
                     "part": "stg_parts", "nation": "stg_nations"}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="kit")
    p.add_argument("--only", default=None, help="comma separated incident ids")
    args = p.parse_args(argv)
    out = Path(args.out)
    out.mkdir(exist_ok=True)

    raw_db = build_raw(out / "source.duckdb")
    clean = workspace.copy_project(project_src, out / "work" / "clean")
    (clean / "warehouse.duckdb").write_bytes(raw_db.read_bytes())
    res = workspace.build(clean)
    assert res["ok"], f"clean warehouse does not pass: {res}"
    golden = {"marts": workspace.fingerprints(clean / "warehouse.duckdb"),
              "signature": workspace.test_signature(clean)}
    (out / "golden.json").write_text(json.dumps(golden, indent=2))

    incidents = catalog()
    if args.only:
        incidents = [i for i in incidents if i.id in args.only.split(",")]
    rows = []
    for inc in incidents:
        t0 = time.time()
        work = workspace.prepare(inc, project_src, raw_db, out / "work" / inc.id)
        broken = workspace.build(work)
        row = inc.as_dict() | {"failed": broken["failed"], "errored": broken["errored"],
                               "breaks_something": not broken["ok"]}
        if inc.action == "patch":
            reference_fix(inc, work)
            row["reference_fix"] = workspace.check_fix(work, golden)
        rows.append(row)
        status = "ok" if row["breaks_something"] and row.get("reference_fix", {"ok": True})["ok"] else "PROBLEM"
        print(f"{inc.id} {inc.kind:12s} {inc.cause:13s} {inc.action:8s} "
              f"fails={len(broken['failed'])} errors={len(broken['errored'])} {status} ({time.time() - t0:.0f}s)")
    if args.only and (out / "catalog.json").exists():
        # validating a few incidents updates their rows and keeps the rest
        kept = {r["id"]: r for r in json.loads((out / "catalog.json").read_text())}
        kept.update({r["id"]: r for r in rows})
        rows = [kept[k] for k in sorted(kept)]
    (out / "catalog.json").write_text(json.dumps(rows, indent=2))


def validate_attacks(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--kit", default="kit")
    p.add_argument("--only", default=None)
    args = p.parse_args(argv)
    kit = Path(args.kit)
    golden = json.loads((kit / "golden.json").read_text())
    raw_db = kit / "source.duckdb"
    attacks = attack_catalog(kit / "attacks.json")
    if args.only:
        attacks = [a for a in attacks if a.id in args.only.split(",")]
    ok, fail = 0, 0
    for inc in attacks:
        t0 = time.time()
        work = workspace.prepare(inc, project_src, raw_db, kit / "work" / inc.id)
        broken = workspace.build(work)
        if not broken["ok"]:
            status = "ok"
        else:
            status = "PROBLEM: attack did not break anything"
            fail += 1
        if inc.action == "patch" and broken["ok"] is False:
            reference_fix(inc, work)
            check = workspace.check_fix(work, golden)
            if not check["ok"]:
                status = f"PROBLEM: reference fix failed: {check}"
                fail += 1
        if status == "ok":
            ok += 1
        print(f"{inc.id} base={inc.attack['base_incident_id']} {inc.attack['placement']:13s} "
              f"{inc.attack['goal']:14s} {status} ({time.time() - t0:.0f}s)")
    print(f"\n{ok} ok, {fail} problems out of {len(attacks)}")


if __name__ == "__main__":
    import sys
    if "--validate-attacks" in sys.argv:
        sys.argv.remove("--validate-attacks")
        validate_attacks()
    else:
        main()
