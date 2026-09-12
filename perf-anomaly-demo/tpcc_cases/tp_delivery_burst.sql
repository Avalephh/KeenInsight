-- Real TPC-C Delivery transaction under external concurrency.
-- SKIP LOCKED is part of the transaction path; the savepoint keeps the
-- existing TPCC business rows unchanged after the delivery work runs.

\set w_id random(1,5)
\set d_id random(1,10)

BEGIN;
SAVEPOINT tpcc_demo_safety;

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

ROLLBACK TO SAVEPOINT tpcc_demo_safety;
COMMIT;
