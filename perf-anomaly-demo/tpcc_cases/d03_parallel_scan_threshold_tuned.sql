SET min_parallel_table_scan_size = '1GB';
SELECT sum(ol_amount)
FROM keeninsight_tpcc.order_line;
