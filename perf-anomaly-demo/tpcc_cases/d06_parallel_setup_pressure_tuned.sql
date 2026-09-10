SET parallel_setup_cost = '100000';
SELECT o.o_w_id, o.o_d_id, count(*), sum(ol.ol_amount)
FROM keeninsight_tpcc.orders AS o
JOIN keeninsight_tpcc.order_line AS ol
  ON ol.ol_w_id = o.o_w_id
 AND ol.ol_d_id = o.o_d_id
 AND ol.ol_o_id = o.o_id
GROUP BY o.o_w_id, o.o_d_id
ORDER BY sum(ol.ol_amount) DESC;
SELECT o.o_w_id, o.o_d_id, count(*), sum(ol.ol_amount)
FROM keeninsight_tpcc.orders AS o
JOIN keeninsight_tpcc.order_line AS ol
  ON ol.ol_w_id = o.o_w_id
 AND ol.ol_d_id = o.o_d_id
 AND ol.ol_o_id = o.o_id
GROUP BY o.o_w_id, o.o_d_id
ORDER BY sum(ol.ol_amount) DESC;
SELECT o.o_w_id, o.o_d_id, count(*), sum(ol.ol_amount)
FROM keeninsight_tpcc.orders AS o
JOIN keeninsight_tpcc.order_line AS ol
  ON ol.ol_w_id = o.o_w_id
 AND ol.ol_d_id = o.o_d_id
 AND ol.ol_o_id = o.o_id
GROUP BY o.o_w_id, o.o_d_id
ORDER BY sum(ol.ol_amount) DESC;
SELECT o.o_w_id, o.o_d_id, count(*), sum(ol.ol_amount)
FROM keeninsight_tpcc.orders AS o
JOIN keeninsight_tpcc.order_line AS ol
  ON ol.ol_w_id = o.o_w_id
 AND ol.ol_d_id = o.o_d_id
 AND ol.ol_o_id = o.o_id
GROUP BY o.o_w_id, o.o_d_id
ORDER BY sum(ol.ol_amount) DESC;
SELECT o.o_w_id, o.o_d_id, count(*), sum(ol.ol_amount)
FROM keeninsight_tpcc.orders AS o
JOIN keeninsight_tpcc.order_line AS ol
  ON ol.ol_w_id = o.o_w_id
 AND ol.ol_d_id = o.o_d_id
 AND ol.ol_o_id = o.o_id
GROUP BY o.o_w_id, o.o_d_id
ORDER BY sum(ol.ol_amount) DESC;
SELECT o.o_w_id, o.o_d_id, count(*), sum(ol.ol_amount)
FROM keeninsight_tpcc.orders AS o
JOIN keeninsight_tpcc.order_line AS ol
  ON ol.ol_w_id = o.o_w_id
 AND ol.ol_d_id = o.o_d_id
 AND ol.ol_o_id = o.o_id
GROUP BY o.o_w_id, o.o_d_id
ORDER BY sum(ol.ol_amount) DESC;
