select order_date, region_name, count(*) as n
from {{ ref('fct_daily_revenue') }}
group by order_date, region_name
having count(*) > 1
