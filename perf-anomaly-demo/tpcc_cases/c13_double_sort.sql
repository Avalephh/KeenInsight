SELECT count(*), sum(a.ol_amount)
FROM (
  SELECT ol_w_id, ol_d_id, ol_o_id, ol_number, ol_amount
  FROM keeninsight_tpcc.order_line
  ORDER BY ol_amount, ol_w_id, ol_d_id, ol_o_id, ol_number
) AS a
JOIN (
  SELECT ol_w_id, ol_d_id, ol_o_id, ol_number, ol_amount
  FROM keeninsight_tpcc.order_line
  ORDER BY ol_amount, ol_w_id, ol_d_id, ol_o_id, ol_number
) AS b
  ON b.ol_w_id = a.ol_w_id
 AND b.ol_d_id = a.ol_d_id
 AND b.ol_o_id = a.ol_o_id
 AND b.ol_number = a.ol_number;
