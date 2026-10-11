"""What the agents can see: read-only sql, schema, lineage, alarm results and model code.

Plain functions over one incident workspace, so they can be tested directly and wrapped by the mcp server.
"""
import json
import re
import shutil
import tempfile
import threading
from pathlib import Path

import duckdb
import sqlglot
from sqlglot import exp

from . import workspace

max_rows = 50
timeout_s = 5
pii_columns = {"address", "phone", "c_address", "c_phone", "customer_name", "c_name"}
blocked_functions = {"read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_text",
                     "read_blob", "glob", "getenv", "parquet_scan", "csv_scan", "sniff_csv", "query_table",
                     "query"}
allowed_roots = (exp.Select, exp.Union, exp.Intersect, exp.Except)
# statement types that write or change things. looked up by name because they differ between sqlglot versions.
blocked_nodes = tuple(getattr(exp, n) for n in ("Insert", "Update", "Delete", "Create", "Drop", "Alter", "Command",
                                                 "Copy", "Attach", "Detach", "Set", "Pragma", "Merge", "Use")
                      if hasattr(exp, n))
allowed_schemas = {"raw", "staging", "marts", "information_schema"}
date_formats = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%Y", "%Y%m%d", "%d.%m.%Y"]


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
        if isinstance(node, blocked_nodes):
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

    def list_tables(self, schema=None):
        con = duckdb.connect(str(self.db), read_only=True)
        rows = con.execute(
            "select table_schema, table_name, column_name, data_type from information_schema.columns "
            "where ? is null or table_schema = ? order by table_schema, table_name, ordinal_position",
            [schema, schema]).fetchall()
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
        schema, _, rest = name.partition(".")
        if rest and schema in ("staging", "marts"):
            name = rest  # models can be asked for with or without their schema
        ids = [i for i in list(m["nodes"]) + list(m["sources"])
               if i.split(".")[-1] == name or i.endswith(f".{name}") or i == name]
        ids = [i for i in ids if i.startswith(("model.", "source."))]
        if not ids:
            known = sorted(".".join(i.split(".")[2:]) if i.startswith("source.") else i.split(".")[-1]
                           for i in list(m["nodes"]) + list(m["sources"]) if i.startswith(("model.", "source.")))
            return {"error": f"unknown model or source {name}", "known": known}
        uid = ids[0]
        short = lambda i: ".".join(i.split(".")[2:]) if i.startswith("source.") else i.split(".")[-1]
        return {"node": uid,
                "upstream": [short(i) for i in m["parent_map"].get(uid, [])],
                "downstream": [short(i) for i in m["child_map"].get(uid, []) if not i.startswith("test.")],
                "checks": [i.split(".")[2] for i in m["child_map"].get(uid, []) if i.startswith("test.")]}

    def get_test_results(self):
        return problems(self.project)

    def get_model_sql(self, name):
        hits = list((self.project / "models").rglob(f"{name}.sql"))
        if not hits:
            return {"error": f"no model called {name}"}
        return {"path": str(hits[0].relative_to(self.project)).replace("\\", "/"), "sql": hits[0].read_text()}

    def profile_column(self, table, column, target_type):
        """Whether a raw column converts to the type its staging contract expects without losing anything."""
        table = table.split(".")[-1]
        target = target_type.strip().lower()
        if not re.fullmatch(r"[a-z_]+", table) or not re.fullmatch(r"[a-z_]+", column):
            return {"error": "table and column must be plain names, e.g. lineitem and l_discount"}
        if not re.fullmatch(r"decimal\(\d{1,2},\d{1,2}\)|integer|bigint|double|date|timestamp|varchar", target):
            return {"error": "target_type must be one of decimal(p,s), integer, bigint, double, date, timestamp, varchar"}
        con = duckdb.connect(str(self.db), read_only=True)
        try:
            found = con.execute("select data_type from information_schema.columns where table_schema = 'raw' "
                                "and table_name = ? and column_name = ?", [table, column]).fetchone()
            if not found:
                return {"error": f"raw.{table} has no column {column}"}
            current = found[0].lower()
            col, src = f'"{column}"', f"raw.{table}"
            total, nulls = con.execute(f"select count(*), count(*) - count({col}) from {src}").fetchone()
            out = {"column": f"raw.{table}.{column}", "current_type": current, "target_type": target,
                   "rows": total, "nulls": nulls}
            issues = []
            text = current == "varchar"

            if target in ("date", "timestamp") and text:
                # a format change looks like a failed cast unless the actual format is tried
                tried = {}
                for fmt in date_formats:
                    tried[fmt] = con.execute(f"select count(try_strptime(trim({col}), '{fmt}')) from {src}").fetchone()[0]
                best = max(tried, key=tried.get)
                parsed = tried[best]
                out["formats_that_parse"] = {f: n for f, n in tried.items() if n}
                out["best_format"] = best if parsed else None
                out["convertible"] = parsed
                both = [f for f, n in tried.items() if n == parsed and f != best and parsed]
                if both:
                    issues.append(f"ambiguous: {best} and {both} parse the same values, so day and month can't be told apart")
                expr = f"strptime(trim({column}), '{best}')::{target}"
            else:
                out["convertible"] = con.execute(f"select count(try_cast({col} as {target})) from {src}").fetchone()[0]
                expr = f"cast({column} as {target})"

            failed = total - nulls - out["convertible"]
            out["fail_to_convert"] = failed
            if failed:
                examples = con.execute(
                    f"select distinct {col}::varchar from {src} where {col} is not null and "
                    + (f"try_strptime(trim({col}), '{out['best_format']}') is null" if "best_format" in out and out["best_format"]
                       else f"try_cast({col} as {target}) is null") + " limit 5").fetchall()
                out["examples_that_fail"] = [e[0] for e in examples]
                issues.append(f"{failed} values do not convert to {target}")

            scale = re.fullmatch(r"decimal\(\d+,(\d+)\)", target)
            if scale or target in ("integer", "bigint"):
                keep = int(scale.group(1)) if scale else 0
                exact = f"try_cast({col} as decimal(38,12))"
                # how many decimal places the source actually carries
                if text:
                    places = f"length(rtrim(split_part(trim({col}), '.', 2), '0'))"
                    written = f"length(split_part(trim({col}), '.', 2))"
                    out["decimal_places_sent"] = dict(con.execute(
                        f"select {written} as d, count(*) from {src} where {col} is not null group by d order by d").fetchall())
                elif current in ("integer", "bigint", "smallint", "hugeint", "tinyint"):
                    places = "0"
                    out["decimal_places_sent"] = {0: total - nulls}
                else:
                    places = f"length(rtrim(split_part({exact}::varchar, '.', 2), '0'))"
                    m = re.fullmatch(r"decimal\(\d+,(\d+)\)", current)
                    out["decimal_places_sent"] = {int(m.group(1)) if m else "float": total - nulls}
                rounded = con.execute(f"select count(*) from {src} where {places} > {keep}").fetchone()[0]
                out["values_changed_by_conversion"] = rounded
                if rounded:
                    issues.append(f"{rounded} values have more than {keep} decimal places and would be rounded")
                with_fraction = con.execute(
                    f"select count(*) from {src} where {exact} is not null and {exact} <> floor({exact})").fetchone()[0]
                out["share_with_fraction"] = round(with_fraction / max(total - nulls, 1), 4)
                # only meaningful when the type changed: a decimal column that holds whole numbers is fine as is
                if keep > 0 and current != target and with_fraction == 0 and total - nulls > 0:
                    issues.append(f"no value has a fractional part although the contract keeps {keep} decimal places; "
                                  "if the source used to send fractions, they were dropped before arriving")
                elif keep > 0 and text and max(out["decimal_places_sent"], default=0) < keep:
                    issues.append(f"values arrive with at most {max(out['decimal_places_sent'])} decimal places where "
                                  f"the contract keeps {keep}; the missing digits can't be recovered")
            out["lossless"] = not issues
            out["issues"] = issues
            out["conversion"] = expr if not issues else None
            return out
        except duckdb.Error as e:
            return {"error": str(e).splitlines()[0]}
        finally:
            con.close()

    def try_patch(self, file, sql):
        """Build the project with one model file replaced, in a throwaway copy. Nothing here is kept."""
        f = file.replace("\\", "/")
        if not re.fullmatch(r"models/(staging|marts)/[a-z_]+\.sql", f) or not (self.project / f).exists():
            return {"error": f"{file} is not an existing model file"}
        with tempfile.TemporaryDirectory() as tmp:
            copy = workspace.copy_project(self.project, Path(tmp) / "project")
            shutil.copy2(self.db, copy / "warehouse.duckdb")
            (copy / f).write_text(sql)
            workspace.build(copy)
            out = problems(copy)
        out["passes"] = not out.get("problems") and "error" not in out
        return out


def problems(project):
    path = Path(project) / "target" / "run_results.json"
    if not path.exists():
        return {"error": "no run results, the project failed to parse"}
    results = json.loads(path.read_text())["results"]
    out = []
    for r in results:
        if r["status"] in ("fail", "error"):
            out.append({"node": r["unique_id"].split(".")[2] if r["unique_id"].startswith("test.")
                        else r["unique_id"], "status": r["status"],
                        "failing_rows": r.get("failures"), "message": (r.get("message") or "")[:1500]})
    return {"problems": out, "skipped_downstream": sum(r["status"] == "skipped" for r in results)}
