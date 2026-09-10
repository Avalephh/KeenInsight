SELECT sum(
  (c.c_balance::double precision * g.n)
  + sqrt(abs(c.c_balance::double precision))
  + sin(c.c_balance::double precision)
  + cos(c.c_balance::double precision)
  + ln(abs(c.c_balance::double precision) + 1)
  + exp((c.c_id::double precision) / 100000.0)
)
FROM keeninsight_tpcc.customer AS c
CROSS JOIN generate_series(1,10000) AS g(n)
WHERE g.n = 1;
