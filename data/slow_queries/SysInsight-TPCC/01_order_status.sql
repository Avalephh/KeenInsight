SELECT o.o_w_id,
       o.o_d_id,
       o.o_id,
       o.o_entry_d,
       o.o_carrier_id
FROM keeninsight_tpcc.orders AS o
WHERE o.o_w_id = 1
  AND o.o_d_id = 1
ORDER BY o.o_id DESC
LIMIT 50;
