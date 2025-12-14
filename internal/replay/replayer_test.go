package replay

import (
	"context"
	"ruc-db-replay/internal/model"
	"testing"
	"time"
)

func TestReplayer_GroupByVxID(t *testing.T) {
	// 创建测试语句，同一个 VxID 的语句应该在同一个事务中
	baseTime := time.Now()
	statements := []*model.SQLStatement{
		// 事务1: vxid=3/100
		{ID: 1, VxID: "3/100", SessionID: "session1", TxID: 100, Operation: "BEGIN", SQL: "BEGIN", Timestamp: baseTime},
		{ID: 2, VxID: "3/100", SessionID: "session1", TxID: 100, Operation: "INSERT", SQL: "INSERT INTO t1 VALUES (1)", Timestamp: baseTime.Add(100 * time.Millisecond)},
		{ID: 3, VxID: "3/100", SessionID: "session1", TxID: 100, Operation: "INSERT", SQL: "INSERT INTO t1 VALUES (2)", Timestamp: baseTime.Add(200 * time.Millisecond)},
		{ID: 4, VxID: "3/100", SessionID: "session1", TxID: 100, Operation: "COMMIT", SQL: "COMMIT", Timestamp: baseTime.Add(300 * time.Millisecond)},
		// 事务2: vxid=3/101
		{ID: 5, VxID: "3/101", SessionID: "session1", TxID: 101, Operation: "BEGIN", SQL: "BEGIN", Timestamp: baseTime.Add(400 * time.Millisecond)},
		{ID: 6, VxID: "3/101", SessionID: "session1", TxID: 101, Operation: "UPDATE", SQL: "UPDATE t1 SET v=1 WHERE id=1", Timestamp: baseTime.Add(500 * time.Millisecond)},
		{ID: 7, VxID: "3/101", SessionID: "session1", TxID: 101, Operation: "COMMIT", SQL: "COMMIT", Timestamp: baseTime.Add(600 * time.Millisecond)},
	}

	config := ReplayConfig{
		Host:        "localhost",
		Port:        5432,
		User:        "test",
		Password:    "test",
		Database:    "test",
		SpeedFactor: 0, // 快速模式
		FastMode:    true,
	}

	replayer := NewReplayer(config, statements)

	// 验证按 session 分组
	if len(replayer.sessionStmts) != 1 {
		t.Errorf("Expected 1 session, got %d", len(replayer.sessionStmts))
	}

	// 验证 session1 有 7 条语句
	sessionStmts := replayer.sessionStmts["session1"]
	if len(sessionStmts) != 7 {
		t.Errorf("Expected 7 statements in session1, got %d", len(sessionStmts))
	}

	// 验证语句按时间排序
	for i := 1; i < len(sessionStmts); i++ {
		if sessionStmts[i].Timestamp.Before(sessionStmts[i-1].Timestamp) {
			t.Error("Statements not sorted by timestamp")
		}
	}

	// 验证统计信息
	stats := replayer.GetStats()
	if stats.TotalStatements != 7 {
		t.Errorf("Expected 7 total statements, got %d", stats.TotalStatements)
	}
}

func TestReplayer_VxIDTransactionBoundary(t *testing.T) {
	// 测试不同 VxID 应该创建新事务
	baseTime := time.Now()
	statements := []*model.SQLStatement{
		{ID: 1, VxID: "3/100", SessionID: "session1", TxID: 100, Operation: "BEGIN", SQL: "BEGIN", Timestamp: baseTime},
		{ID: 2, VxID: "3/100", SessionID: "session1", TxID: 100, Operation: "SELECT", SQL: "SELECT 1", Timestamp: baseTime.Add(100 * time.Millisecond)},
		{ID: 3, VxID: "3/100", SessionID: "session1", TxID: 100, Operation: "COMMIT", SQL: "COMMIT", Timestamp: baseTime.Add(200 * time.Millisecond)},
		// 不同的 VxID，应该是新事务
		{ID: 4, VxID: "3/101", SessionID: "session1", TxID: 101, Operation: "BEGIN", SQL: "BEGIN", Timestamp: baseTime.Add(300 * time.Millisecond)},
		{ID: 5, VxID: "3/101", SessionID: "session1", TxID: 101, Operation: "SELECT", SQL: "SELECT 2", Timestamp: baseTime.Add(400 * time.Millisecond)},
		{ID: 6, VxID: "3/101", SessionID: "session1", TxID: 101, Operation: "COMMIT", SQL: "COMMIT", Timestamp: baseTime.Add(500 * time.Millisecond)},
	}

	config := ReplayConfig{
		Host:        "localhost",
		Port:        5432,
		User:        "test",
		Password:    "test",
		Database:    "test",
		SpeedFactor: 0,
		FastMode:    true,
	}

	replayer := NewReplayer(config, statements)

	// 验证 VxID 分组
	vxidGroups := make(map[string]int)
	for _, stmt := range replayer.sessionStmts["session1"] {
		vxidGroups[stmt.VxID]++
	}

	if len(vxidGroups) != 2 {
		t.Errorf("Expected 2 VxID groups, got %d", len(vxidGroups))
	}

	if vxidGroups["3/100"] != 3 {
		t.Errorf("Expected 3 statements in vxid 3/100, got %d", vxidGroups["3/100"])
	}

	if vxidGroups["3/101"] != 3 {
		t.Errorf("Expected 3 statements in vxid 3/101, got %d", vxidGroups["3/101"])
	}
}

func TestReplayer_StartStop(t *testing.T) {
	// 测试回放器的启动和停止功能
	statements := []*model.SQLStatement{
		{ID: 1, VxID: "3/100", SessionID: "session1", TxID: 100, Operation: "SELECT", SQL: "SELECT 1", Timestamp: time.Now()},
	}

	config := ReplayConfig{
		Host:        "localhost",
		Port:        5432,
		User:        "test",
		Password:    "test",
		Database:    "test",
		SpeedFactor: 0,
		FastMode:    true,
	}

	replayer := NewReplayer(config, statements)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// 启动回放（不会真正执行因为数据库连接会失败，但可以测试启动/停止逻辑）
	err := replayer.Start(ctx)
	if err != nil {
		t.Fatalf("Failed to start replayer: %v", err)
	}

	if !replayer.IsRunning() {
		t.Error("Replayer should be running after Start()")
	}

	// 停止回放
	replayer.Stop()

	if replayer.IsRunning() {
		t.Error("Replayer should not be running after Stop()")
	}
}

func TestReplayer_GenerateReport(t *testing.T) {
	statements := []*model.SQLStatement{
		{ID: 1, VxID: "3/100", SessionID: "session1", TxID: 100, Operation: "SELECT", SQL: "SELECT 1", Timestamp: time.Now()},
		{ID: 2, VxID: "3/100", SessionID: "session1", TxID: 100, Operation: "SELECT", SQL: "SELECT 2", Timestamp: time.Now()},
	}

	config := ReplayConfig{
		Host:     "localhost",
		Port:     5432,
		User:     "test",
		Password: "test",
		Database: "test",
		FastMode: true,
	}

	replayer := NewReplayer(config, statements)
	replayer.stats.StartTime = time.Now()
	replayer.stats.ExecutedStatements = 2
	replayer.stats.SuccessCount = 1
	replayer.stats.FailureCount = 1
	replayer.stats.DivergenceCount = 1
	replayer.stats.RowsAffectedDiff = 1
	replayer.stats.ErrorStateDiff = 0
	replayer.stats.LastUpdateTime = time.Now()

	report := replayer.GenerateReport()

	if report.TotalStatements != 2 {
		t.Errorf("Expected 2 total statements, got %d", report.TotalStatements)
	}

	if report.ExecutedStmts != 2 {
		t.Errorf("Expected 2 executed statements, got %d", report.ExecutedStmts)
	}

	if report.SuccessStmts != 1 {
		t.Errorf("Expected 1 success statement, got %d", report.SuccessStmts)
	}

	if report.FailedStmts != 1 {
		t.Errorf("Expected 1 failed statement, got %d", report.FailedStmts)
	}

	if report.DivergenceCount != 1 {
		t.Errorf("Expected 1 divergence, got %d", report.DivergenceCount)
	}

	// 验证成功率
	expectedSuccessRate := 50.0
	if report.SuccessRate != expectedSuccessRate {
		t.Errorf("Expected success rate %f, got %f", expectedSuccessRate, report.SuccessRate)
	}

	// 验证差异率
	expectedDivergenceRate := 50.0
	if report.DivergenceRate != expectedDivergenceRate {
		t.Errorf("Expected divergence rate %f, got %f", expectedDivergenceRate, report.DivergenceRate)
	}
}

