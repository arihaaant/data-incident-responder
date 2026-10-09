select
    order_date,
    region_name,
    count(*) as orders,
    sum(net_revenue) as net_revenue
from {{ ref('fct_orders') }}
group by order_date, region_name
