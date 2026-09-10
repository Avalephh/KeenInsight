SELECT sum(q)
FROM generate_series(1,5000) AS g
CROSS JOIN LATERAL (
  SELECT sum(c_balance) AS q
  FROM keeninsight_tpcc.customer
  WHERE c_w_id = 1
    AND c_d_id = 1
    AND c_id BETWEEN 1 AND (300 + g * 0)
) AS customer_slice;
