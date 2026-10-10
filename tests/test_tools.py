from pathlib import Path

import pytest

from responder import workspace
from responder.incidents import catalog
from responder.tools import Workspace, check_sql
from responder.warehouse import build_raw

project_src = Path(__file__).resolve().parents[1] / "warehouse"


@pytest.mark.parametrize("q", [
    "select * from marts.fct_orders",
    "with x as (select 1 as a) select a from x",
    "select order_date, count(*) from staging.stg_orders group by 1 order by 1 desc limit 5",
    "select 1 union all select 2",
    "select table_name from information_schema.tables",
])
def test_reads_allowed(q):
    assert check_sql(q) is None


@pytest.mark.parametrize("q", [
    "drop table raw.orders",
    "delete from raw.orders",
    "update raw.orders set o_custkey = 1",
    "insert into raw.orders select * from raw.orders",
    "create table x as select 1",
    "alter table raw.orders rename column o_custkey to x",
    "attach 'other.duckdb'",
    "copy raw.orders to 'out.csv'",
    "select * from read_csv('secrets.csv')",
    "select * from 'C:/somewhere/file.parquet'",
    'select * from "data.csv"',
    "select * from fct_orders",
    "select * from other_db.main.t",
    "select getenv('GROQ_API_KEY')",
    "select 1; drop table raw.orders",
    "install httpfs",
    "pragma database_list",
    "set threads = 1",
])
def test_everything_else_refused(q):
    assert check_sql(q) is not None


@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    # a small warehouse with one incident applied, built for real
    base = tmp_path_factory.mktemp("ws")
    raw = build_raw(base / "source.duckdb", sf=0.001)
    inc = next(i for i in catalog() if i.id == "inc016")  # nulls in customer market segment
    work = workspace.prepare(inc, project_src, raw, base / "inc")
    workspace.build(work)
    return Workspace(work)


def test_run_sql_masks_personal_columns(ws):
    res = ws.run_sql("select customer_key, address, phone, market_segment from staging.stg_customers limit 3")
    assert res["columns"] == ["customer_key", "address", "phone", "market_segment"]
    assert all(r[1] == "***" and r[2] == "***" for r in res["rows"])


def test_run_sql_caps_rows_and_refuses_writes(ws):
    res = ws.run_sql("select * from raw.lineitem")
    assert len(res["rows"]) == 50 and res["truncated"]
    assert ws.run_sql("delete from raw.orders")["error"].startswith("refused")


def test_lineage_and_alarms(ws):
    lin = ws.get_lineage("stg_customers")
    assert "raw.customer" in lin["upstream"] and "dim_customers" in lin["downstream"]
    assert any("market_segment" in c for c in lin["checks"])
    problems = ws.get_test_results()["problems"]
    assert any("market_segment" in p["node"] for p in problems)


def test_model_sql_and_schema(ws):
    assert "c_mktsegment" in ws.get_model_sql("stg_customers")["sql"]
    assert "raw.customer" in ws.list_tables()
    # the failed check stops dbt from building most marts, so only check that the filter keeps to one schema
    marts = ws.list_tables("marts")
    assert marts and all(t.startswith("marts.") for t in marts)
