import asyncio
import json

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


def test_provider_parse_errors_are_retried():
    from responder.agents import call_model

    class Rejects:
        calls = 0

        async def ainvoke(self, messages):
            Rejects.calls += 1
            if Rejects.calls < 3:
                raise RuntimeError("Error code: 400 - {'error': {'code': 'tool_use_failed', 'message': 'Parsing failed'}}")
            return "ok"

    assert asyncio.run(call_model(Rejects(), [])) == "ok" and Rejects.calls == 3


def test_no_conclusion_means_escalate():
    from responder.agents import undecided

    for schema in (Diagnosis, Resolution):
        d = undecided(schema)
        assert d.action == "escalate" and d.root_node == "unknown"


def test_root_names_are_canonical():
    from responder.agents import canonical

    assert canonical("raw.lineitem") == "source.raw.lineitem"
    assert canonical("source.tpch_warehouse.raw.orders") == "source.raw.orders"
    assert canonical("model.tpch_warehouse.fct_orders") == "model.fct_orders"
    assert canonical("staging.stg_orders") == "model.stg_orders"
    assert canonical("unknown") == "unknown"


def test_patch_without_sql_is_sent_back():
    from responder.agents import incomplete

    res = Resolution(root_node="source.raw.lineitem", cause="upstream_data", action="patch", evidence="dupes")
    assert incomplete(res)
    assert incomplete(res.model_copy(update={"file": "models/staging/stg_lineitem.sql", "sql": "select 1"})) is None
    assert incomplete(res.model_copy(update={"action": "escalate"})) is None


class Answers:
    """Hands back the given answers in order, one per structured call."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def with_structured_output(self, schema, **kw):
        model = self

        class Runner:
            async def ainvoke(self, messages):
                model.calls += 1
                return {"parsed": model.answers.pop(0), "raw": AIMessage("")}

        return Runner()


class Lineage:
    async def ainvoke(self, args):
        if args["name"] in ("raw.lineitem", "stg_lineitem"):
            return json.dumps({"node": args["name"]})
        return json.dumps({"error": "unknown", "known": ["raw.lineitem", "stg_lineitem"]})


def test_fallback_conclusion_is_checked():
    from responder.agents import conclude

    d = dict(cause="upstream_data", action="patch", evidence="dupes")
    model = Answers(Diagnosis(root_node="raw.lineitems", **d), Diagnosis(root_node="raw.lineitem", **d))
    got, _ = asyncio.run(conclude(model, {"get_lineage": Lineage()}, Diagnosis, []))
    assert got.root_node == "source.raw.lineitem" and model.calls == 2
    model = Answers(Diagnosis(root_node="raw.nope", **d), Diagnosis(root_node="model.nope", **d))
    got, _ = asyncio.run(conclude(model, {"get_lineage": Lineage()}, Diagnosis, []))
    assert got.root_node == "unknown" and got.action == "escalate"


def test_column_profiles_reach_fixer_and_reviewer():
    from responder.agents import column_profiles

    assert "none were run" in column_profiles([])
    steps = [{"tool": "profile_column", "result": '{"lossless": true}'}, {"tool": "run_sql", "result": "x"}]
    assert column_profiles(steps).endswith('{"lossless": true}')
