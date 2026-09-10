CREATE TEMP TABLE tpcc_maintenance_pressure AS
SELECT g.n AS copy_no,
       ol_w_id, ol_d_id, ol_o_id, ol_number, ol_amount, ol_i_id
FROM keeninsight_tpcc.order_line
CROSS JOIN generate_series(1,2) AS g(n);
CREATE INDEX tpcc_maintenance_pressure_idx
  ON tpcc_maintenance_pressure (ol_i_id, ol_amount);
DROP TABLE tpcc_maintenance_pressure;
