from responder.incident_report import render

alarms = [{"node": "not_null_stg_customers_market_segment", "status": "fail", "failing_rows": 375,
           "message": "Got 375 results, configured to fail if != 0"}]
steps = [{"agent": "investigate", "tool": "run_sql",
          "args": {"query": "select count(*) from raw.customer where c_mktsegment is null"}, "result": "375"},
         {"agent": "investigate", "output": {"evidence": "375 customers arrive with no market segment."}}]


def test_escalation_report():
    out = {"action": "escalate", "root_node": "source.raw.customer", "root_column": "c_mktsegment",
           "cause": "upstream_data", "patch": None, "escalated_after_failed_fix": False}
    md = render("inc016", alarms, out, steps)
    assert "Escalated to the owner of raw.customer" in md
    assert "375 rows" in md and "375 customers arrive" in md
    assert "select count(*) from raw.customer" in md
    assert "No change was made" in md and "- [ ] Confirm the problem with the owner of `raw.customer`" in md


def test_fix_report_has_diff_test_result_and_checklist():
    patch = {"file": "models/staging/stg_orders.sql", "explanation": "Follow the renamed column.",
             "original": "select\n    o_orderpriority as priority\nfrom x\n",
             "sql": "select\n    o_order_priority as priority\nfrom x\n", "dry_run": {"passes": True}}
    out = {"action": "patch", "root_node": "source.raw.orders", "root_column": "o_orderpriority",
           "cause": "upstream_data", "patch": patch}
    md = render("inc001", alarms, out, steps + [{"agent": "review", "output": {"approved": True, "reasons": "ok"}}])
    assert "Fix proposed. Needs a human review" in md
    assert "-    o_orderpriority as priority" in md and "+    o_order_priority as priority" in md
    assert "every check passed" in md and "Reviewer:** approved" in md
    assert "- [ ] The owner of `raw.orders` confirms the upstream change is intended." in md


def test_failed_fix_is_reported_as_escalation():
    out = {"action": "escalate", "root_node": "model.dim_customers", "cause": "code_change", "patch": None,
           "escalated_after_failed_fix": True}
    md = render("inc025", alarms, out, [{"agent": "review", "output": {"approved": False, "reasons": "drops rows"}}])
    assert "A fix was attempted but did not pass review" in md and "drops rows" in md
