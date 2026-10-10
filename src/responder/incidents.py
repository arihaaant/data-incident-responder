"""Seeded data incidents with answer keys.

Every incident is one of four kinds (schema drift, null spike, duplicates, late data) and comes either
from upstream data (a change to the raw tables) or from a code change (an edit to a dbt model).
The answer key says where it started and what the right response is: patch the dbt project, or
escalate to whoever owns the upstream data because nothing downstream can honestly fix it.
"""
from dataclasses import asdict, dataclass, field

as_of = "1998-08-02"


@dataclass
class Incident:
    id: str
    kind: str          # schema_drift | null_spike | duplicates | late_data
    cause: str         # upstream_data | code_change
    action: str        # patch | escalate
    root_node: str     # e.g. source.raw.orders or model.stg_orders
    root_column: str | None
    description: str
    sql: list = field(default_factory=list)    # run against the raw tables
    edit: dict | None = None                   # {"file", "old", "new"} applied to the dbt project
    split: str = "dev"
    also_accept: list = field(default_factory=list)  # other names for the root column, e.g. after a rename

    def as_dict(self):
        return asdict(self)


def pick(key, pct):
    # deterministic "random" subset of rows
    return f"({key} * 7919) % 100 < {pct}"


# schema drift: an upstream column was renamed or changed type. the staging model has to follow it.

renames = [
    ("orders", "o_orderpriority", "o_order_priority"),
    ("orders", "o_custkey", "o_customer_key"),
    ("orders", "o_orderdate", "o_order_date"),
    ("customer", "c_mktsegment", "c_market_segment"),
    ("customer", "c_acctbal", "c_account_balance"),
    ("customer", "c_nationkey", "c_nation_key"),
    ("lineitem", "l_discount", "l_discount_rate"),
    ("lineitem", "l_extendedprice", "l_extended_price"),
    ("lineitem", "l_shipdate", "l_ship_date"),
    ("part", "p_brand", "p_brand_name"),
    ("nation", "n_regionkey", "n_region_key"),
]

retypes = [  # table, column, how it now arrives (alter ... using), what it looked like, how to read it back
    ("lineitem", "l_discount", "l_discount::varchar", "text", "cast({c} as decimal(15,2))"),
    ("lineitem", "l_extendedprice", "l_extendedprice::varchar", "text", "cast({c} as decimal(15,2))"),
    ("orders", "o_orderdate", "strftime(o_orderdate, '%m/%d/%Y')", "MM/DD/YYYY text",
     "strptime({c}, '%m/%d/%Y')::date"),
    ("customer", "c_acctbal", "c_acctbal::varchar", "text", "cast({c} as decimal(15,2))"),
]

# null spikes from upstream: values missing in the source. downstream can't invent them, so escalate.

upstream_nulls = [
    ("customer", "c_mktsegment", "c_custkey", 25),
    ("orders", "o_custkey", "o_orderkey", 10),
    ("lineitem", "l_extendedprice", "l_orderkey * 8 + l_linenumber", 15),
    ("lineitem", "l_discount", "l_orderkey * 8 + l_linenumber", 20),
    ("orders", "o_orderdate", "o_orderkey", 5),
    ("customer", "c_nationkey", "c_custkey", 8),
    ("nation", "n_regionkey", "n_nationkey", 12),
    ("lineitem", "l_orderkey", "l_orderkey * 8 + l_linenumber", 3),
    ("customer", "c_custkey", "c_custkey", 4),
]

# null spikes from code: a model edit started producing nulls. revert the bad logic.

code_nulls = [
    ("models/marts/dim_customers.sql", "join {{ ref('stg_nations') }} n on c.nation_key = n.nation_key",
     "left join {{ ref('stg_nations') }} n on c.customer_key = n.nation_key", "model.dim_customers", "region_name",
     "nation join switched to the customer key"),
    ("models/staging/stg_lineitem.sql", "l_extendedprice as extended_price,",
     "case when l_returnflag = 'N' then l_extendedprice end as extended_price,", "model.stg_lineitem", "extended_price",
     "price only kept for non-returned items"),
    ("models/staging/stg_customers.sql", "c_mktsegment as market_segment,",
     "nullif(trim(c_mktsegment), 'MACHINERY') as market_segment,", "model.stg_customers", "market_segment",
     "one market segment nulled out"),
    ("models/marts/fct_orders.sql", "left join {{ ref('dim_customers') }} c on o.customer_key = c.customer_key",
     "left join {{ ref('dim_customers') }} c on o.order_key = c.customer_key", "model.fct_orders", "region_name",
     "customer join on the order key"),
    ("models/staging/stg_orders.sql", "o_orderdate as order_date,",
     "case when o_orderstatus <> 'P' then o_orderdate end as order_date,", "model.stg_orders", "order_date",
     "order date dropped for pending orders"),
    ("models/staging/stg_lineitem.sql", "l_discount as discount,",
     "case when l_discount > 0 then l_discount end as discount,", "model.stg_lineitem", "discount",
     "zero discounts turned into nulls"),
]

# duplicates from upstream: a batch was delivered twice. deduplicating in staging is the standard fix.

upstream_dupes = [
    ("orders", "o_orderkey", 2), ("orders", "o_orderkey", 8), ("orders", "o_orderkey", 20),
    ("lineitem", "l_orderkey * 8 + l_linenumber", 3), ("lineitem", "l_orderkey * 8 + l_linenumber", 10),
    ("lineitem", "l_orderkey * 8 + l_linenumber", 25),
    ("customer", "c_custkey", 5), ("customer", "c_custkey", 15),
    ("part", "p_partkey", 10),
]

dupe_keys = {"orders": "o_orderkey", "lineitem": "l_orderkey", "customer": "c_custkey", "part": "p_partkey"}

# duplicates from code: a join or grouping change fans rows out.

code_dupes = [
    ("models/marts/fct_orders.sql", "    group by order_key", "    group by order_key, line_number",
     "model.fct_orders", "order_key", "order items grouped by line instead of order"),
    ("models/staging/stg_orders.sql", "from {{ source('raw', 'orders') }}",
     "from {{ source('raw', 'orders') }}\nunion all\nselect o_orderkey, o_custkey, o_orderstatus, o_totalprice, o_orderdate, "
     "o_orderpriority, _loaded_at\nfrom {{ source('raw', 'orders') }}\nwhere o_orderstatus = 'P'",
     "model.stg_orders", "order_key", "pending orders unioned in a second time"),
    ("models/marts/dim_customers.sql", "on n.region_key = r.region_key", "on n.region_key = n.region_key",
     "model.dim_customers", "customer_key", "region join compares a column to itself"),
    ("models/staging/stg_customers.sql", "from {{ source('raw', 'customer') }}",
     "from {{ source('raw', 'customer') }}\nunion all\nselect c_custkey, c_name, c_address, c_phone, c_nationkey, "
     "c_mktsegment, c_acctbal\nfrom {{ source('raw', 'customer') }}\nwhere c_mktsegment = 'BUILDING'",
     "model.stg_customers", "customer_key", "one segment unioned in a second time"),
    ("models/marts/fct_daily_revenue.sql", "group by order_date, region_name",
     "group by order_date, region_name, priority", "model.fct_daily_revenue", "order_date",
     "daily revenue grouped by priority as well"),
    ("models/staging/stg_lineitem.sql", "from {{ source('raw', 'lineitem') }}",
     "from {{ source('raw', 'lineitem') }}\nunion all\nselect l_orderkey || '-' || l_linenumber, l_orderkey, l_linenumber, "
     "l_partkey, l_suppkey, l_quantity, l_extendedprice, l_discount, l_tax, l_returnflag, l_shipdate, "
     "l_extendedprice * (1 - l_discount)\nfrom {{ source('raw', 'lineitem') }}\nwhere l_returnflag = 'R'",
     "model.stg_lineitem", "order_line_id", "returned items unioned in a second time"),
]

# late data from upstream: the latest days never arrived. nothing to patch, escalate.

late_days = [3, 4, 6, 8, 11, 15, 21, 30, 45]

# late data from code: a filter in the model hides the recent rows.

code_late = [
    ("from {{ source('raw', 'orders') }}", "from {{ source('raw', 'orders') }}\nwhere o_orderdate < date '1998-07-01'",
     "hard-coded date cutoff left in"),
    ("from {{ source('raw', 'orders') }}", "from {{ source('raw', 'orders') }}\nwhere _loaded_at < date '1998-06-15'",
     "load-time cutoff left in"),
    ("from {{ source('raw', 'orders') }}", "from {{ source('raw', 'orders') }}\nwhere year(o_orderdate) < 1998",
     "current year filtered out"),
    ("from {{ source('raw', 'orders') }}", "from {{ source('raw', 'orders') }}\nwhere o_orderstatus <> 'O'",
     "open orders filtered out"),
    ("from {{ source('raw', 'orders') }}",
     "from {{ source('raw', 'orders') }}\nwhere o_orderdate < date '{{ var(\"as_of\") }}' - interval 30 day",
     "thirty day lag buffer"),
    ("from {{ source('raw', 'orders') }}",
     "from {{ source('raw', 'orders') }}\nwhere o_orderdate < (select max(o_orderdate) from {{ source('raw', 'orders') }}) - interval 7 day",
     "trailing week excluded"),
]

lossy_retypes = [  # table, column, "<new type> using <expression>", what happened, split
    ("lineitem", "l_extendedprice", "bigint using round(l_extendedprice)",
     "raw.lineitem.l_extendedprice now arrives as whole dollars; the cents are gone", "dev"),
    ("orders", "o_orderdate",
     f"varchar using case when {pick('o_orderkey', 4)} then 'unknown' else strftime(o_orderdate, '%Y-%m-%d') end",
     "raw.orders.o_orderdate now arrives as text and 4% of it says 'unknown'", "dev"),
    ("customer", "c_acctbal", "varchar using round(c_acctbal, 1)::varchar",
     "raw.customer.c_acctbal now arrives as text with one decimal place; the cents are gone", "holdout"),
]

staging_for = {"orders": "stg_orders", "customer": "stg_customers", "lineitem": "stg_lineitem",
               "part": "stg_parts", "nation": "stg_nations"}


def catalog():
    out = []
    n = 0

    def add(**kw):
        nonlocal n
        n += 1
        out.append(Incident(id=f"inc{n:03d}", **kw))

    def rename(t, old, new):
        add(kind="schema_drift", cause="upstream_data", action="patch", root_node=f"source.raw.{t}", root_column=old,
            also_accept=[new], description=f"raw.{t}.{old} renamed to {new}; {staging_for[t]} has to follow",
            sql=[f"alter table raw.{t} rename column {old} to {new}"])

    def retype(t, col, using, looks):
        add(kind="schema_drift", cause="upstream_data", action="patch", root_node=f"source.raw.{t}", root_column=col,
            description=f"raw.{t}.{col} now arrives as {looks}; {staging_for[t]} has to read it back",
            sql=[f"alter table raw.{t} alter column {col} type varchar using {using}"])

    # ordered so the held-out set gets two renames and one type change
    for r in renames[:-2]:
        rename(*r)
    for t, col, using, looks, _ in retypes:
        retype(t, col, using, looks)
    for r in renames[-2:]:
        rename(*r)

    for t, col, key, pct in upstream_nulls:
        add(kind="null_spike", cause="upstream_data", action="escalate", root_node=f"source.raw.{t}", root_column=col,
            description=f"{pct}% of raw.{t}.{col} arrived empty",
            sql=[f"update raw.{t} set {col} = null where {pick(key, pct)}"])
    for f, old, new, node, col, what in code_nulls:
        add(kind="null_spike", cause="code_change", action="patch", root_node=node, root_column=col,
            description=what, edit={"file": f, "old": old, "new": new})

    for t, key, pct in upstream_dupes:
        add(kind="duplicates", cause="upstream_data", action="patch", root_node=f"source.raw.{t}",
            root_column=dupe_keys[t], description=f"{pct}% of raw.{t} delivered twice",
            sql=[f"insert into raw.{t} select * from raw.{t} where {pick(key, pct)}"])
    for f, old, new, node, col, what in code_dupes:
        add(kind="duplicates", cause="code_change", action="patch", root_node=node, root_column=col,
            description=what, edit={"file": f, "old": old, "new": new})

    for d in late_days:
        cutoff = f"date '{as_of}' - interval {d} day"
        add(kind="late_data", cause="upstream_data", action="escalate", root_node="source.raw.orders",
            root_column="o_orderdate", description=f"the last {d} days of orders have not arrived",
            sql=[f"delete from raw.lineitem where l_orderkey in (select o_orderkey from raw.orders where o_orderdate > {cutoff})",
                 f"delete from raw.orders where o_orderdate > {cutoff}"])
    for old, new, what in code_late:
        add(kind="late_data", cause="code_change", action="patch", root_node="model.stg_orders", root_column="order_date",
            description=what, edit={"file": "models/staging/stg_orders.sql", "old": old, "new": new})

    # hold out the last 2 upstream and the last code variant of every kind for the final test
    for kind in ("schema_drift", "null_spike", "duplicates", "late_data"):
        for cause, k in (("upstream_data", 2), ("code_change", 1)):
            same = [i for i in out if i.kind == kind and i.cause == cause]
            for i in same[-k:]:
                i.split = "holdout"
    # schema drift has no code variants, so it gives up one more upstream variant instead
    drift = [i for i in out if i.kind == "schema_drift" and i.split == "dev"]
    drift[-1].split = "holdout"

    # type changes that lose information. added after the first 60 so their ids stay stable.
    # the staging model can't get back what the source no longer sends, so the right call is to flag it.
    for t, col, using, what, split in lossy_retypes:
        add(kind="schema_drift", cause="upstream_data", action="escalate", root_node=f"source.raw.{t}",
            root_column=col, description=what, split=split,
            sql=[f"alter table raw.{t} alter column {col} type {using}"])
    return out
