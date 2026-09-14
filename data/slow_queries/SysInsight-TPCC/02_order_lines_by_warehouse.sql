SELECT o.o_w_id,
       o.o_d_id,
       COUNT(*) AS line_count,
       SUM(ol.ol_amount) AS total_amount
FROM keeninsight_tpcc.orders AS o
JOIN keeninsight_tpcc.order_line AS ol
  ON ol.ol_w_id = o.o_w_id
 AND ol.ol_d_id = o.o_d_id
 AND ol.ol_o_id = o.o_id
WHERE o.o_w_id BETWEEN 1 AND 5
GROUP BY o.o_w_id, o.o_d_id
ORDER BY total_amount DESC;
