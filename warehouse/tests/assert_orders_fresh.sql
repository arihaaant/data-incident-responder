-- the latest orders should have landed by the as_of date
select max(order_date) as latest_order_date
from {{ ref('stg_orders') }}
having max(order_date) < date '{{ var("as_of") }}' - interval 2 day
