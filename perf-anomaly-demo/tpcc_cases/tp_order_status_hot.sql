-- Real TPC-C Order Status traffic concentrated on one warehouse/district.
-- A current customer/order pair is selected from the TPCC tables so the
-- transaction always exercises both the order and order_line lookups.

\set w_id 1
\set d_id 1

BEGIN;
SAVEPOINT tpcc_demo_safety;

SELECT o_c_id AS c_id, o_id
  FROM keeninsight_tpcc.orders
 WHERE o_w_id = :w_id AND o_d_id = :d_id
 ORDER BY o_id DESC
 LIMIT 1 \gset

SELECT o_id, o_entry_d, o_carrier_id
  FROM keeninsight_tpcc.orders
 WHERE o_w_id = :w_id AND o_d_id = :d_id AND o_id = :o_id;

SELECT ol_i_id, ol_quantity, ol_amount
  FROM keeninsight_tpcc.order_line
 WHERE ol_w_id = :w_id AND ol_d_id = :d_id AND ol_o_id = :o_id
 ORDER BY ol_number;

ROLLBACK TO SAVEPOINT tpcc_demo_safety;
COMMIT;
