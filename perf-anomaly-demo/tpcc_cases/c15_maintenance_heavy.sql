\timing on
BEGIN;
CREATE INDEX tpcc_case15_a_idx ON keeninsight_tpcc.order_line (ol_amount, ol_w_id, ol_d_id, ol_o_id, ol_number);
CREATE INDEX tpcc_case15_b_idx ON keeninsight_tpcc.order_line (ol_i_id, ol_quantity, ol_supply_w_id, ol_amount);
CREATE INDEX tpcc_case15_c_idx ON keeninsight_tpcc.order_line (ol_delivery_d, ol_amount, ol_i_id);
CREATE INDEX tpcc_case15_d_idx ON keeninsight_tpcc.order_line (ol_w_id, ol_d_id, ol_o_id, ol_amount, ol_dist_info);
ROLLBACK;
