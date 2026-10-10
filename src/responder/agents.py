"""The responder: triage -> investigate -> fix -> review, as a LangGraph graph.

Agents only see the warehouse through the mcp tools. The fixer proposes a full new version of one model
file; nothing is applied here. The reviewer checks it against hard rules first, then asks the model
whether the fix repairs the cause or just hides it.
"""
import json
import re
from typing import Literal, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

max_tool_output = 2500
max_investigate_steps = 10
max_fix_attempts = 2
token_budget = 80_000


# what each agent hands back

class Triage(BaseModel):
    kind: Literal["schema_drift", "null_spike", "duplicates", "late_data", "other"]
    summary: str = Field(description="one or two sentences on what the alarms show")
    suspects: list[str] = Field(description="models or sources worth checking first")


class Diagnosis(BaseModel):
    root_node: str = Field(description="where it started, as source.raw.<table> or model.<name>")
    root_column: Optional[str] = Field(description="the column at the root, if there is one")
    cause: Literal["upstream_data", "code_change"]
    action: Literal["patch", "escalate"]
    evidence: str = Field(description="the queries and findings that support this, briefly")


class Patch(BaseModel):
    file: str = Field(description="path of the model file to replace, e.g. models/staging/stg_orders.sql")
    sql: str = Field(description="the complete new contents of that file")
    explanation: str


class Review(BaseModel):
    approved: bool
    reasons: str


class State(TypedDict, total=False):
    alarms: str
    triage: dict
    diagnosis: dict
    patch: dict
    review: dict
    feedback: str
    attempts: int
    tokens: int
    steps: list
    outcome: dict


triage_prompt = """You are the first responder for a dbt + DuckDB data warehouse (TPC-H data).
Schemas: raw (data as it lands from upstream), staging (cleaned views, one per raw table), marts (business tables).
Read the failing checks and say what kind of incident this looks like and where to look first."""

investigate_prompt = """You investigate a data incident in a dbt + DuckDB warehouse. Use the tools to find where
the problem started. In SQL, always write tables as schema.table (raw, staging, marts).

How to work:
- Start from the failing check and walk lineage upstream, one step at a time, until you find the first table
  where the problem appears. The node just before that one is fine; the first bad one is where it started.
- At each step compare with the inputs: row counts, distinct keys, and the failing condition itself.
- Missing values downstream can come from rows that a join dropped or failed to match, not only from nulls
  in a column. If a joined table has far fewer rows than its input, look at that model's join.
- Read the SQL of the first bad model and check joins, filters, group bys and column mappings.
- If the raw table already shows the problem, the cause is upstream data. If the raw data looks normal
  but a model's output is wrong, the cause is that model's code.

Decide:
- cause: upstream_data if the raw data itself changed or is wrong; code_change if a model's SQL is wrong.
- action: patch if a change to a dbt model fixes it without hiding or inventing data (for example following
  a renamed or retyped source column, removing rows delivered twice, reverting a bad model change);
  escalate if the data is missing or wrong at the source, because no downstream change can honestly fix that.
- root_node: source.raw.<table> when the cause is upstream data, model.<name> when it is a code change.
Be efficient: you have at most {steps} tool calls."""

fix_prompt = """You fix a data incident by rewriting one dbt model file. Keep everything else in the file the same.
Rules: do not edit checks, tests or yml files; do not filter out rows or fill in values to make a check pass,
unless the rows are exact duplicates of a redelivered batch; keep column names and types the same as before
(the staging models have enforced column type contracts)."""

review_prompt = """You review a proposed fix to a dbt model before it is applied. Approve it only if it repairs the
root cause described in the diagnosis. Reject it if it hides the problem instead: dropping rows that are not
exact duplicates, replacing nulls with made-up values, loosening logic just to satisfy a check, or touching
personal data columns. Be strict and brief."""

ddl = re.compile(r"\b(create|drop|alter|insert|delete|update|truncate|attach|copy|grant|pragma)\b", re.I)
pii = re.compile(r"\b(c_address|c_phone|address|phone)\b", re.I)


def policy_check(patch, original):
    """Hard rules that no model gets a vote on."""
    problems = []
    f = patch["file"].replace("\\", "/")
    if not re.fullmatch(r"models/(staging|marts)/[a-z_]+\.sql", f):
        problems.append(f"only model .sql files under models/ can be changed, not {f}")
    if ddl.search(patch["sql"]):
        problems.append("the new sql contains a statement that writes or changes objects")
    new_pii = set(m.lower() for m in pii.findall(patch["sql"])) - set(m.lower() for m in pii.findall(original or ""))
    if new_pii:
        problems.append(f"the fix starts using personal columns: {sorted(new_pii)}")
    if original is not None and patch["sql"].strip() == original.strip():
        problems.append("the patch does not change anything")
    return problems


def usage(msg):
    u = getattr(msg, "usage_metadata", None) or {}
    return u.get("total_tokens", 0)


def short(text):
    text = text if isinstance(text, str) else json.dumps(text)
    return text if len(text) <= max_tool_output else text[:max_tool_output] + " ...[truncated]"


def tool_text(result):
    # mcp tool results come back as content blocks
    if isinstance(result, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in result)
    return str(result)


def build_graph(llm, tools):
    by_name = {t.name: t for t in tools}
    investigate_tools = [by_name[n] for n in ("run_sql", "get_lineage", "get_model_sql", "list_tables")]

    async def structured(schema, messages):
        out = await llm.with_structured_output(schema, method="function_calling", include_raw=True).ainvoke(messages)
        return out["parsed"], usage(out["raw"])

    async def triage(state):
        result = await by_name["get_test_results"].ainvoke({})
        alarms = short(tool_text(result))
        t, n = await structured(Triage, [SystemMessage(triage_prompt), HumanMessage(f"Failing checks:\n{alarms}")])
        return {"alarms": alarms, "triage": t.model_dump(), "tokens": state.get("tokens", 0) + n,
                "steps": [{"agent": "triage", "output": t.model_dump()}]}

    async def investigate(state):
        # submitting the diagnosis is itself a tool, so the model decides when it has seen enough
        bound = llm.bind_tools(investigate_tools + [Diagnosis])
        intro = f"Failing checks:\n{state['alarms']}\n\nTriage:\n{json.dumps(state['triage'])}"
        messages = [SystemMessage(investigate_prompt.format(steps=max_investigate_steps)),
                    HumanMessage(intro + "\n\nInvestigate, then call Diagnosis with your conclusion.")]
        tokens, steps, diagnosis, notes = state["tokens"], list(state["steps"]), None, []
        for _ in range(max_investigate_steps):
            if tokens > token_budget:
                break
            reply = await bound.ainvoke(messages)
            tokens += usage(reply)
            messages.append(reply)
            if not reply.tool_calls:
                if reply.content:
                    notes.append(f"thought: {reply.content}")
                messages.append(HumanMessage("Call Diagnosis with your conclusion, or keep investigating."))
                continue
            for call in reply.tool_calls:
                if call["name"] == "Diagnosis":
                    diagnosis = Diagnosis(**call["args"])
                    break
                out = short(tool_text(await by_name[call["name"]].ainvoke(call["args"])))
                steps.append({"agent": "investigate", "tool": call["name"], "args": call["args"], "result": out[:500]})
                notes.append(f"{call['name']}({json.dumps(call['args'])}) ->\n{out[:1200]}")
                messages.append(ToolMessage(out, tool_call_id=call["id"]))
            if diagnosis:
                break
        if diagnosis is None:
            # out of steps: hand the findings over as plain notes, with no tool history to continue from
            d, n = await structured(Diagnosis, [
                SystemMessage(investigate_prompt.format(steps=0)),
                HumanMessage(intro + "\n\nWhat the investigation found:\n\n" + "\n\n".join(notes[-12:]) +
                             "\n\nGive your diagnosis now.")])
            diagnosis, tokens = d, tokens + n
        steps.append({"agent": "investigate", "output": diagnosis.model_dump()})
        return {"diagnosis": diagnosis.model_dump(), "tokens": tokens, "steps": steps}

    async def fix(state):
        d = state["diagnosis"]
        target = d["root_node"].split(".")[-1]
        if d["root_node"].startswith("source."):
            # upstream changes are handled in the staging model that reads that source
            lin = json.loads(tool_text(await by_name["get_lineage"].ainvoke({"name": "raw." + target})))
            target = next((n for n in lin.get("downstream", []) if n.startswith("stg_")), target)
        current = json.loads(tool_text(await by_name["get_model_sql"].ainvoke({"name": target})))
        prompt = (f"Diagnosis:\n{json.dumps(d)}\n\nFile {current.get('path')}:\n{current.get('sql')}\n\n"
                  f"Failing checks:\n{state['alarms']}")
        if state.get("feedback"):
            prompt += f"\n\nYour previous attempt was rejected: {state['feedback']}"
        p, n = await structured(Patch, [SystemMessage(fix_prompt), HumanMessage(prompt)])
        patch = p.model_dump() | {"original": current.get("sql")}
        return {"patch": patch, "attempts": state.get("attempts", 0) + 1, "tokens": state["tokens"] + n,
                "steps": state["steps"] + [{"agent": "fix", "output": {k: patch[k] for k in ("file", "explanation")}}]}

    async def review(state):
        p = state["patch"]
        problems = policy_check(p, p.get("original"))
        if problems:
            r, n = Review(approved=False, reasons="; ".join(problems)), 0
        else:
            r, n = await structured(Review, [SystemMessage(review_prompt), HumanMessage(
                f"Diagnosis:\n{json.dumps(state['diagnosis'])}\n\nOriginal file:\n{p['original']}\n\n"
                f"Proposed file ({p['file']}):\n{p['sql']}\n\nExplanation: {p['explanation']}")])
        return {"review": r.model_dump(), "feedback": r.reasons if not r.approved else "",
                "tokens": state["tokens"] + n, "steps": state["steps"] + [{"agent": "review", "output": r.model_dump()}]}

    def report(state):
        d = state.get("diagnosis", {})
        approved = state.get("review", {}).get("approved", False)
        action = "patch" if d.get("action") == "patch" and approved else "escalate"
        return {"outcome": {"action": action, "root_node": d.get("root_node"), "root_column": d.get("root_column"),
                            "cause": d.get("cause"), "patch": state.get("patch") if action == "patch" else None,
                            "escalated_after_failed_fix": d.get("action") == "patch" and not approved,
                            "tokens": state.get("tokens", 0)}}

    def after_investigate(state):
        return "fix" if state["diagnosis"]["action"] == "patch" and state["tokens"] <= token_budget else "report"

    def after_review(state):
        if state["review"]["approved"] or state["attempts"] >= max_fix_attempts:
            return "report"
        return "fix"

    g = StateGraph(State)
    for name, fn in [("triage", triage), ("investigate", investigate), ("fix", fix), ("review", review),
                     ("report", report)]:
        g.add_node(name, fn)
    g.set_entry_point("triage")
    g.add_edge("triage", "investigate")
    g.add_conditional_edges("investigate", after_investigate, ["fix", "report"])
    g.add_edge("fix", "review")
    g.add_conditional_edges("review", after_review, ["fix", "report"])
    g.add_edge("report", END)
    return g.compile()


# baseline: one agent with the same tools and budget, no separate fixer and no reviewer

class Resolution(BaseModel):
    root_node: str = Field(description="where it started, as source.raw.<table> or model.<name>")
    root_column: Optional[str]
    cause: Literal["upstream_data", "code_change"]
    action: Literal["patch", "escalate"]
    file: Optional[str] = Field(description="if patching: the model file to replace, e.g. models/staging/stg_orders.sql")
    sql: Optional[str] = Field(description="if patching: the complete new contents of that file")
    evidence: str


def build_single_agent(llm, tools):
    by_name = {t.name: t for t in tools}
    usable = [by_name[n] for n in ("get_test_results", "run_sql", "get_lineage", "get_model_sql", "list_tables")]
    bound = llm.bind_tools(usable + [Resolution])
    prompt = investigate_prompt.format(steps=max_investigate_steps + 2) + "\n\nIf you patch: " + fix_prompt

    async def solve(state):
        messages = [SystemMessage(prompt), HumanMessage(
            "A dbt build just failed. Find out why, then call Resolution. If the action is patch, include the "
            "complete new contents of the one model file you change.")]
        tokens, steps, res, notes = 0, [], None, []
        for _ in range(max_investigate_steps + 2):
            if tokens > token_budget:
                break
            reply = await bound.ainvoke(messages)
            tokens += usage(reply)
            messages.append(reply)
            if not reply.tool_calls:
                messages.append(HumanMessage("Call Resolution with your conclusion, or keep investigating."))
                continue
            for call in reply.tool_calls:
                if call["name"] == "Resolution":
                    res = Resolution(**call["args"])
                    break
                out = short(tool_text(await by_name[call["name"]].ainvoke(call["args"])))
                steps.append({"agent": "single", "tool": call["name"], "args": call["args"], "result": out[:500]})
                notes.append(f"{call['name']}({json.dumps(call['args'])}) ->\n{out[:1200]}")
                messages.append(ToolMessage(out, tool_call_id=call["id"]))
            if res:
                break
        if res is None:
            out = await llm.with_structured_output(Resolution, method="function_calling", include_raw=True).ainvoke(
                [SystemMessage(prompt), HumanMessage("What the investigation found:\n\n" + "\n\n".join(notes[-12:]) +
                                                     "\n\nGive your resolution now.")])
            res, tokens = out["parsed"], tokens + usage(out["raw"])
        patch = None
        if res.action == "patch" and res.file and res.sql:
            patch = {"file": res.file, "sql": res.sql, "explanation": res.evidence}
        steps.append({"agent": "single", "output": res.model_dump(exclude={"sql"})})
        return {"steps": steps, "outcome": {
            "action": "patch" if patch else "escalate", "root_node": res.root_node, "root_column": res.root_column,
            "cause": res.cause, "patch": patch, "escalated_after_failed_fix": False, "tokens": tokens}}

    g = StateGraph(State)
    g.add_node("solve", solve)
    g.set_entry_point("solve")
    g.add_edge("solve", END)
    return g.compile()
