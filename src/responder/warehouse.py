import argparse
from pathlib import Path

import duckdb

raw_tables = ["region", "nation", "customer", "supplier", "part", "partsupp", "orders", "lineitem"]


def build_raw(path, sf=0.01):
    # tpc-h generated in duckdb, copied into a "raw" schema that plays the role of landed source data
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    con.execute("install tpch; load tpch")
    con.execute(f"call dbgen(sf={sf})")
    con.execute("create schema raw")
    for t in raw_tables:
        con.execute(f"create table raw.{t} as select * from main.{t}")
        con.execute(f"drop table main.{t}")
    # orders land the day after they are placed
    con.execute("alter table raw.orders add column _loaded_at timestamp")
    con.execute("update raw.orders set _loaded_at = o_orderdate + interval 1 day")
    con.close()
    return path


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--path", default="warehouse/warehouse.duckdb")
    p.add_argument("--sf", type=float, default=0.01)
    args = p.parse_args(argv)
    build_raw(args.path, args.sf)
    print("built", args.path)


if __name__ == "__main__":
    main()
