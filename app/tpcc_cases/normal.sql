\set w_id random(1,5)
\set d_id random(1,10)
\set c_id random(1,3000)
\set o_id random(1,3000)
\set i_id random(1,100000)

SELECT c_id, c_balance
FROM keeninsight_tpcc.customer
WHERE c_w_id = :w_id AND c_d_id = :d_id AND c_id = :c_id;

SELECT o_id, o_entry_d, o_carrier_id
FROM keeninsight_tpcc.orders
WHERE o_w_id = :w_id AND o_d_id = :d_id AND o_id = :o_id;

SELECT ol_amount, ol_quantity
FROM keeninsight_tpcc.order_line
WHERE ol_w_id = :w_id AND ol_d_id = :d_id AND ol_o_id = :o_id
ORDER BY ol_number;

SELECT s_quantity, s_ytd, s_order_cnt
FROM keeninsight_tpcc.stock
WHERE s_w_id = :w_id AND s_i_id = :i_id;
