SELECT sum((ol.ol_amount * (ol.ol_quantity + 1))::numeric)
FROM keeninsight_tpcc.order_line AS ol
JOIN keeninsight_tpcc.item AS i ON i.i_id = ol.ol_i_id
WHERE i.i_price > 10;
