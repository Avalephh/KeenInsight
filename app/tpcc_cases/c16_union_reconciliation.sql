SELECT count(*)
FROM (
  SELECT ol_i_id, ol_amount, ol_quantity, ol_supply_w_id, ol_w_id
  FROM keeninsight_tpcc.order_line
  UNION
  SELECT ol_i_id, ol_amount, ol_quantity, ol_supply_w_id, ol_w_id
  FROM keeninsight_tpcc.order_line
) AS duplicate_feed;
