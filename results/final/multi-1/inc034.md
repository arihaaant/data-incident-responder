# Incident inc034

**Status:** Fix proposed. Needs a human review before it is merged.

## What broke

- `unique_stg_lineitem_order_line_id` fail (1550 rows): Got 1550 results, configured to fail if != 0

## Diagnosis

- **Started at:** `source.raw.lineitem`
- **Cause:** data arriving from upstream
- **Downstream models affected:** `fct_daily_revenue`, `fct_order_items`, `fct_orders`, `stg_lineitem`

**Evidence**

raw.lineitem has 61,725 rows but only 60,175 distinct order_line_id values; duplicate rows like order_key 583 linenumber 2 appear twice with identical column values, causing 1,550 duplicate order_line_id in stg_lineitem. Identical duplicates indicate a delivery batch duplication, which can be resolved by deduplicating in the stg_lineitem model.

<details><summary>6 queries run during the investigation</summary>

```sql
SELECT COUNT(*) AS total_rows, COUNT(DISTINCT order_line_id) AS distinct_order_line_id FROM raw.lineitem
```
```sql
SELECT COUNT(*) AS total_rows, COUNT(DISTINCT l_orderkey, l_linenumber) AS distinct_combo FROM raw.lineitem
```
```sql
SELECT COUNT(*) AS total_rows, COUNT(DISTINCT l_orderkey || '-' || l_linenumber) AS distinct_order_line_id FROM raw.lineitem
```
```sql
SELECT order_line_id, COUNT(*) AS cnt FROM (SELECT l_orderkey || '-' || l_linenumber AS order_line_id, * FROM raw.lineitem) GROUP BY order_line_id HAVING cnt > 1 LIMIT 5
```
```sql
SELECT l_orderkey, l_linenumber, COUNT(*) AS cnt, COUNT(DISTINCT l_partkey) AS distinct_part, COUNT(DISTINCT l_suppkey) AS distinct_supp FROM raw.lineitem WHERE (l_orderkey || '-' || l_linenumber) = '583-2' GROUP BY l_orderkey, l_linenumber
```
```sql
SELECT * FROM raw.lineitem WHERE l_orderkey = 583 AND l_linenumber = 2
```

</details>

## What was done

Proposed a change to `models/staging/stg_lineitem.sql`. Added DISTINCT to remove exact duplicate rows that were causing 1,550 duplicate order_line_id values. This preserves the original column list and types while ensuring each order_line_id appears only once, satisfying the unique check.

```diff
--- a/models/staging/stg_lineitem.sql
+++ b/models/staging/stg_lineitem.sql
@@ -1,4 +1,5 @@
 select
+    distinct
     l_orderkey || '-' || l_linenumber as order_line_id,
     l_orderkey as order_key,
     l_linenumber as line_number,
```

- **Tested in a copy of the warehouse:** every check passed.
- **Reviewer:** approved. Fix adds DISTINCT to remove exact duplicate rows, directly addressing the duplicate order_line_id issue without masking or altering data semantics. Appropriate for the diagnosed upstream duplication.

## Please verify

- [ ] The diff changes only what the diagnosis describes.
- [ ] No rows are dropped and no values are filled in, except exact duplicates.
- [ ] `dbt build` passes on your branch before merging.
- [ ] The owner of `raw.lineitem` confirms the upstream change is intended.
- [ ] Spot-check the affected models: fct_daily_revenue, fct_order_items, fct_orders, stg_lineitem.
