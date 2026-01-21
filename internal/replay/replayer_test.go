package replay

import (
	"context"
	"ruc-db-replay/internal/model"
	"testing"
	"time"
)

func TestReplayer_GroupByTxID(t *testing.T) {
	// 创建测试语句，同一个 TxID 的语句应该在同一个事务中
	baseTime := time.Now()
	statements := []*model.TrafficBaseline{
		// 事务1: TxID=100
		{ID: 1, SessionID: "session1", TxID: "100", Operation: "BEGIN", SQLText: "BEGIN", OriginTime: baseTime},
		{ID: 2, SessionID: "session1", TxID: "100", Operation: "INSERT", SQLText: "INSERT INTO t1 VALUES (1)", OriginTime: baseTime.Add(100 * time.Millisecond)},
		{ID: 3, SessionID: "session1", TxID: "100", Operation: "INSERT", SQLText: "INSERT INTO t1 VALUES (2)", OriginTime: baseTime.Add(200 * time.Millisecond)},
		{ID: 4, SessionID: "session1", TxID: "100", Operation: "COMMIT", SQLText: "COMMIT", OriginTime: baseTime.Add(300 * time.Millisecond)},
		// 事务2: TxID=101
		{ID: 5, SessionID: "session1", TxID: "101", Operation: "BEGIN", SQLText: "BEGIN", OriginTime: baseTime.Add(400 * time.Millisecond)},
		{ID: 6, SessionID: "session1", TxID: "101", Operation: "UPDATE", SQLText: "UPDATE t1 SET v=1 WHERE id=1", OriginTime: baseTime.Add(500 * time.Millisecond)},
		{ID: 7, SessionID: "session1", TxID: "101", Operation: "COMMIT", SQLText: "COMMIT", OriginTime: baseTime.Add(600 * time.Millisecond)},
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
		if sessionStmts[i].OriginTime.Before(sessionStmts[i-1].OriginTime) {
			t.Error("Statements not sorted by timestamp")
		}
	}

	// 验证统计信息
	stats := replayer.GetStats()
	if stats.TotalStatements != 7 {
		t.Errorf("Expected 7 total statements, got %d", stats.TotalStatements)
	}
}

func TestReplayer_TxIDTransactionBoundary(t *testing.T) {
	// 测试不同 TxID 应该创建新事务
	baseTime := time.Now()
	statements := []*model.TrafficBaseline{
		{ID: 1, SessionID: "session1", TxID: "100", Operation: "BEGIN", SQLText: "BEGIN", OriginTime: baseTime},
		{ID: 2, SessionID: "session1", TxID: "100", Operation: "SELECT", SQLText: "SELECT 1", OriginTime: baseTime.Add(100 * time.Millisecond)},
		{ID: 3, SessionID: "session1", TxID: "100", Operation: "COMMIT", SQLText: "COMMIT", OriginTime: baseTime.Add(200 * time.Millisecond)},
		// 不同的 TxID，应该是新事务
		{ID: 4, SessionID: "session1", TxID: "101", Operation: "BEGIN", SQLText: "BEGIN", OriginTime: baseTime.Add(300 * time.Millisecond)},
		{ID: 5, SessionID: "session1", TxID: "101", Operation: "SELECT", SQLText: "SELECT 2", OriginTime: baseTime.Add(400 * time.Millisecond)},
		{ID: 6, SessionID: "session1", TxID: "101", Operation: "COMMIT", SQLText: "COMMIT", OriginTime: baseTime.Add(500 * time.Millisecond)},
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

	// 验证 TxID (mocked as vxid behavior) 分组
	// Note: internal logic now uses TxID to map groups.
	txGroups := make(map[string]int)
	for _, stmt := range replayer.sessionStmts["session1"] {
		txGroups[stmt.TxID]++
	}

	if len(txGroups) != 2 {
		t.Errorf("Expected 2 TxID groups, got %d", len(txGroups))
	}

	if txGroups["100"] != 3 {
		t.Errorf("Expected 3 statements in TxID 100, got %d", txGroups["100"])
	}

	if txGroups["101"] != 3 {
		t.Errorf("Expected 3 statements in TxID 101, got %d", txGroups["101"])
	}
}

func TestReplayer_StartStop(t *testing.T) {
	// 测试回放器的启动和停止功能
	statements := []*model.TrafficBaseline{
		{ID: 1, SessionID: "session1", TxID: "100", Operation: "SELECT", SQLText: "SELECT 1", OriginTime: time.Now()},
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
	statements := []*model.TrafficBaseline{
		{ID: 1, SessionID: "session1", TxID: "100", Operation: "SELECT", SQLText: "SELECT 1", OriginTime: time.Now()},
		{ID: 2, SessionID: "session1", TxID: "100", Operation: "SELECT", SQLText: "SELECT 2", OriginTime: time.Now()},
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
	// replayer.stats.RowsAffectedDiff = 1
	// replayer.stats.ErrorStateDiff = 0
	replayer.stats.LastUpdateTime = time.Now()

	report := replayer.GenerateReport()

	if report.TotalStmts != 2 {
		t.Errorf("Expected 2 total statements, got %d", report.TotalStmts)
	}

	// ReplaySummary structure doesn't track ExecutedStmts directly (it's implicit in Success+Error)
	// But let's check what we have.
	// report.ExecutedStmts is gone.
	if report.SuccessCnt+report.ErrorCnt != 2 {
		t.Errorf("Expected 2 executed statements, got %d", report.SuccessCnt+report.ErrorCnt)
	}

	if report.SuccessCnt != 1 {
		t.Errorf("Expected 1 success statement, got %d", report.SuccessCnt)
	}

	if report.ErrorCnt != 1 {
		t.Errorf("Expected 1 failed statement, got %d", report.ErrorCnt)
	}

	// report.DivergenceCount is gone from summary model.
	// We can check other available fields if any.
	// SuccessRate/DivergenceRate might be added to summary or transient.
	// Model ReplaySummary has QPS, TPS, but not SuccessRate/DivergenceRate explicitly in struct?
	// Checking model:
	// type ReplaySummary struct {
	// 	ID            int64
	// 	TaskID        string
	// 	Round         int
	// 	TotalDuration int64
	// 	TotalStmts    int
	// 	TxCount       int
	// 	SuccessCnt    int
	// 	ErrorCnt      int
	// 	QPS           float64
	// 	TPS           float64
	// }
	// So SuccessRate/DivergenceRate are NOT in model.
	// We should remove those checks or calculate them in test to verify logic if method returned them (it returns struct).
	// Since struct doesn't have them, we remove the checks.
}
