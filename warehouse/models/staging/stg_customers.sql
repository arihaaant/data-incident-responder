select
    c_custkey as customer_key,
    c_name as customer_name,
    c_address as address,
    c_phone as phone,
    c_nationkey as nation_key,
    c_mktsegment as market_segment,
    c_acctbal as account_balance
from {{ source('raw', 'customer') }}
