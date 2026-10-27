SET work_mem = '256MB';
SELECT sum(ol_amount)
FROM (
  SELECT ol_w_id, ol_d_id, ol_o_id, ol_number, ol_amount
  FROM keeninsight_tpcc.order_line
  ORDER BY ol_amount DESC, ol_w_id, ol_d_id, ol_o_id, ol_number
) AS sorted_order_lines;
