SELECT count(*)
FROM (
  SELECT ol_i_id, count(*), sum(ol_amount)
  FROM keeninsight_tpcc.order_line
  GROUP BY ol_i_id
  ORDER BY ol_i_id
) AS item_rollup;
