import shutil

import duckdb
import pytest

from responder.incidents import catalog
from responder.tools import Workspace
from responder.warehouse import build_raw


@pytest.fixture(scope="module")
def raw_db(tmp_path_factory):
    return build_raw(tmp_path_factory.mktemp("profile") / "source.duckdb", sf=0.001)


def workspace_with(raw_db, tmp_path, incident_id=None):
    shutil.copy2(raw_db, tmp_path / "warehouse.duckdb")
    if incident_id:
        inc = next(i for i in catalog() if i.id == incident_id)
        con = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
        for stmt in inc.sql:
            con.execute(stmt)
        con.close()
    return Workspace(tmp_path)


def test_untouched_columns_are_lossless(raw_db, tmp_path):
    ws = workspace_with(raw_db, tmp_path)
    p = ws.profile_column("raw.lineitem", "l_discount", "decimal(15,2)")
    assert p["lossless"] and p["conversion"] == "cast(l_discount as decimal(15,2))"
    # whole quantities in a decimal column are not a precision loss
    assert ws.profile_column("lineitem", "l_quantity", "decimal(15,2)")["lossless"]


def test_number_arriving_as_text_is_lossless(raw_db, tmp_path):
    p = workspace_with(raw_db, tmp_path, "inc010").profile_column("lineitem", "l_discount", "decimal(15,2)")
    assert p["current_type"] == "varchar" and p["lossless"], p["issues"]


def test_us_dates_parse_with_their_format(raw_db, tmp_path):
    p = workspace_with(raw_db, tmp_path, "inc012").profile_column("orders", "o_orderdate", "date")
    assert p["lossless"] and p["best_format"] == "%m/%d/%Y"
    assert p["conversion"] == "strptime(trim(o_orderdate), '%m/%d/%Y')::date"


def test_rounded_prices_are_flagged(raw_db, tmp_path):
    p = workspace_with(raw_db, tmp_path, "inc061").profile_column("lineitem", "l_extendedprice", "decimal(15,2)")
    assert not p["lossless"] and p["share_with_fraction"] == 0 and p["conversion"] is None


def test_unparseable_dates_are_flagged(raw_db, tmp_path):
    p = workspace_with(raw_db, tmp_path, "inc062").profile_column("orders", "o_orderdate", "date")
    assert not p["lossless"] and p["fail_to_convert"] > 0 and "unknown" in p["examples_that_fail"]


def test_truncated_decimals_are_flagged(raw_db, tmp_path):
    p = workspace_with(raw_db, tmp_path, "inc063").profile_column("customer", "c_acctbal", "decimal(15,2)")
    assert not p["lossless"] and any("decimal places" in i for i in p["issues"])


def test_extra_digits_would_be_rounded(raw_db, tmp_path):
    ws = workspace_with(raw_db, tmp_path)
    con = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    con.execute("alter table raw.lineitem alter column l_discount type varchar using (l_discount + 0.005)::varchar")
    con.close()
    p = ws.profile_column("lineitem", "l_discount", "decimal(15,2)")
    assert not p["lossless"] and p["values_changed_by_conversion"] > 0


def test_bad_arguments_are_refused(raw_db, tmp_path):
    ws = workspace_with(raw_db, tmp_path)
    assert "error" in ws.profile_column("lineitem; drop table x", "l_discount", "decimal(15,2)")
    assert "error" in ws.profile_column("lineitem", "l_discount", "text); drop table raw.orders; --")
    assert "error" in ws.profile_column("lineitem", "no_such_column", "date")
