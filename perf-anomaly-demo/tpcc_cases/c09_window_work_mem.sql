SELECT sum(running_amount)
FROM (
  SELECT ol_w_id, ol_d_id, ol_o_id, ol_number,
         sum(ol_amount) OVER (
           PARTITION BY ol_w_id
           ORDER BY ol_d_id, ol_o_id, ol_number
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
         ) AS running_amount
  FROM keeninsight_tpcc.order_line
) AS running_orders;
