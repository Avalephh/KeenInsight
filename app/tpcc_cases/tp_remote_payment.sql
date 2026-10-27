-- Real TPC-C remote Payment branch under external concurrency.
-- The home warehouse/district and remote customer are selected from the
-- loaded TPCC warehouses; the savepoint protects business rows.

\set w_id random(1,5)
\set d_id random(1,10)
\set c_id random(1,3000)
SELECT CASE WHEN :w_id < 5 THEN :w_id + 1 ELSE 1 END AS c_w_id \gset

BEGIN;
SAVEPOINT tpcc_demo_safety;

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
 WHERE c_w_id = :c_w_id AND c_d_id = :d_id AND c_id = :c_id;

INSERT INTO keeninsight_tpcc.history
    (h_c_id, h_c_d_id, h_c_w_id, h_d_id, h_d_w_id, h_date, h_amount, h_data)
VALUES (:c_id, :d_id, :c_w_id, :d_id, :w_id, clock_timestamp(), 1.00, 'TPCC-REMOTE-DEMO');

ROLLBACK TO SAVEPOINT tpcc_demo_safety;
COMMIT;
