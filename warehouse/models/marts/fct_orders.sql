with items as (
    select
        order_key,
        count(*) as item_count,
        sum(extended_price) as gross_revenue,
        sum(net_revenue) as net_revenue
    from {{ ref('fct_order_items') }}
    group by order_key
)

select
    o.order_key,
    o.customer_key,
    c.region_name,
    o.order_status,
    o.order_date,
    o.priority,
    coalesce(i.item_count, 0) as item_count,
    coalesce(i.gross_revenue, 0) as gross_revenue,
    coalesce(i.net_revenue, 0) as net_revenue,
    o.loaded_at
from {{ ref('stg_orders') }} o
left join {{ ref('dim_customers') }} c on o.customer_key = c.customer_key
left join items i on o.order_key = i.order_key
