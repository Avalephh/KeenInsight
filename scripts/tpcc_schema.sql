-- Deterministic workload fixture for KeenInsight, NOT a certified TPC-C load.
-- Invoked by prepare_workloads.py in ONE transaction; no DROP/TRUNCATE/reset.
-- Existing schema is an error, not an invitation to overwrite user data.
SELECT pg_advisory_xact_lock(184201, 1);
CREATE SCHEMA keeninsight_tpcc;
SET LOCAL search_path = keeninsight_tpcc, pg_catalog;

CREATE TABLE warehouse (
  w_id integer PRIMARY KEY, w_name text NOT NULL,
  w_street_1 text NOT NULL, w_street_2 text NOT NULL, w_city text NOT NULL,
  w_state char(2) NOT NULL, w_zip char(9) NOT NULL,
  w_tax numeric(4,4) NOT NULL, w_ytd numeric(12,2) NOT NULL
);
CREATE TABLE district (
  d_w_id integer NOT NULL REFERENCES warehouse, d_id integer NOT NULL,
  d_name text NOT NULL, d_street_1 text NOT NULL, d_street_2 text NOT NULL,
  d_city text NOT NULL, d_state char(2) NOT NULL, d_zip char(9) NOT NULL,
  d_tax numeric(4,4) NOT NULL, d_ytd numeric(12,2) NOT NULL,
  d_next_o_id integer NOT NULL, PRIMARY KEY (d_w_id,d_id)
);
CREATE TABLE item (
  i_id integer PRIMARY KEY, i_im_id integer NOT NULL, i_name text NOT NULL,
  i_price numeric(5,2) NOT NULL, i_data text NOT NULL
);
CREATE TABLE customer (
  c_w_id integer NOT NULL, c_d_id integer NOT NULL, c_id integer NOT NULL,
  c_first text NOT NULL, c_middle char(2) NOT NULL, c_last text NOT NULL,
  c_street_1 text NOT NULL, c_street_2 text NOT NULL, c_city text NOT NULL,
  c_state char(2) NOT NULL, c_zip char(9) NOT NULL, c_phone text NOT NULL,
  c_since timestamptz NOT NULL, c_credit char(2) NOT NULL,
  c_credit_lim numeric(12,2) NOT NULL, c_discount numeric(4,4) NOT NULL,
  c_balance numeric(12,2) NOT NULL, c_ytd_payment numeric(12,2) NOT NULL,
  c_payment_cnt integer NOT NULL, c_delivery_cnt integer NOT NULL,
  c_data text NOT NULL, PRIMARY KEY (c_w_id,c_d_id,c_id)
);
CREATE TABLE stock (
  s_w_id integer NOT NULL REFERENCES warehouse,
  s_i_id integer NOT NULL REFERENCES item,
  s_quantity integer NOT NULL, s_ytd integer NOT NULL, s_order_cnt integer NOT NULL,
  s_remote_cnt integer NOT NULL, s_data text NOT NULL,
  s_dist_01 char(24) NOT NULL, s_dist_02 char(24) NOT NULL,
  s_dist_03 char(24) NOT NULL, s_dist_04 char(24) NOT NULL,
  s_dist_05 char(24) NOT NULL, s_dist_06 char(24) NOT NULL,
  s_dist_07 char(24) NOT NULL, s_dist_08 char(24) NOT NULL,
  s_dist_09 char(24) NOT NULL, s_dist_10 char(24) NOT NULL,
  PRIMARY KEY (s_w_id,s_i_id)
);
CREATE TABLE orders (
  o_w_id integer NOT NULL, o_d_id integer NOT NULL, o_id integer NOT NULL,
  o_c_id integer NOT NULL, o_entry_d timestamptz NOT NULL, o_carrier_id integer,
  o_ol_cnt integer NOT NULL, o_all_local integer NOT NULL,
  PRIMARY KEY (o_w_id,o_d_id,o_id)
);
CREATE TABLE new_order (
  no_w_id integer NOT NULL, no_d_id integer NOT NULL, no_o_id integer NOT NULL,
  PRIMARY KEY (no_w_id,no_d_id,no_o_id)
);
CREATE TABLE order_line (
  ol_w_id integer NOT NULL, ol_d_id integer NOT NULL, ol_o_id integer NOT NULL,
  ol_number integer NOT NULL, ol_i_id integer NOT NULL, ol_supply_w_id integer NOT NULL,
  ol_delivery_d timestamptz, ol_quantity integer NOT NULL, ol_amount numeric(6,2) NOT NULL,
  ol_dist_info char(24) NOT NULL, PRIMARY KEY (ol_w_id,ol_d_id,ol_o_id,ol_number)
);
CREATE TABLE history (
  h_id bigserial PRIMARY KEY, h_c_id integer NOT NULL, h_c_d_id integer NOT NULL,
  h_c_w_id integer NOT NULL, h_d_id integer NOT NULL, h_d_w_id integer NOT NULL,
  h_date timestamptz NOT NULL, h_amount numeric(6,2) NOT NULL, h_data text NOT NULL
);
CREATE TABLE load_info (
  id integer PRIMARY KEY, warehouses integer NOT NULL, generator_version text NOT NULL,
  loaded_at timestamptz NOT NULL, row_counts jsonb NOT NULL, total_bytes bigint NOT NULL
);

INSERT INTO warehouse
SELECT w, 'Warehouse '||w, 'Street 1','Street 2','Demo City','CA','123456789',0.1,300000
FROM generate_series(1,:warehouses) w;
INSERT INTO district
SELECT w,d,'District '||d,'Street 1','Street 2','Demo City','CA','123456789',0.1,30000,3001
FROM generate_series(1,:warehouses) w CROSS JOIN generate_series(1,10) d;
INSERT INTO item
SELECT i, i%10000+1, 'Item '||i, (100+i%9900)::numeric/100,
       repeat(md5((i+:seed)::text),2)
FROM generate_series(1,100000) i;
INSERT INTO customer
SELECT w,d,c,'First '||c,'OE','Last '||(c%1000),
       'Street 1','Street 2','Demo City','CA','123456789','1234567890123456',
       TIMESTAMPTZ '2025-01-01 00:00:00+00',
       CASE WHEN c%10=0 THEN 'BC' ELSE 'GC' END,
       50000,(c%5000)::numeric/10000,-10,10,1,0,repeat(md5((c+:seed)::text),12)
FROM generate_series(1,:warehouses) w CROSS JOIN generate_series(1,10) d
     CROSS JOIN generate_series(1,3000) c;
INSERT INTO stock
SELECT w,i,10+(i*17+w*:seed)%91,0,0,0,repeat(md5(i::text),2),
       'DEMO-DIST-01','DEMO-DIST-02','DEMO-DIST-03','DEMO-DIST-04','DEMO-DIST-05',
       'DEMO-DIST-06','DEMO-DIST-07','DEMO-DIST-08','DEMO-DIST-09','DEMO-DIST-10'
FROM generate_series(1,:warehouses) w CROSS JOIN generate_series(1,100000) i;
INSERT INTO orders
SELECT w,d,o,((o*7919+d*17)%3000)+1,
       TIMESTAMPTZ '2025-01-01 00:00:00+00'+o*INTERVAL '1 second',
       CASE WHEN o<=2100 THEN o%10+1 ELSE NULL END,10,1
FROM generate_series(1,:warehouses) w CROSS JOIN generate_series(1,10) d
     CROSS JOIN generate_series(1,3000) o;
INSERT INTO new_order
SELECT w,d,o FROM generate_series(1,:warehouses) w
 CROSS JOIN generate_series(1,10) d CROSS JOIN generate_series(2101,3000) o;
INSERT INTO order_line
SELECT w,d,o,n,(o*37+n*997+d*13+w*101+:seed)%100000+1,w,
       CASE WHEN o<=2100 THEN TIMESTAMPTZ '2025-01-02 00:00:00+00' ELSE NULL END,
       5, CASE WHEN o<=2100 THEN 0 ELSE (o+n)%99999/100.0 END,'TPCC-DEMO'
FROM generate_series(1,:warehouses) w CROSS JOIN generate_series(1,10) d
 CROSS JOIN generate_series(1,3000) o CROSS JOIN generate_series(1,10) n;
INSERT INTO history(h_c_id,h_c_d_id,h_c_w_id,h_d_id,h_d_w_id,h_date,h_amount,h_data)
SELECT c,d,w,d,w,TIMESTAMPTZ '2025-01-01 00:00:00+00',10,'TPCC-DEMO'
FROM generate_series(1,:warehouses) w CROSS JOIN generate_series(1,10) d
 CROSS JOIN generate_series(1,3000) c;
CREATE INDEX customer_last_idx ON customer(c_w_id,c_d_id,c_last,c_first,c_id);
CREATE INDEX orders_customer_idx ON orders(o_w_id,o_d_id,o_c_id,o_id);
CREATE INDEX order_line_item_idx ON order_line(ol_w_id,ol_i_id);
CREATE INDEX stock_item_idx ON stock(s_w_id,s_i_id);
ANALYZE warehouse; ANALYZE district; ANALYZE item; ANALYZE customer;
ANALYZE stock; ANALYZE orders; ANALYZE new_order; ANALYZE order_line; ANALYZE history;
INSERT INTO load_info
SELECT 1,:warehouses,'keeninsight-deterministic-v1-seed-'||:seed,clock_timestamp(),
       jsonb_build_object('warehouse',:warehouses,'district',:warehouses*10,
         'customer',:warehouses*30000,'item',100000,'stock',:warehouses*100000,
         'orders',:warehouses*30000,'new_order',:warehouses*9000,
         'order_line',:warehouses*300000,'history',:warehouses*30000),
       (SELECT sum(pg_total_relation_size(c.oid))
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='keeninsight_tpcc' AND c.relkind='r');
SELECT generator_version, warehouses, row_counts, total_bytes FROM load_info;
