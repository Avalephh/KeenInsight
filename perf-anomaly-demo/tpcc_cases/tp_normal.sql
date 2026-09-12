-- Rollback-protected TPC-C transaction mix.
-- The SQL paths are real New Order, Payment, Order Status, Delivery and
-- Stock Level operations.  The savepoint keeps the demo database unchanged
-- while still exercising PostgreSQL's transaction, lock, index and WAL paths.

\set txn random(1,100)
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

\if :txn <= 45
  -- New Order: allocate an order id, insert order headers/lines and update stock.
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

\elif :txn <= 88
  -- Payment: update warehouse, district and customer and write history.
  UPDATE keeninsight_tpcc.warehouse
     SET w_ytd = w_ytd + 1.00
   WHERE w_id = :w_id;
  UPDATE keeninsight_tpcc.district
     SET d_ytd = d_ytd + 1.00
   WHERE d_w_id = :w_id AND d_id = :d_id;
  UPDATE keeninsight_tpcc.customer
     SET c_balance = c_balance - 1.00,
         c_ytd_payment = c_ytd_payment + 1.00,
         c_payment_cnt = c_payment_cnt + 1
   WHERE c_w_id = :w_id AND c_d_id = :d_id AND c_id = :c_id;
  INSERT INTO keeninsight_tpcc.history
      (h_c_id, h_c_d_id, h_c_w_id, h_d_id, h_d_w_id, h_date, h_amount, h_data)
  VALUES (:c_id, :d_id, :w_id, :d_id, :w_id, clock_timestamp(), 1.00, 'TPCC-DEMO');

\elif :txn <= 92
  -- Order Status: indexed customer/order lookup followed by order lines.
  SELECT o_id
    FROM keeninsight_tpcc.orders
   WHERE o_w_id = :w_id AND o_d_id = :d_id AND o_c_id = :c_id
   ORDER BY o_id DESC
   LIMIT 1 \gset
  SELECT ol_i_id, ol_quantity, ol_amount
    FROM keeninsight_tpcc.order_line
   WHERE ol_w_id = :w_id AND ol_d_id = :d_id AND ol_o_id = :o_id
   ORDER BY ol_number;

\elif :txn <= 96
  -- Delivery: claim the oldest pending order and update its lines/customer.
  -- The CTE deliberately becomes a no-op when a district has no pending
  -- order.  That is valid for this already-used demo database and keeps the
  -- TP transaction mix running without inventing an order id.
  WITH picked AS MATERIALIZED (
         SELECT no_w_id, no_d_id, no_o_id
           FROM keeninsight_tpcc.new_order
          WHERE no_w_id = :w_id AND no_d_id = :d_id
          ORDER BY no_o_id
          LIMIT 1
          FOR UPDATE SKIP LOCKED
       ),
       deleted AS (
         DELETE FROM keeninsight_tpcc.new_order n
          USING picked p
          WHERE n.no_w_id = p.no_w_id
            AND n.no_d_id = p.no_d_id
            AND n.no_o_id = p.no_o_id
         RETURNING p.no_w_id, p.no_d_id, p.no_o_id
       ),
       order_update AS (
         UPDATE keeninsight_tpcc.orders o
            SET o_carrier_id = 1
           FROM deleted d
          WHERE o.o_w_id = d.no_w_id
            AND o.o_d_id = d.no_d_id
            AND o.o_id = d.no_o_id
         RETURNING d.no_w_id, d.no_d_id, d.no_o_id, o.o_c_id
       ),
       line_update AS (
         UPDATE keeninsight_tpcc.order_line ol
            SET ol_delivery_d = clock_timestamp()
           FROM deleted d
          WHERE ol.ol_w_id = d.no_w_id
            AND ol.ol_d_id = d.no_d_id
            AND ol.ol_o_id = d.no_o_id
       )
  UPDATE keeninsight_tpcc.customer c
     SET c_delivery_cnt = c_delivery_cnt + 1
    FROM order_update o
   WHERE c.c_w_id = o.no_w_id
     AND c.c_d_id = o.no_d_id
     AND c.c_id = o.o_c_id;

\else
  -- Stock Level: recent-order item lookup and stock range check.
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
