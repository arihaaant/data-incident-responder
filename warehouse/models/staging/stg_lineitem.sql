select
    l_orderkey || '-' || l_linenumber as order_line_id,
    l_orderkey as order_key,
    l_linenumber as line_number,
    l_partkey as part_key,
    l_suppkey as supplier_key,
    l_quantity as quantity,
    l_extendedprice as extended_price,
    l_discount as discount,
    l_tax as tax,
    l_returnflag as return_flag,
    l_shipdate as ship_date,
    l_extendedprice * (1 - l_discount) as net_revenue
from {{ source('raw', 'lineitem') }}
