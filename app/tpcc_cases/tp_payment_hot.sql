-- Real TPC-C Payment transactions concentrated on one home warehouse and
-- district.  This models an external site/tenant hot spot while preserving
-- the Payment transaction path and rolling back business-row changes.

\set w_id 1
\set d_id 1
\set c_id random(1,3000)

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
 WHERE c_w_id = :w_id AND c_d_id = :d_id AND c_id = :c_id;
INSERT INTO keeninsight_tpcc.history
    (h_c_id, h_c_d_id, h_c_w_id, h_d_id, h_d_w_id, h_date, h_amount, h_data)
VALUES (:c_id, :d_id, :w_id, :d_id, :w_id, clock_timestamp(), 1.00, 'TPCC-HOT-DEMO');

ROLLBACK TO SAVEPOINT tpcc_demo_safety;
COMMIT;
