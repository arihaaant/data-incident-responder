select
    r_regionkey as region_key,
    r_name as region_name
from {{ source('raw', 'region') }}
