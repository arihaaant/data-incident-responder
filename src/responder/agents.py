"""The responder: triage -> investigate -> fix -> review, as a LangGraph graph.

Agents only see the warehouse through the mcp tools. The fixer proposes a full new version of one model
file; nothing is applied here. The reviewer checks it against hard rules first, then asks the model
whether the fix repairs the cause or just hides it.
"""
import json
import re
from pathlib import Path
from typing import Literal, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field, ValidationError

max_tool_output = 2500
max_investigate_steps = 10
max_fix_attempts = 2
max_dry_runs = 3
token_budget = 80_000


# what each agent hands back

class Triage(BaseModel):
    kind: Literal["schema_drift", "null_spike", "duplicates", "late_data", "other"]
    summary: str = Field(description="one or two sentences on what the alarms show")
    suspects: list[str] = Field(description="models or sources worth checking first")


class Diagnosis(BaseModel):
    root_node: str = Field(description="where it started, as source.raw.<table> or model.<name>")
    root_column: Optional[str] = Field(default=None, description="the column at the root, if there is one")
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

Team policy on schema changes:
- A renamed column carries the same data, so the staging model follows the new name (patch).
- A type or format change is patched only if it is lossless: every value converts back to the contracted type
  and nothing is lost (no values that fail to parse, no precision or digits missing compared with what the
  column used to hold). Check it with profile_column(table, column), which reads the contracted type from the
  staging contract. Patch only if the profile says lossless, and use the conversion it gives.
- If anything was lost (rounded, truncated or unparseable values), the staging model can't get it back:
  escalate to the source owner.

Team policy on duplicates: if duplicated rows are identical in every column, the batch was delivered twice and
the fix is to deduplicate in the staging model (patch). If rows share a key but differ in any column, that is a
data conflict for the source owner (escalate). Check which one it is with a query before deciding.

Decide:
- cause: upstream_data if the raw data itself changed or is wrong; code_change if a model's SQL is wrong.
- action: patch if a change to a dbt model fixes it without hiding or inventing data (for example following
  a renamed or retyped source column, removing rows delivered twice, reverting a bad model change);
  escalate if the data is missing or wrong at the source, because no downstream change can honestly fix that.
- root_node: where the problem started, which is not always the file you would edit to fix it. If a source
  table changed (a column renamed, retyped, missing, duplicated or late) the root is source.raw.<table>, even
  though the fix goes in a staging model. Only when a model's own SQL is wrong is the root model.<name>.
Be efficient: you have at most {steps} tool calls."""

fix_prompt = """You fix a data incident by rewriting one dbt model file. Keep everything else in the file the same.
Rules: do not edit checks, tests or yml files; do not filter out rows or fill in values to make a check pass,
unless the rows are exact duplicates of a redelivered batch; keep column names and types exactly as before
(the staging models have enforced column type contracts, e.g. decimal(15,2) has to stay decimal(15,2)).
For a source type or format change, use the conversion from the column profile below.
Your patch is built in a copy of the warehouse before anyone reviews it; if that build fails you will see why."""

review_prompt = """You review a proposed fix to a dbt model before it is applied. Approve it only if it repairs the
root cause described in the diagnosis. Reject it if it hides the problem instead: dropping rows that are not
exact duplicates, replacing nulls with made-up values, loosening logic just to satisfy a check, or touching
personal data columns. When a source column was renamed, changed type or changed format, updating the staging
model to read the new column, cast it or parse the new format is the correct fix, not masking: adapting to the
source is what the staging layer is for. For a type or format change, approve only if a column profile below
says the conversion is lossless and the patch uses that conversion; if no profile was run or it lists issues,
reject. The patch has already been built in a copy of the warehouse and passed every check. Be strict and brief."""

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


provider_parse_errors = ("tool_use_failed", "Parsing failed", "tool call validation", "failed_generation")


async def call_model(runnable, messages, tries=3):
    """Some providers reject a malformed generation themselves (a 400) before we see it; those are retried."""
    for attempt in range(tries):
        try:
            return await runnable.ainvoke(messages)
        except Exception as e:
            if attempt == tries - 1 or not any(m in str(e) for m in provider_parse_errors):
                raise


async def ask_structured(llm, schema, messages, tries=3):
    messages = list(messages)
    tokens = 0
    for _ in range(tries):
        out = await call_model(llm.with_structured_output(schema, method="function_calling", include_raw=True),
                               messages)
        tokens += usage(out["raw"])
        if out["parsed"] is not None:
            return out["parsed"], tokens
        messages.append(HumanMessage(f"That was not a valid {schema.__name__} call ({str(out.get('parsing_error'))[:300]}). "
                                     f"Call {schema.__name__} again with every required field."))
    raise RuntimeError(f"no valid {schema.__name__} after {tries} tries")


def undecided(schema):
    # no usable conclusion came back, so nothing is changed and a person takes over
    return schema(root_node="unknown", cause="upstream_data", action="escalate",
                  evidence="The investigation did not reach a conclusion; handing over for manual review.")


def parse_call(schema, call):
    """A submitted tool call as the schema, or the validation problem to send back to the model."""
    try:
        return schema(**call["args"]), None
    except ValidationError as e:
        return None, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5])


def canonical(root_node):
    """source.raw.<table> or model.<name>, however the model wrote it (raw.orders, model.project.stg_orders)."""
    parts = (root_node or "").strip().lower().split(".")
    if len(parts) > 1 and (parts[0] == "source" or (parts[0] == "raw" and len(parts) == 2)):
        return "source.raw." + parts[-1]
    if len(parts) > 1 and (parts[0] == "model" or (parts[0] in ("staging", "marts") and len(parts) == 2)):
        return "model." + parts[-1]
    return root_node


def incomplete(resolution):
    if resolution.action == "patch" and not (resolution.file and resolution.sql):
        return "a patch needs file and sql: the model file's path and its complete new contents"
    return None


async def conclude(llm, by_name, schema, messages):
    """The conclusion once the tool budget is spent, held to the same check as one submitted during the run."""
    messages, tokens = list(messages), 0
    for _ in range(2):
        try:
            out, n = await ask_structured(llm, schema, messages)
        except RuntimeError:
            break
        tokens += n
        out.root_node = canonical(out.root_node)
        problem = await unknown_node(by_name, out.root_node)
        if not problem:
            return out, tokens
        messages.append(HumanMessage(f"That {schema.__name__} was rejected: {problem}. Give it again."))
    return undecided(schema), tokens


async def unknown_node(by_name, root_node):
    """None if the diagnosis names a real model or source, otherwise a message listing the real ones."""
    kind, _, rest = (root_node or "").partition(".")
    if kind == "source":
        name = "raw." + rest.split(".")[-1]
    elif kind == "model":
        name = rest.split(".")[-1]
    else:
        return f"root_node has to look like source.raw.<table> or model.<name>, not {root_node}"
    lineage = json.loads(tool_text(await by_name["get_lineage"].ainvoke({"name": name})))
    if "error" not in lineage:
        return None
    return (f"{root_node} does not exist in this project. Sources are source.raw.<table>, models are "
            f"model.<name>, using one of: {', '.join(lineage.get('known', []))}")


def column_profiles(steps):
    found = [s["result"] for s in steps if s.get("tool") == "profile_column"]
    if not found:
        return "Column profiles: none were run during the investigation."
    return "Column profiles from the investigation:\n" + "\n".join(found)


def clean_name(name):
    # gpt-oss sometimes leaks its channel prefix into the tool name, e.g. "functions.run_sql"
    return name.split(".")[-1].strip()


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
    investigate_tools = [by_name[n] for n in ("run_sql", "get_lineage", "get_model_sql", "list_tables",
                                              "profile_column")]

    async def structured(schema, messages):
        return await ask_structured(llm, schema, messages)

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
            reply = await call_model(bound, messages)
            tokens += usage(reply)
            messages.append(reply)
            if not reply.tool_calls:
                if reply.content:
                    notes.append(f"thought: {reply.content}")
                messages.append(HumanMessage("Call Diagnosis with your conclusion, or keep investigating."))
                continue
            for call in reply.tool_calls:
                call = call | {"name": clean_name(call["name"])}
                if call["name"] == "Diagnosis":
                    diagnosis, problem = parse_call(Diagnosis, call)
                    if not problem:
                        diagnosis.root_node = canonical(diagnosis.root_node)
                        problem = await unknown_node(by_name, diagnosis.root_node)
                        diagnosis = None if problem else diagnosis
                    if problem:
                        messages.append(ToolMessage(f"Diagnosis rejected, fix and resend: {problem}",
                                                    tool_call_id=call["id"]))
                        continue
                    break
                if call["name"] not in by_name:
                    messages.append(ToolMessage(f"unknown tool {call['name']}", tool_call_id=call["id"]))
                    continue
                out = short(tool_text(await by_name[call["name"]].ainvoke(call["args"])))
                steps.append({"agent": "investigate", "tool": call["name"], "args": call["args"],
                              "result": out if call["name"] == "profile_column" else out[:500]})
                notes.append(f"{call['name']}({json.dumps(call['args'])}) ->\n{out[:1200]}")
                messages.append(ToolMessage(out, tool_call_id=call["id"]))
            if diagnosis:
                break
        if diagnosis is None:
            # out of steps: hand the findings over as plain notes, with no tool history to continue from
            diagnosis, n = await conclude(llm, by_name, Diagnosis, [
                SystemMessage(investigate_prompt.format(steps=0)),
                HumanMessage(intro + "\n\nWhat the investigation found:\n\n" + "\n\n".join(notes[-12:]) +
                             "\n\nGive your diagnosis now.")])
            tokens += n
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
        base = (f"Diagnosis:\n{json.dumps(d)}\n\nFile {current.get('path')}:\n{current.get('sql')}\n\n"
                f"Failing checks:\n{state['alarms']}\n\n{column_profiles(state['steps'])}")
        feedback, tokens, steps = state.get("feedback"), state["tokens"], list(state["steps"])
        # write a patch, build it in a throwaway copy, and try again with the errors if it doesn't pass
        for _ in range(max_dry_runs):
            prompt = base + (f"\n\nYour previous attempt did not work: {feedback}" if feedback else "")
            p, n = await structured(Patch, [SystemMessage(fix_prompt), HumanMessage(prompt)])
            tokens += n
            dry = json.loads(tool_text(await by_name["try_patch"].ainvoke({"file": p.file, "sql": p.sql})))
            steps.append({"agent": "fix", "output": {"file": p.file, "explanation": p.explanation,
                                                     "dry_run_passes": dry.get("passes", False)}})
            if dry.get("passes"):
                break
            feedback = "it was built in a copy of the warehouse and failed: " + short(json.dumps(dry))
        patch = p.model_dump() | {"dry_run": dry}
        return {"patch": patch, "attempts": state.get("attempts", 0) + 1, "tokens": tokens, "steps": steps}

    async def review(state):
        p = state["patch"]
        # compare against the file this patch actually replaces, not the one the diagnosis pointed at
        stem = Path(p["file"].replace("\\", "/")).stem
        current = json.loads(tool_text(await by_name["get_model_sql"].ainvoke({"name": stem})))
        same_file = current.get("path") == p["file"].replace("\\", "/")
        p["original"] = current.get("sql") if same_file else None
        problems = policy_check(p, p["original"])
        if not same_file:
            problems.append(f"{p['file']} is not an existing model file")
        if not p.get("dry_run", {}).get("passes"):
            problems.append("the patch does not pass the build: " + short(json.dumps(p.get("dry_run")))[:600])
        if problems:
            r, n = Review(approved=False, reasons="; ".join(problems)), 0
        else:
            r, n = await structured(Review, [SystemMessage(review_prompt), HumanMessage(
                f"Diagnosis:\n{json.dumps(state['diagnosis'])}\n\nOriginal file:\n{p['original']}\n\n"
                f"Proposed file ({p['file']}):\n{p['sql']}\n\nExplanation: {p['explanation']}\n\n"
                f"{column_profiles(state['steps'])}")])
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
    root_column: Optional[str] = None
    cause: Literal["upstream_data", "code_change"]
    action: Literal["patch", "escalate"]
    file: Optional[str] = Field(default=None, description="if patching: the model file to replace, e.g. models/staging/stg_orders.sql")
    sql: Optional[str] = Field(default=None, description="if patching: the complete new contents of that file")
    evidence: str


def build_single_agent(llm, tools):
    by_name = {t.name: t for t in tools}
    usable = [by_name[n] for n in ("get_test_results", "run_sql", "get_lineage", "get_model_sql", "list_tables",
                                    "profile_column", "try_patch")]
    bound = llm.bind_tools(usable + [Resolution])
    prompt = (investigate_prompt.format(steps=max_investigate_steps + 2) + "\n\nIf you patch: " + fix_prompt +
              " Use try_patch to build your patch in a throwaway copy before you submit it.")

    async def solve(state):
        messages = [SystemMessage(prompt), HumanMessage(
            "A dbt build just failed. Find out why, then call Resolution. If the action is patch, include the "
            "complete new contents of the one model file you change.")]
        tokens, steps, res, notes = 0, [], None, []
        for _ in range(max_investigate_steps + 2):
            if tokens > token_budget:
                break
            reply = await call_model(bound, messages)
            tokens += usage(reply)
            messages.append(reply)
            if not reply.tool_calls:
                messages.append(HumanMessage("Call Resolution with your conclusion, or keep investigating."))
                continue
            for call in reply.tool_calls:
                call = call | {"name": clean_name(call["name"])}
                if call["name"] == "Resolution":
                    res, problem = parse_call(Resolution, call)
                    if not problem:
                        res.root_node = canonical(res.root_node)
                        problem = await unknown_node(by_name, res.root_node) or incomplete(res)
                        res = None if problem else res
                    if problem:
                        messages.append(ToolMessage(f"Resolution rejected, fix and resend: {problem}",
                                                    tool_call_id=call["id"]))
                        continue
                    break
                if call["name"] not in by_name:
                    messages.append(ToolMessage(f"unknown tool {call['name']}", tool_call_id=call["id"]))
                    continue
                out = short(tool_text(await by_name[call["name"]].ainvoke(call["args"])))
                steps.append({"agent": "single", "tool": call["name"], "args": call["args"],
                              "result": out if call["name"] == "profile_column" else out[:500]})
                notes.append(f"{call['name']}({json.dumps(call['args'])}) ->\n{out[:1200]}")
                messages.append(ToolMessage(out, tool_call_id=call["id"]))
            if res:
                break
        if res is None:
            res, n = await conclude(llm, by_name, Resolution, [
                SystemMessage(prompt), HumanMessage("What the investigation found:\n\n" + "\n\n".join(notes[-12:]) +
                                                    "\n\nGive your resolution now.")])
            tokens += n
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
