SELECT s.s_w_id,
       s.s_i_id,
       i.i_name,
       s.s_quantity
FROM keeninsight_tpcc.stock AS s
JOIN keeninsight_tpcc.item AS i
  ON i.i_id = s.s_i_id
WHERE s.s_w_id = 1
  AND s.s_quantity < 20
ORDER BY s.s_quantity, s.s_i_id
LIMIT 100;
