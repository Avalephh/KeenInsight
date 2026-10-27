SELECT sum(running_amount)
FROM (
  SELECT ol_i_id, ol_amount,
         sum(ol_amount) OVER (
           PARTITION BY ol_i_id
           ORDER BY ol_amount
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
         ) AS running_amount
  FROM keeninsight_tpcc.order_line
) AS item_running_orders;
