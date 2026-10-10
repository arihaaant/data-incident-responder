"""What the agents can see: read-only sql, schema, lineage, alarm results and model code.

Plain functions over one incident workspace, so they can be tested directly and wrapped by the mcp server.
"""
import json
import threading
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

max_rows = 50
timeout_s = 5
pii_columns = {"address", "phone", "c_address", "c_phone", "customer_name", "c_name"}
blocked_functions = {"read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_text",
                     "read_blob", "glob", "getenv", "parquet_scan", "csv_scan", "sniff_csv", "query_table",
                     "query"}
allowed_roots = (exp.Select, exp.Union, exp.Intersect, exp.Except)
allowed_schemas = {"raw", "staging", "marts", "information_schema"}


def check_sql(query):
    """Returns None if the query is a plain read, otherwise the reason it is refused."""
    try:
        statements = [s for s in sqlglot.parse(query, read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        return f"could not parse: {str(e).splitlines()[0]}"
    if len(statements) != 1:
        return "one statement at a time"
    stmt = statements[0]
    if not isinstance(stmt, allowed_roots):
        return f"only select queries are allowed, got {stmt.key}"
    for node in stmt.walk():
        if isinstance(node, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command,
                             exp.Copy, exp.Attach, exp.Set, exp.Pragma)):
            return f"{node.key} is not allowed"
        if isinstance(node, exp.Func) and node.sql_name().lower() in blocked_functions:
            return f"{node.sql_name().lower()} is not allowed"
        if isinstance(node, exp.Anonymous) and node.name.lower() in blocked_functions:
            return f"{node.name.lower()} is not allowed"
    # every table has to be a warehouse table or a cte from the same query. this also rules out file paths.
    ctes = {c.alias.lower() for c in stmt.find_all(exp.CTE)}
    for t in stmt.find_all(exp.Table):
        if t.db.lower() in allowed_schemas or (not t.db and t.name.lower() in ctes):
            continue
        return f"unknown table {t.sql(dialect='duckdb')}, use schema.table with schema raw, staging or marts"
    return None


class Workspace:
    def __init__(self, project):
        self.project = Path(project)
        self.db = self.project / "warehouse.duckdb"

    def run_sql(self, query):
        reason = check_sql(query)
        if reason:
            return {"error": f"refused: {reason}"}
        con = duckdb.connect(str(self.db), read_only=True)
        timer = threading.Timer(timeout_s, con.interrupt)
        timer.start()
        try:
            cur = con.execute(query)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows + 1)
        except duckdb.Error as e:
            return {"error": str(e).splitlines()[0]}
        finally:
            timer.cancel()
            con.close()
        masked = [i for i, c in enumerate(cols) if c.lower() in pii_columns]
        rows = [[("***" if i in masked and v is not None else v) for i, v in enumerate(r)] for r in rows]
        return {"columns": cols, "rows": [[str(v) if v is not None else None for v in r] for r in rows[:max_rows]],
                "truncated": len(rows) > max_rows}

    def list_tables(self):
        con = duckdb.connect(str(self.db), read_only=True)
        rows = con.execute(
            "select table_schema, table_name, column_name, data_type from information_schema.columns "
            "order by table_schema, table_name, ordinal_position").fetchall()
        con.close()
        out = {}
        for schema, table, col, typ in rows:
            out.setdefault(f"{schema}.{table}", []).append(f"{col} {typ}")
        return out

    def _manifest(self):
        path = self.project / "target" / "manifest.json"
        return json.loads(path.read_text()) if path.exists() else None

    def get_lineage(self, name):
        m = self._manifest()
        if m is None:
            return {"error": "no manifest, the project failed to parse"}
        ids = [i for i in list(m["nodes"]) + list(m["sources"])
               if i.split(".")[-1] == name or i.endswith(f".{name}") or i == name]
        ids = [i for i in ids if i.startswith(("model.", "source."))]
        if not ids:
            return {"error": f"unknown model or source {name}"}
        uid = ids[0]
        short = lambda i: ".".join(i.split(".")[2:]) if i.startswith("source.") else i.split(".")[-1]
        return {"node": uid,
                "upstream": [short(i) for i in m["parent_map"].get(uid, [])],
                "downstream": [short(i) for i in m["child_map"].get(uid, []) if not i.startswith("test.")],
                "checks": [i.split(".")[2] for i in m["child_map"].get(uid, []) if i.startswith("test.")]}

    def get_test_results(self):
        path = self.project / "target" / "run_results.json"
        if not path.exists():
            return {"error": "no run results, the project failed to parse"}
        out = []
        for r in json.loads(path.read_text())["results"]:
            if r["status"] in ("fail", "error"):
                out.append({"node": r["unique_id"].split(".")[2] if r["unique_id"].startswith("test.")
                            else r["unique_id"], "status": r["status"],
                            "failing_rows": r.get("failures"), "message": (r.get("message") or "")[:1500]})
        skipped = sum(r["status"] == "skipped" for r in json.loads(path.read_text())["results"])
        return {"problems": out, "skipped_downstream": skipped}

    def get_model_sql(self, name):
        hits = list((self.project / "models").rglob(f"{name}.sql"))
        if not hits:
            return {"error": f"no model called {name}"}
        return {"path": str(hits[0].relative_to(self.project)).replace("\\", "/"), "sql": hits[0].read_text()}
