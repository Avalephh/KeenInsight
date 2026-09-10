SELECT count(*)
FROM (
  SELECT DISTINCT ol_i_id, ol_amount, ol_quantity, ol_supply_w_id
  FROM keeninsight_tpcc.order_line
) AS distinct_order_lines;
