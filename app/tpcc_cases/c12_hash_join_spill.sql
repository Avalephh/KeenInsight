SELECT count(*), sum(a.ol_amount)
FROM keeninsight_tpcc.order_line AS a
JOIN keeninsight_tpcc.order_line AS b
  ON b.ol_w_id = a.ol_w_id
 AND b.ol_d_id = a.ol_d_id
 AND b.ol_o_id = a.ol_o_id
 AND b.ol_number = a.ol_number
WHERE a.ol_amount > 0;

SELECT count(*), sum(a.ol_amount)
FROM keeninsight_tpcc.order_line AS a
JOIN keeninsight_tpcc.order_line AS b
  ON b.ol_w_id = a.ol_w_id
 AND b.ol_d_id = a.ol_d_id
 AND b.ol_o_id = a.ol_o_id
 AND b.ol_number = a.ol_number
WHERE a.ol_amount > 0;

SELECT count(*), sum(a.ol_amount)
FROM keeninsight_tpcc.order_line AS a
JOIN keeninsight_tpcc.order_line AS b
  ON b.ol_w_id = a.ol_w_id
 AND b.ol_d_id = a.ol_d_id
 AND b.ol_o_id = a.ol_o_id
 AND b.ol_number = a.ol_number
WHERE a.ol_amount > 0;
