-- External committed update churn.  The table is a disposable TPCC-schema
-- fixture prepared by tpcc_transaction_cases.py; the business tables are not
-- modified by this pressure source.

\set row_id random(1,100)
UPDATE keeninsight_tpcc.tpcc_demo_autovacuum
   SET value = value + 1,
       touched_at = clock_timestamp()
 WHERE id = :row_id;
