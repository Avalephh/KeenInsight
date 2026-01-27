package e2e

import (
	"database/sql"
	"fmt"
	"io"
	"os"
	"ruc-db-replay/internal/model"
	"ruc-db-replay/internal/parser"
	"strings"
	"testing"
	"time"

	_ "github.com/lib/pq"
)

// TestLogToReplayDataFlow simulates the end-to-end flow from Log extraction to Replay Data generation.
func TestLogToReplayDataFlow(t *testing.T) {
	// 1. Source Data (Simulated Logs from PostgreSQL with new config)
	// Config: log_line_prefix = '%u,%d,%n,%e,%c,%v,%Q '
	// Format: user,db,epoch,state,session,vxid,qid LOG:  AUDIT: ...
	rawLogs := `postgres,mydb,1732100000.000,00000,sess1,3/100,0 LOG:  AUDIT: SESSION,1,1,MISC,BEGIN,,,BEGIN,<not logged>,1
postgres,mydb,1732100000.100,00000,sess1,3/100,1001 LOG:  AUDIT: SESSION,2,1,WRITE,INSERT,,,"INSERT INTO t1 VALUES ($1)","10",1
postgres,mydb,1732100000.200,00000,sess1,3/100,1002 LOG:  AUDIT: SESSION,3,1,READ,SELECT,,,"SELECT * FROM t1 WHERE id=$1","10",1
postgres,mydb,1732100000.300,00000,sess1,3/100,0 LOG:  AUDIT: SESSION,4,1,MISC,COMMIT,,,COMMIT,<not logged>,1`

	// 2. Parse Logs
	p := parser.NewPgAuditParser()
	result, err := p.Parse(strings.NewReader(rawLogs), "e2e_task")
	if err != nil {
		t.Fatalf("Failed to parse logs: %v", err)
	}

	// 3. Verify Intermediate Results (Replay Units)

	// Check total units
	if len(result.Units) != 4 {
		t.Errorf("Expected 4 replay units, got %d", len(result.Units))
	}

	// Check Transaction grouping
	if len(result.Transactions) != 1 {
		t.Errorf("Expected 1 transaction, got %d", len(result.Transactions))
	}

	txID := "3/100"
	tx, exists := result.Transactions[txID]
	if !exists {
		t.Fatalf("Transaction %s not found", txID)
	}
	if tx.StmtCount != 4 {
		t.Errorf("Expected 4 statements in transaction, got %d", tx.StmtCount)
	}
	if !tx.Committed {
		t.Error("Transaction should be committed")
	}

	// Check Specific Units content
	// Unit 1: INSERT
	insertUnit := result.Units[1]
	if insertUnit.Operation != "INSERT" {
		t.Errorf("Unit 1 op mismatch: %s", insertUnit.Operation)
	}
	expectedSQL := "INSERT INTO t1 VALUES (10)"
	if insertUnit.SQLText != expectedSQL {
		t.Errorf("Unit 1 SQLText mismatch: expected '%s', got '%s'", expectedSQL, insertUnit.SQLText)
	}

	// Unit 2: SELECT
	selectUnit := result.Units[2]
	if selectUnit.Operation != "SELECT" {
		t.Errorf("Unit 2 op mismatch: %s", selectUnit.Operation)
	}
}

// TestRealDBReplay performs a real integration test against a local PostgreSQL instance.
func TestRealDBReplay(t *testing.T) {
	// 1. Setup DB Connection
	connStr := "postgres://test:test1234@localhost:5432/postgres?sslmode=disable"
	db, err := sql.Open("postgres", connStr)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	if err := db.Ping(); err != nil {
		t.Skipf("Skipping real DB test: failed to ping DB: %v", err)
	}

	// Create test table
	_, err = db.Exec("CREATE TABLE IF NOT EXISTS e2e_orders (id SERIAL PRIMARY KEY, amount INT, data TEXT)")
	if err != nil {
		t.Fatalf("Failed to create table: %v", err)
	}
	defer func() {
		// Clean up
		db.Exec("DROP TABLE e2e_orders")
	}()

	// 2. Prepare for Efficient Log Reading
	logFilePath := "/opt/homebrew/var/log/postgresql@18.log"
	logFile, err := os.Open(logFilePath)
	if err != nil {
		t.Skipf("Skipping log check: cannot open log file: %v", err)
	}
	// Seek to end of file to ignore past logs
	startOffset, err := logFile.Seek(0, 2)
	if err != nil {
		t.Fatalf("Failed to seek log file: %v", err)
	}
	logFile.Close()

	// 3. Execute Meaningful Transaction (Simulate Service Request)
	// Transaction: BEGIN -> INSERT -> UPDATE -> COMMIT
	uniqueTag := fmt.Sprintf("tag_%d", time.Now().UnixNano())

	tx, err := db.Begin()
	if err != nil {
		t.Fatalf("Failed to begin tx: %v", err)
	}

	_, err = tx.Exec("INSERT INTO e2e_orders (amount, data) VALUES ($1, $2)", 100, uniqueTag)
	if err != nil {
		tx.Rollback()
		t.Fatalf("Failed to insert: %v", err)
	}

	_, err = tx.Exec("UPDATE e2e_orders SET amount = 200 WHERE data = $1", uniqueTag)
	if err != nil {
		tx.Rollback()
		t.Fatalf("Failed to update: %v", err)
	}

	if err := tx.Commit(); err != nil {
		t.Fatalf("Failed to commit: %v", err)
	}

	// Wait for logs to flush
	time.Sleep(1 * time.Second)

	// 4. Extract New Logs to Temp File
	// We do this to use ParseStream (High Level API) and strictly limit parsing to relevant logs
	f, err := os.Open(logFilePath)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()

	_, err = f.Seek(startOffset, 0)
	if err != nil {
		t.Fatal(err)
	}

	tempLogFile, err := os.CreateTemp("", "e2e_test_*.log")
	if err != nil {
		t.Fatal(err)
	}
	defer os.Remove(tempLogFile.Name())

	// Copy from startOffset to end
	if _, err := io.Copy(tempLogFile, f); err != nil {
		t.Fatal(err)
	}
	tempLogFile.Close()

	// 5. Use High-Level Interface (ParseStream)
	p := parser.NewPgAuditParser()

	var capturedUnits []*parser.ReplayUnit
	callback := func(units []*parser.ReplayUnit, txs map[string]*model.Transaction) error {
		capturedUnits = append(capturedUnits, units...)
		return nil
	}

	_, err = p.ParseStream(tempLogFile.Name(), "e2e_task_stream", 1000, callback)
	if err != nil {
		t.Fatalf("ParseStream failed: %v", err)
	}

	// 6. Verify Logic
	foundTx := false
	var txUnits []*parser.ReplayUnit

	for _, unit := range capturedUnits {
		if strings.Contains(unit.SQLText, uniqueTag) {
			txUnits = append(txUnits, unit)
			foundTx = true
		}
	}

	if !foundTx {
		t.Logf("Parsed %d units from tail log", len(capturedUnits))
		t.Errorf("Transaction with tag %s not found in parsed logs", uniqueTag)
	}

	// Verify operations in the transaction
	opsFound := make(map[string]bool)
	for _, u := range txUnits {
		opsFound[u.Operation] = true
	}

	if !opsFound["INSERT"] {
		t.Error("INSERT operation not captured")
	}
	if !opsFound["UPDATE"] {
		t.Error("UPDATE operation not captured")
	}
}
