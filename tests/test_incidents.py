from collections import Counter
from pathlib import Path

from responder.incidents import catalog

project = Path(__file__).resolve().parents[1] / "warehouse"


def test_incident_counts():
    incs = catalog()
    assert len(incs) == 63
    assert len({i.id for i in incs}) == 63
    assert Counter(i.kind for i in incs) == {"schema_drift": 18, "null_spike": 15, "duplicates": 15, "late_data": 15}


def test_holdout_is_three_per_kind():
    held = Counter(i.kind for i in catalog() if i.split == "holdout")
    assert held == {"schema_drift": 4, "null_spike": 3, "duplicates": 3, "late_data": 3}


def test_actions_follow_cause():
    for i in catalog():
        if i.cause == "code_change":
            assert i.action == "patch" and i.edit and not i.sql
        else:
            assert i.sql and not i.edit
        if i.kind in ("null_spike", "late_data") and i.cause == "upstream_data":
            assert i.action == "escalate"


def test_code_edits_target_existing_text():
    for i in catalog():
        if i.edit:
            text = (project / i.edit["file"]).read_text()
            assert text.count(i.edit["old"]) >= 1, i.id
            assert i.root_node == "model." + Path(i.edit["file"]).stem, i.id


def test_scoring_normalizes_node_names_and_accepts_renamed_columns():
    from responder.run import score

    inc = next(i for i in catalog() if i.id == "inc001")
    out = {"root_node": "source.tpch_warehouse.raw.orders", "root_column": inc.also_accept[0],
           "cause": "upstream_data", "action": "patch"}
    s = score(inc, out, {"ok": True})
    assert s["root_node_correct"] and s["root_column_correct"] and s["resolved"]
    assert score(inc, out | {"root_node": "model.tpch_warehouse.stg_orders"}, None)["root_node_correct"] is False


def test_lossy_type_changes_are_escalated_and_lossless_ones_patched():
    drift = [i for i in catalog() if i.kind == "schema_drift" and "arrives as" in i.description]
    lossy = [i for i in drift if i.action == "escalate"]
    assert len(lossy) == 3 and {i.split for i in lossy} == {"dev", "holdout"}
    assert all("gone" in i.description or "unknown" in i.description for i in lossy)
    assert len([i for i in drift if i.action == "patch"]) == 4
    assert [i.id for i in lossy] == ["inc061", "inc062", "inc063"]
