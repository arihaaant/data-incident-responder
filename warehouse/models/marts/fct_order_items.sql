select
    li.order_line_id,
    li.order_key,
    li.line_number,
    li.part_key,
    p.brand,
    p.part_type,
    li.quantity,
    li.extended_price,
    li.discount,
    li.net_revenue,
    li.return_flag,
    li.ship_date
from {{ ref('stg_lineitem') }} li
left join {{ ref('stg_parts') }} p on li.part_key = p.part_key
