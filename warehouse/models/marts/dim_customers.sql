select
    c.customer_key,
    c.customer_name,
    c.market_segment,
    c.account_balance,
    n.nation_name,
    r.region_name
from {{ ref('stg_customers') }} c
join {{ ref('stg_nations') }} n on c.nation_key = n.nation_key
join {{ ref('stg_regions') }} r on n.region_key = r.region_key
