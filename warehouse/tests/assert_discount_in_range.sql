select order_line_id, discount
from {{ ref('stg_lineitem') }}
where discount < 0 or discount > 0.1
