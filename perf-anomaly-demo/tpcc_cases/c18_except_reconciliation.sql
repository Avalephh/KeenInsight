SELECT count(*)
FROM (
  SELECT ol_i_id, ol_amount, ol_quantity, ol_supply_w_id, ol_w_id
  FROM keeninsight_tpcc.order_line
  EXCEPT
  SELECT ol_i_id, ol_amount, ol_quantity, ol_supply_w_id, ol_w_id
  FROM keeninsight_tpcc.order_line
  WHERE ol_w_id=1
) AS remaining_feed;
