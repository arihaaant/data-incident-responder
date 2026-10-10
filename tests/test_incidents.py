from collections import Counter
from pathlib import Path

from responder.incidents import catalog

project = Path(__file__).resolve().parents[1] / "warehouse"


def test_sixty_incidents_fifteen_per_kind():
    incs = catalog()
    assert len(incs) == 60
    assert len({i.id for i in incs}) == 60
    assert set(Counter(i.kind for i in incs).values()) == {15}


def test_holdout_is_three_per_kind():
    held = Counter(i.kind for i in catalog() if i.split == "holdout")
    assert held == {"schema_drift": 3, "null_spike": 3, "duplicates": 3, "late_data": 3}


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
