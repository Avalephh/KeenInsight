SET work_mem = '256MB';
SELECT count(*)
FROM (
  SELECT ol_i_id, ol_amount, ol_quantity, ol_supply_w_id
  FROM keeninsight_tpcc.order_line
  WHERE ol_amount > 0
  INTERSECT
  SELECT ol_i_id, ol_amount, ol_quantity, ol_supply_w_id
  FROM keeninsight_tpcc.order_line
  WHERE ol_amount > 0
) AS common_feed;
