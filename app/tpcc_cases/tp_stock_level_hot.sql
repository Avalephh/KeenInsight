-- Real TPC-C Stock Level traffic concentrated on one warehouse/district.
-- The query remains the bounded recent-order inventory check defined by the
-- TPCC transaction, rather than an aggregate report over the database.

\set w_id 1
\set d_id 1

BEGIN;
SAVEPOINT tpcc_demo_safety;

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

ROLLBACK TO SAVEPOINT tpcc_demo_safety;
COMMIT;
