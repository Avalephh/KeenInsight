\timing on
BEGIN;
CREATE INDEX tpcc_case6_probe_idx
  ON keeninsight_tpcc.order_line (ol_i_id, ol_amount);
ROLLBACK;

BEGIN;
CREATE INDEX tpcc_case6_probe_idx
  ON keeninsight_tpcc.order_line (ol_i_id, ol_amount);
ROLLBACK;

BEGIN;
CREATE INDEX tpcc_case6_probe_idx
  ON keeninsight_tpcc.order_line (ol_i_id, ol_amount);
ROLLBACK;

BEGIN;
CREATE INDEX tpcc_case6_probe_idx
  ON keeninsight_tpcc.order_line (ol_i_id, ol_amount);
ROLLBACK;
