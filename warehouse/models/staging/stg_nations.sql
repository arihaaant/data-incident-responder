select
    n_nationkey as nation_key,
    n_name as nation_name,
    n_regionkey as region_key
from {{ source('raw', 'nation') }}
