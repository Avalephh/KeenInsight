\timing on
SET maintenance_work_mem = '256MB';
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
