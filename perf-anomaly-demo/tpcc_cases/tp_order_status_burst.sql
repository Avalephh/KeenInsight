-- Real TPC-C Order Status transaction under external read concurrency.
-- The COALESCE keeps the lookup total (and avoids inventing a missing
-- variable) on any district/customer combination in the demo dataset.

\set w_id random(1,5)
\set d_id random(1,10)
\set c_id random(1,3000)

BEGIN;
SAVEPOINT tpcc_demo_safety;

SELECT COALESCE(
         (SELECT max(o_id)
            FROM keeninsight_tpcc.orders
           WHERE o_w_id = :w_id AND o_d_id = :d_id AND o_c_id = :c_id),
         1
       ) AS o_id \gset

SELECT o_id, o_entry_d, o_carrier_id
  FROM keeninsight_tpcc.orders
 WHERE o_w_id = :w_id AND o_d_id = :d_id AND o_id = :o_id;

SELECT ol_i_id, ol_quantity, ol_amount
  FROM keeninsight_tpcc.order_line
 WHERE ol_w_id = :w_id AND ol_d_id = :d_id AND ol_o_id = :o_id
 ORDER BY ol_number;

ROLLBACK TO SAVEPOINT tpcc_demo_safety;
COMMIT;
