SELECT sum(a.ol_amount + b.ol_amount)
FROM keeninsight_tpcc.orders AS o
JOIN keeninsight_tpcc.order_line AS a
  ON a.ol_w_id = o.o_w_id
 AND a.ol_d_id = o.o_d_id
 AND a.ol_o_id = o.o_id
JOIN keeninsight_tpcc.order_line AS b
  ON b.ol_w_id = o.o_w_id
 AND b.ol_d_id = o.o_d_id
 AND b.ol_o_id = o.o_id
WHERE o.o_id = (o.o_id + floor(random() * 0));
