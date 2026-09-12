-- External mix of two real TPCC read transactions: Order Status and Stock
-- Level.  It intentionally excludes reporting/AP SQL and changes no rows.

\set txn random(1,100)
\set w_id random(1,5)
\set d_id random(1,10)
\set c_id random(1,3000)

BEGIN;
SAVEPOINT tpcc_demo_safety;

\if :txn <= 60
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
\else
  SELECT count(*)
    FROM keeninsight_tpcc.stock s
   WHERE s.s_w_id = :w_id
     AND s.s_quantity < 15
     AND s.s_i_id IN (
           SELECT DISTINCT ol.ol_i_id
             FROM keeninsight_tpcc.order_line ol
            WHERE ol.ol_w_id = :w_id
              AND ol.ol_d_id = :d_id
              AND ol.ol_o_id >= 2980
         );
\endif

ROLLBACK TO SAVEPOINT tpcc_demo_safety;
COMMIT;
