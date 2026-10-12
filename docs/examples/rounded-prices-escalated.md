# Incident inc061

**Status:** Escalated to the owner of raw.lineitem. Nothing was changed.

## What broke

- `model.tpch_warehouse.stg_lineitem` error: Compilation Error in model stg_lineitem (models/staging/stg_lineitem.sql)

## Diagnosis

- **Started at:** `source.raw.lineitem`, column `l_extendedprice`
- **Cause:** data arriving from upstream
- **Downstream models affected:** `fct_daily_revenue`, `fct_order_items`, `fct_orders`, `stg_lineitem`

**Evidence**

The contract for stg_lineitem expects extended_price as DECIMAL(15,2) but raw.lineitem.l_extendedprice is BIGINT. profile_column shows lossless false: all values are integers, no fractional part, so conversion would lose precision. net_revenue also mismatches because it derives from the integer column. The model simply selects the raw column without casting, so the schema drift originates from the source raw table.

**Column profile** `raw.lineitem.l_extendedprice` (bigint to decimal(15,2)): would lose data: no value has a fractional part although the contract keeps 2 decimal places; if the source used to send fractions, they were dropped before arriving

## What was done

No change was made to the warehouse. The data has to be corrected where it comes from.

## Please verify

- [ ] Confirm the problem with the owner of `raw.lineitem`.
- [ ] Re-run `dbt build` once the source is corrected; the alarms should clear.
- [ ] Until then, treat these models as unreliable: fct_daily_revenue, fct_order_items, fct_orders, stg_lineitem.
