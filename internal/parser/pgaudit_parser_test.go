package parser

import (
	"strings"
	"testing"
)

func TestPgAuditParser_ParseLine(t *testing.T) {
	parser := NewPgAuditParser()

	testCases := []struct {
		name         string
		line         string
		expectNil    bool
		expectError  bool
		expectedSQL  string
		expectedTxID string
		expectedType string
		expectedOp   string
	}{
		{
			name:         "INSERT with new CSV prefix (QID=0)",
			line:         `test,test,1765626376.161,00000,693d5208.8382,4/12532,0 LOG:  AUDIT: SESSION,28717,1,WRITE,INSERT,,,"INSERT INTO sbtest7 (id, k, c, pad) VALUES ($1, $2, $3, $4)","4992,5026,test-value,pad-data",1`,
			expectNil:    false,
			expectError:  false,
			expectedSQL:  "INSERT INTO sbtest7 (id, k, c, pad) VALUES (4992, 5026, 'test-value', 'pad-data')",
			expectedTxID: "4/12532",
			expectedType: "WRITE",
			expectedOp:   "INSERT",
		},
		{
			name:         "SELECT with new CSV prefix and valid QID",
			line:         `test,test,1765626376.161,00000,693d5208.8382,4/690,123456789 LOG:  AUDIT: SESSION,1,1,READ,SELECT,,,"SELECT * FROM users WHERE id = $1","123",1`,
			expectNil:    false,
			expectError:  false,
			expectedSQL:  "SELECT * FROM users WHERE id = 123",
			expectedTxID: "4/690",
			expectedType: "READ",
			expectedOp:   "SELECT",
		},
		{
			name:         "BEGIN transaction",
			line:         `test,test,1765626376.000,00000,693d5208.8382,4/12534,0 LOG:  AUDIT: SESSION,28716,1,MISC,BEGIN,,,BEGIN,<not logged>,1`,
			expectNil:    false,
			expectError:  false,
			expectedSQL:  "BEGIN",
			expectedTxID: "4/12534",
			expectedType: "MISC",
			expectedOp:   "BEGIN",
		},
		{
			name:         "COMMIT transaction",
			line:         `test,test,1765626376.300,00000,693d5208.8382,4/12534,0 LOG:  AUDIT: SESSION,28719,1,MISC,COMMIT,,,COMMIT,<not logged>,1`,
			expectNil:    false,
			expectError:  false,
			expectedSQL:  "COMMIT",
			expectedTxID: "4/12534",
			expectedType: "MISC",
			expectedOp:   "COMMIT",
		},
		{
			name:        "Non-audit line",
			line:        `2025-12-09 22:03:41.664 CST [12345] LOG:  connection received: host=127.0.0.1 port=54321`,
			expectNil:   true,
			expectError: false,
		},
		{
			name:        "Empty line",
			line:        ``,
			expectNil:   true,
			expectError: false,
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			stmt, err := parser.parseLine(tc.line, "test-task")

			if tc.expectError && err == nil {
				t.Errorf("Expected error but got none")
			}

			if !tc.expectError && err != nil {
				t.Errorf("Unexpected error: %v", err)
			}

			if tc.expectNil {
				if stmt != nil {
					t.Errorf("Expected nil statement but got: %+v", stmt)
				}
				return
			}

			if stmt == nil {
				t.Fatalf("Expected non-nil statement")
			}

			if stmt.TxID != tc.expectedTxID {
				t.Errorf("TxID mismatch: expected %s, got %s", tc.expectedTxID, stmt.TxID)
			}

			if stmt.SQLType != tc.expectedType {
				t.Errorf("SQLType mismatch: expected %s, got %s", tc.expectedType, stmt.SQLType)
			}

			if stmt.Operation != tc.expectedOp {
				t.Errorf("Operation mismatch: expected %s, got %s", tc.expectedOp, stmt.Operation)
			}

			if tc.expectedSQL != "" && stmt.SQLText != tc.expectedSQL {
				t.Errorf("SQL mismatch:\nexpected: %s\ngot:      %s", tc.expectedSQL, stmt.SQLText)
			}

		})
	}
}

func TestPgAuditParser_ParseWithVxID(t *testing.T) {
	parser := NewPgAuditParser()

	// New CSV format log content
	logContent := `test,test,1765626376.000,00000,693d5208.8382,4/100,0 LOG:  AUDIT: SESSION,28716,1,MISC,BEGIN,,,BEGIN,<not logged>,1
test,test,1765626376.100,00000,693d5208.8382,4/100,55555 LOG:  AUDIT: SESSION,28717,1,READ,SELECT,,,"SELECT * FROM users WHERE id = $1","1",1
test,test,1765626376.200,00000,693d5208.8382,4/100,66666 LOG:  AUDIT: SESSION,28718,1,WRITE,UPDATE,,,"UPDATE users SET name = $1 WHERE id = $2","new_name,1",1
test,test,1765626376.300,00000,693d5208.8382,4/100,0 LOG:  AUDIT: SESSION,28719,1,MISC,COMMIT,,,COMMIT,<not logged>,1
test,test,1765626377.000,00000,693d5208.8383,5/200,77777 LOG:  AUDIT: SESSION,28720,1,READ,SELECT,,,"SELECT count(*) FROM orders",<not logged>,1`

	result, err := parser.Parse(strings.NewReader(logContent), "test-task")
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	// 验证解析结果
	if result.TotalLines != 5 {
		t.Errorf("TotalLines mismatch: expected 5, got %d", result.TotalLines)
	}

	if result.ParsedLines != 5 {
		t.Errorf("ParsedLines mismatch: expected 5, got %d", result.ParsedLines)
	}

	if len(result.Units) != 5 {
		t.Errorf("Statements count mismatch: expected 5, got %d", len(result.Units))
	}

	// 验证事务数量 (应该有 2 个事务: vxid=4/100 和 vxid=5/200 -> Now mapped to TxID)
	if len(result.Transactions) != 2 {
		t.Errorf("Transactions count mismatch: expected 2, got %d", len(result.Transactions))
	}

	// 验证事务 4/100 的语句数量
	tx1 := result.Transactions["4/100"]
	if tx1 == nil {
		t.Fatal("Transaction with TxID=4/100 not found")
	}
	if tx1.StmtCount != 4 {
		t.Errorf("Transaction 4/100 statement count mismatch: expected 4, got %d", tx1.StmtCount)
	}
	if !tx1.Committed {
		t.Error("Transaction 4/100 should be marked as committed")
	}
	if tx1.TxID != "4/100" {
		t.Errorf("Transaction TxID mismatch: expected 4/100, got %s", tx1.TxID)
	}

	// 验证事务 5/200 的语句数量
	tx2 := result.Transactions["5/200"]
	if tx2 == nil {
		t.Fatal("Transaction with TxID=5/200 not found")
	}
	if tx2.StmtCount != 1 {
		t.Errorf("Transaction 5/200 statement count mismatch: expected 1, got %d", tx2.StmtCount)
	}

}

func TestPgAuditParser_TransactionGroupingByTxID(t *testing.T) {
	parser := NewPgAuditParser()

	// New CSV format log content
	logContent := `test,test,1765626376.000,00000,session1,3/100,0 LOG:  AUDIT: SESSION,1,1,MISC,BEGIN,,,BEGIN,<not logged>,1
test,test,1765626376.100,00000,session1,3/100,8888 LOG:  AUDIT: SESSION,2,1,WRITE,INSERT,,,"INSERT INTO t1 VALUES ($1)","1",1
test,test,1765626376.200,00000,session1,3/100,8888 LOG:  AUDIT: SESSION,3,1,WRITE,INSERT,,,"INSERT INTO t1 VALUES ($1)","2",1
test,test,1765626376.300,00000,session1,3/100,0 LOG:  AUDIT: SESSION,4,1,MISC,COMMIT,,,COMMIT,<not logged>,1
test,test,1765626377.000,00000,session1,3/101,0 LOG:  AUDIT: SESSION,5,1,MISC,BEGIN,,,BEGIN,<not logged>,1
test,test,1765626377.100,00000,session1,3/101,9999 LOG:  AUDIT: SESSION,6,1,WRITE,UPDATE,,,"UPDATE t1 SET v=$1 WHERE id=$2","new,1",1
test,test,1765626377.200,00000,session1,3/101,0 LOG:  AUDIT: SESSION,7,1,MISC,COMMIT,,,COMMIT,<not logged>,1`

	result, err := parser.Parse(strings.NewReader(logContent), "test-task")
	if err != nil {
		t.Fatalf("Parse failed: %v", err)
	}

	// 应该有 2 个事务
	if len(result.Transactions) != 2 {
		t.Errorf("Expected 2 transactions, got %d", len(result.Transactions))
	}

	// 验证第一个事务 (TxID=3/100)
	tx1 := result.Transactions["3/100"]
	if tx1 == nil {
		t.Fatal("Transaction 3/100 not found")
	}
	if tx1.StmtCount != 4 {
		t.Errorf("Transaction 3/100 should have 4 statements, got %d", tx1.StmtCount)
	}

	// 验证第二个事务 (TxID=3/101)
	tx2 := result.Transactions["3/101"]
	if tx2 == nil {
		t.Fatal("Transaction 3/101 not found")
	}
	if tx2.StmtCount != 3 {
		t.Errorf("Transaction 3/101 should have 3 statements, got %d", tx2.StmtCount)
	}

	// 验证语句在事务内的顺序 (SeqInTx)
	seqByTxID := make(map[string][]int)
	for _, stmt := range result.Units {
		seqByTxID[stmt.TxID] = append(seqByTxID[stmt.TxID], stmt.SeqInTx)
	}

	// 事务 3/100 应该有序号 1, 2, 3, 4
	if len(seqByTxID["3/100"]) != 4 {
		t.Errorf("Transaction 3/100 should have 4 statements")
	}
	for i, seq := range seqByTxID["3/100"] {
		if seq != i+1 {
			t.Errorf("Statement %d in transaction 3/100 has SeqInTx=%d, expected %d", i, seq, i+1)
		}
	}

	// 事务 3/101 应该有序号 1, 2, 3
	if len(seqByTxID["3/101"]) != 3 {
		t.Errorf("Transaction 3/101 should have 3 statements")
	}
	for i, seq := range seqByTxID["3/101"] {
		if seq != i+1 {
			t.Errorf("Statement %d in transaction 3/101 has SeqInTx=%d, expected %d", i, seq, i+1)
		}
	}
}

func TestPgAuditParser_FillParameters(t *testing.T) {
	parser := NewPgAuditParser()

	testCases := []struct {
		name     string
		sql      string
		params   string
		expected string
	}{
		{
			name:     "Integer parameters",
			sql:      "SELECT * FROM users WHERE id = $1",
			params:   "123",
			expected: "SELECT * FROM users WHERE id = 123",
		},
		{
			name:     "String parameters",
			sql:      "INSERT INTO users (name) VALUES ($1)",
			params:   "John",
			expected: "INSERT INTO users (name) VALUES ('John')",
		},
		{
			name:     "Mixed parameters",
			sql:      "UPDATE users SET name = $1, age = $2 WHERE id = $3",
			params:   "Alice,30,5",
			expected: "UPDATE users SET name = 'Alice', age = 30 WHERE id = 5",
		},
		{
			name:     "NULL parameter",
			sql:      "INSERT INTO users (name) VALUES ($1)",
			params:   "NULL",
			expected: "INSERT INTO users (name) VALUES (NULL)",
		},
		{
			name:     "String with special chars",
			sql:      "INSERT INTO logs (msg) VALUES ($1)",
			params:   "It's a test",
			expected: "INSERT INTO logs (msg) VALUES ('It''s a test')",
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			result := parser.fillParameters(tc.sql, tc.params)
			if result != tc.expected {
				t.Errorf("fillParameters mismatch:\nexpected: %s\ngot:      %s", tc.expected, result)
			}
		})
	}
}
