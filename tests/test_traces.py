import json

from responder.traces import compact


def obs(id, type, name, start, ms, parent="root", node="investigate", **kw):
    return {"id": id, "type": type, "name": name, "startTime": f"2026-10-12 01:00:{start:06.3f}", "latencyMs": ms,
            "parentObservationId": parent, "metadata": {"langgraph_node": node}, "sessionId": "",
            "usageDetails": {}, "input": "{}", "output": "", **kw}


def test_trace_keeps_the_timeline_and_drops_the_repeated_prompts():
    trace = compact([
        obs("root", "CHAIN", "LangGraph", 0, 9000, parent="", node="", sessionId="multi-1-inc034"),
        obs("a", "CHAIN", "investigate", 0.5, 8000),
        obs("b", "CHAIN", "RunnableSequence", 0.6, 10),
        obs("c", "GENERATION", "ChatOpenAI", 1, 1500, input="[" + "x" * 10_000 + "]",
            usageDetails={"input": 900, "output": 20, "output_reasoning": 40},
            output=json.dumps({"role": "assistant", "content": "",
                               "tool_calls": [{"name": "functions.run_sql", "args": {"query": "select 1"}}]})),
        obs("d", "TOOL", "run_sql", 2.5, 300, input='{"query": "select 1"}',
            output=json.dumps([{"type": "text", "text": '{"rows": [["1"]]}'}])),
    ])
    assert trace["session"] == "multi-1-inc034" and trace["seconds"] == 9.0
    kinds = [e["kind"] for e in trace["events"]]
    assert kinds == ["agent", "model", "tool"]
    model, tool = trace["events"][1], trace["events"][2]
    assert model["calls"] == [{"name": "run_sql", "args": {"query": "select 1"}}] and model["at"] == 1.0
    assert model["tokens"] == {"in": 900, "out": 20, "reasoning": 40}
    assert tool["result"] == '{"rows": [["1"]]}' and tool["args"] == {"query": "select 1"}
    assert "xxxx" not in json.dumps(trace)
