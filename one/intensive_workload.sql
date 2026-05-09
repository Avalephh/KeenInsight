-- Custom intensive workload for pgbench that stresses memory and I/O
\set aid random(1, 100000 * :scale)
\set bid random(1, 1 * :scale)
\set tid random(1, 10 * :scale)
\set delta random(-5000, 5000)
\set random_page_cost random(1, 5)
\set effective_cache_size random(1, 10)

BEGIN;
-- Update with complex where clause
UPDATE pgbench_accounts SET abalance = abalance + :delta 
WHERE aid IN (SELECT aid FROM pgbench_accounts WHERE bid = :bid ORDER BY random() LIMIT 10);

-- Expensive SELECT with ORDER BY (forces sort in work_mem)
SELECT a.aid, a.abalance, t.tid, t.tbalance, b.bid, b.bbalance
FROM pgbench_accounts a
JOIN pgbench_tellers t ON a.bid = t.bid
JOIN pgbench_branches b ON a.bid = b.bid
WHERE a.bid = :bid
ORDER BY a.abalance DESC
LIMIT 50;

-- Another update
UPDATE pgbench_tellers SET tbalance = tbalance + :delta WHERE tid = :tid;

-- Insert with timestamp
INSERT INTO pgbench_history (tid, bid, aid, delta, mtime) 
VALUES (:tid, :bid, :aid, :delta, CURRENT_TIMESTAMP);

COMMIT;
