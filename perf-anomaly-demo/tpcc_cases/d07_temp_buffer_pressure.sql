CREATE TEMP TABLE tpcc_temp_buffer_pressure AS
SELECT g.n AS copy_no,
       ol_w_id, ol_d_id, ol_o_id, ol_number, ol_amount,
       ol_quantity, ol_i_id, ol_supply_w_id,
       ol_dist_info, repeat(ol_dist_info, 2) AS detail_copy
FROM keeninsight_tpcc.order_line
CROSS JOIN generate_series(1,2) AS g(n);
ANALYZE tpcc_temp_buffer_pressure;
SELECT count(*), sum(ol_amount), sum(length(detail_copy)) FROM tpcc_temp_buffer_pressure;
SELECT count(*), sum(ol_amount), sum(length(detail_copy)) FROM tpcc_temp_buffer_pressure;
SELECT count(*), sum(ol_amount), sum(length(detail_copy)) FROM tpcc_temp_buffer_pressure;
SELECT count(*), sum(ol_amount), sum(length(detail_copy)) FROM tpcc_temp_buffer_pressure;
DROP TABLE tpcc_temp_buffer_pressure;
