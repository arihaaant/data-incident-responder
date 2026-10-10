import asyncio

from langchain_core.messages import AIMessage

from responder.agents import Diagnosis, Resolution, ask_structured, parse_call, policy_check


class FlakyModel:
    """Sends back nothing usable on the first try, then a valid answer."""

    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def with_structured_output(self, schema, **kw):
        model = self

        class Runner:
            async def ainvoke(self, messages):
                model.calls += 1
                parsed = None if model.calls == 1 else model.answer
                return {"parsed": parsed, "raw": AIMessage(""), "parsing_error": "bad json"}

        return Runner()


def test_escalation_needs_no_patch_fields():
    res, problem = parse_call(Resolution, {"args": {"root_node": "source.raw.orders", "cause": "upstream_data",
                                                    "action": "escalate", "evidence": "last days missing"}})
    assert problem is None and res.file is None and res.root_column is None


def test_invalid_submission_is_explained_not_raised():
    d, problem = parse_call(Diagnosis, {"args": {"root_node": "model.stg_orders", "action": "patch"}})
    assert d is None and "cause" in problem and "evidence" in problem


def test_structured_answer_is_retried():
    good = Diagnosis(root_node="model.stg_orders", cause="code_change", action="patch", evidence="bad filter")
    model = FlakyModel(good)
    got, _ = asyncio.run(ask_structured(model, Diagnosis, []))
    assert got == good and model.calls == 2


def test_policy_blocks_checks_ddl_and_personal_columns():
    original = "select c_custkey as customer_key from {{ source('raw', 'customer') }}"
    assert policy_check({"file": "models/staging/schema.yml", "sql": original + " x"}, original)
    assert policy_check({"file": "models/staging/stg_customers.sql", "sql": "drop table raw.customer"}, original)
    assert policy_check({"file": "models/staging/stg_customers.sql",
                         "sql": original.replace("c_custkey as customer_key", "c_custkey, c_phone")}, original)
    assert policy_check({"file": "models/staging/stg_customers.sql", "sql": original}, original)
    ok = original.replace("from", "from (select distinct * from").replace("}}", "}})")
    assert policy_check({"file": "models/staging/stg_customers.sql", "sql": ok}, original) == []


def test_tool_names_are_cleaned():
    from responder.agents import clean_name

    assert clean_name("cfunctions.Resolution") == "Resolution"
    assert clean_name("functions.run_sql") == "run_sql"
    assert clean_name("run_sql") == "run_sql"
