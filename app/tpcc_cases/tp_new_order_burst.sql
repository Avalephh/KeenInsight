-- Real TPC-C New Order transaction under external concurrency.
-- The savepoint rolls back business-row changes after all New Order paths
-- (district allocation, order headers/lines, new_order and stock updates)
-- have executed.

\set w_id random(1,5)
\set d_id random(1,10)
\set c_id random(1,3000)
\set i1 random(1,100000)
\set i2 random(1,100000)
\set i3 random(1,100000)
\set i4 random(1,100000)
\set i5 random(1,100000)

BEGIN;
SAVEPOINT tpcc_demo_safety;

UPDATE keeninsight_tpcc.district
   SET d_next_o_id = d_next_o_id + 1
 WHERE d_w_id = :w_id AND d_id = :d_id
RETURNING d_next_o_id - 1 AS o_id \gset

INSERT INTO keeninsight_tpcc.orders
    (o_w_id, o_d_id, o_id, o_c_id, o_entry_d, o_carrier_id, o_ol_cnt, o_all_local)
VALUES (:w_id, :d_id, :o_id, :c_id, clock_timestamp(), NULL, 5, 1);

INSERT INTO keeninsight_tpcc.new_order (no_w_id, no_d_id, no_o_id)
VALUES (:w_id, :d_id, :o_id);

UPDATE keeninsight_tpcc.stock
   SET s_quantity = CASE WHEN s_quantity >= 11 THEN s_quantity - 1 ELSE s_quantity + 89 END,
       s_ytd = s_ytd + 1,
       s_order_cnt = s_order_cnt + 1
 WHERE s_w_id = :w_id AND s_i_id = :i1;
UPDATE keeninsight_tpcc.stock
   SET s_quantity = CASE WHEN s_quantity >= 11 THEN s_quantity - 1 ELSE s_quantity + 89 END,
       s_ytd = s_ytd + 1,
       s_order_cnt = s_order_cnt + 1
 WHERE s_w_id = :w_id AND s_i_id = :i2;
UPDATE keeninsight_tpcc.stock
   SET s_quantity = CASE WHEN s_quantity >= 11 THEN s_quantity - 1 ELSE s_quantity + 89 END,
       s_ytd = s_ytd + 1,
       s_order_cnt = s_order_cnt + 1
 WHERE s_w_id = :w_id AND s_i_id = :i3;
UPDATE keeninsight_tpcc.stock
   SET s_quantity = CASE WHEN s_quantity >= 11 THEN s_quantity - 1 ELSE s_quantity + 89 END,
       s_ytd = s_ytd + 1,
       s_order_cnt = s_order_cnt + 1
 WHERE s_w_id = :w_id AND s_i_id = :i4;
UPDATE keeninsight_tpcc.stock
   SET s_quantity = CASE WHEN s_quantity >= 11 THEN s_quantity - 1 ELSE s_quantity + 89 END,
       s_ytd = s_ytd + 1,
       s_order_cnt = s_order_cnt + 1
 WHERE s_w_id = :w_id AND s_i_id = :i5;

INSERT INTO keeninsight_tpcc.order_line
    (ol_w_id, ol_d_id, ol_o_id, ol_number, ol_i_id, ol_supply_w_id,
     ol_delivery_d, ol_quantity, ol_amount, ol_dist_info)
VALUES
    (:w_id, :d_id, :o_id, 1, :i1, :w_id, NULL, 1, 1.00, 'TPCC-DEMO'),
    (:w_id, :d_id, :o_id, 2, :i2, :w_id, NULL, 1, 1.00, 'TPCC-DEMO'),
    (:w_id, :d_id, :o_id, 3, :i3, :w_id, NULL, 1, 1.00, 'TPCC-DEMO'),
    (:w_id, :d_id, :o_id, 4, :i4, :w_id, NULL, 1, 1.00, 'TPCC-DEMO'),
    (:w_id, :d_id, :o_id, 5, :i5, :w_id, NULL, 1, 1.00, 'TPCC-DEMO');

ROLLBACK TO SAVEPOINT tpcc_demo_safety;
COMMIT;
