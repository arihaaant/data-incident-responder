import json

from responder.compare import compare, load_runs, sign_test


def record(i, resolved, action="patch", expected="patch", ok=True):
    return {"id": i, "kind": "duplicates", "split": "dev", "seconds": 1.0,
            "expected": {"action": expected}, "outcome": {"action": action, "tokens": 10},
            "fix_check": {"ok": ok} if action == "patch" else None,
            "score": {"root_node_correct": resolved, "cause_correct": True, "action_correct": True,
                      "resolved": resolved}}


def write(folder, records):
    folder.mkdir(parents=True)
    for r in records:
        (folder / f"{r['id']}.json").write_text(json.dumps(r))


def test_sign_test():
    assert sign_test(0, 0) == 1.0
    assert round(sign_test(5, 0), 4) == 0.0625
    assert sign_test(3, 3) == 1.0


def test_runs_are_grouped_and_compared_per_incident(tmp_path):
    write(tmp_path / "multi-1", [record("inc001", True), record("inc002", True)])
    write(tmp_path / "multi-2", [record("inc001", True), record("inc002", False)])
    write(tmp_path / "multi-3", [record("inc001", True), record("inc002", True)])
    for k in (1, 2, 3):
        write(tmp_path / f"single-{k}", [record("inc001", True),
                                         record("inc002", False, expected="escalate", ok=False)])
    write(tmp_path / "rules", [record("inc001", False, action="escalate"), record("inc002", False, action="escalate")])
    runs = load_runs(tmp_path)
    assert sorted(runs) == ["multi", "rules", "single"] and len(runs["multi"]) == 3

    out = compare(runs)
    assert round(out["multi"]["overall"]["resolved"]["mean"], 3) == round(5 / 6, 3)
    assert out["multi"]["overall"]["resolved"]["min"] == 0.5 and out["multi"]["resolved_in_every_run"] == 1
    assert out["multi_vs_single"]["resolved"] == {"wins": 1, "losses": 0, "p": 1.0}
    assert out["single"]["safety"] == {"patches": 6, "should_have_escalated": 3, "failed_hidden_check": 3}
    assert out["rules"]["safety"]["patches"] == 0
