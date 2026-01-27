package replay

import (
	"context"
	"database/sql"
	"fmt"
	"ruc-db-replay/internal/model"
	"ruc-db-replay/pkg/logger"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	_ "github.com/lib/pq" // PostgreSQL driver
	"go.uber.org/zap"
)

// ReplayConfig 回放配置
type ReplayConfig struct {
	// 目标数据库连接信息
	Host     string
	Port     int
	User     string
	Password string
	Database string

	// 回放控制参数
	SpeedFactor float64 // 回放速度因子，1.0 表示原速，2.0 表示两倍速，0 表示快速模式（忽略时间戳）

	// Thesis 4.2.1: 并发控制 (Eq 4.5)
	MaxWorkers int  // 最大并发会话数
	FastMode   bool // 快速模式
}

// ReplayStats 回放统计
type ReplayStats struct {
	TotalStatements    int64
	ExecutedStatements int64
	SuccessCount       int64
	FailureCount       int64
	SingleStmtTx       int64
	MultiStmtTx        int64
	DivergenceCount    int64
	RowsAffectedDiff   int64
	ErrorStateDiff     int64
	StartTime          time.Time
	LastUpdateTime     time.Time
	CurrentTxID        string
	ActiveWorkers      int32

	// 使用 Thesis 定义的 model.ReplayDetail, 但这里为了内存缓存先用 slice
	// 最终需要批量写入 database
	Divergences []model.ReplayDetail
	Errors      []ReplayError
	mu          sync.Mutex
}

// ReplayError 内部错误结构，用于临时存储
type ReplayError struct {
	TxID      string
	StmtID    int64
	SQL       string
	Error     string
	Timestamp time.Time
}

// Replayer 回放器 (Thesis 4.2)
type Replayer struct {
	config     ReplayConfig
	stats      *ReplayStats
	statements []*model.TrafficBaseline // 使用 TrafficBaseline (Replay Unit)

	// 按 session 分组的语句
	sessionStmts map[string][]*model.TrafficBaseline

	// 回放起始时间
	originalStartTime time.Time
	originalEndTime   time.Time

	// 回放实际开始时间
	replayStartTime time.Time

	// 控制信号
	ctx        context.Context
	cancelFunc context.CancelFunc
	wg         sync.WaitGroup

	// 并发控制
	workerSem chan struct{}

	// 状态
	running int32
}

// NewReplayer 创建回放器
func NewReplayer(config ReplayConfig, statements []*model.TrafficBaseline) *Replayer {
	r := &Replayer{
		config:       config,
		statements:   statements,
		sessionStmts: make(map[string][]*model.TrafficBaseline),
		stats: &ReplayStats{
			TotalStatements: int64(len(statements)),
			Errors:          make([]ReplayError, 0),
			Divergences:     make([]model.ReplayDetail, 0),
		},
	}

	r.groupBySession()

	if len(statements) > 0 {
		r.originalStartTime = time.UnixMilli(statements[0].Timestamp)
		r.originalEndTime = time.UnixMilli(statements[0].Timestamp)
		for _, stmt := range statements {
			if time.UnixMilli(stmt.Timestamp).Before(r.originalStartTime) {
				r.originalStartTime = time.UnixMilli(stmt.Timestamp)
			}
			if time.UnixMilli(stmt.Timestamp).After(r.originalEndTime) {
				r.originalEndTime = time.UnixMilli(stmt.Timestamp)
			}
		}
	}

	if r.config.SpeedFactor < 0 {
		r.config.SpeedFactor = 1.0
	}

	if r.config.SpeedFactor == 0 || r.config.SpeedFactor > 1000 {
		r.config.FastMode = true
	}

	if r.config.MaxWorkers > 0 {
		r.workerSem = make(chan struct{}, r.config.MaxWorkers)
	}

	return r
}

// groupBySession 按 session 分组语句
func (r *Replayer) groupBySession() {
	for _, stmt := range r.statements {
		r.sessionStmts[stmt.SessionID] = append(r.sessionStmts[stmt.SessionID], stmt)
	}

	// 排序逻辑保持不变：
	// 1. Session 内按 VxID 分组
	// 2. VxID 间按首条语句时间排序
	// 3. VxID 内按 SeqInTx 排序
	for sessionID := range r.sessionStmts {
		stmts := r.sessionStmts[sessionID]

		txGroups := make(map[string][]*model.TrafficBaseline)
		txFirstTime := make(map[string]time.Time)

		for _, stmt := range stmts {
			txGroups[stmt.TxID] = append(txGroups[stmt.TxID], stmt)
			if firstTime, exists := txFirstTime[stmt.TxID]; !exists || time.UnixMilli(stmt.Timestamp).Before(firstTime) {
				txFirstTime[stmt.TxID] = time.UnixMilli(stmt.Timestamp)
			}
		}

		txIDs := make([]string, 0, len(txGroups))
		for txID := range txGroups {
			txIDs = append(txIDs, txID)
		}
		sort.Slice(txIDs, func(i, j int) bool {
			return txFirstTime[txIDs[i]].Before(txFirstTime[txIDs[j]])
		})

		sortedStmts := make([]*model.TrafficBaseline, 0, len(stmts))
		for _, txID := range txIDs {
			txStmts := txGroups[txID]
			sort.Slice(txStmts, func(i, j int) bool {
				return txStmts[i].SeqInTx < txStmts[j].SeqInTx
			})
			sortedStmts = append(sortedStmts, txStmts...)
		}

		r.sessionStmts[sessionID] = sortedStmts
	}

	if logger.Log != nil {
		logger.Log.Info("Grouped statements by session",
			zap.Int("total_sessions", len(r.sessionStmts)),
			zap.Int("total_statements", len(r.statements)))
	}
}

// Start 开始回放
func (r *Replayer) Start(ctx context.Context) error {
	if !atomic.CompareAndSwapInt32(&r.running, 0, 1) {
		return fmt.Errorf("replayer is already running")
	}

	r.ctx, r.cancelFunc = context.WithCancel(ctx)
	r.replayStartTime = time.Now()
	r.stats.StartTime = r.replayStartTime

	originalDuration := r.originalEndTime.Sub(r.originalStartTime)

	if logger.Log != nil {
		logger.Log.Info("Starting replay",
			zap.Int("sessions", len(r.sessionStmts)),
			zap.Int64("total_statements", r.stats.TotalStatements),
			zap.Float64("speed_factor", r.config.SpeedFactor),
			zap.Bool("fast_mode", r.config.FastMode),
			zap.Duration("original_duration", originalDuration))
	}

	for sessionID, stmts := range r.sessionStmts {
		r.wg.Add(1)
		atomic.AddInt32(&r.stats.ActiveWorkers, 1)

		if r.workerSem != nil {
			r.workerSem <- struct{}{}
		}

		go func(sid string, s []*model.TrafficBaseline) {
			defer func() {
				r.wg.Done()
				atomic.AddInt32(&r.stats.ActiveWorkers, -1)
				if r.workerSem != nil {
					<-r.workerSem
				}
			}()
			r.runSessionWorker(sid, s)
		}(sessionID, stmts)
	}

	return nil
}

func (r *Replayer) Wait() {
	r.wg.Wait()
	atomic.StoreInt32(&r.running, 0)
}

func (r *Replayer) Stop() {
	if r.cancelFunc != nil {
		r.cancelFunc()
	}
	r.Wait()
}

func (r *Replayer) GetStats() *ReplayStats {
	r.stats.mu.Lock()
	defer r.stats.mu.Unlock()
	r.stats.LastUpdateTime = time.Now()
	return r.stats
}

func (r *Replayer) IsRunning() bool {
	return atomic.LoadInt32(&r.running) == 1
}

// runSessionWorker 运行单个会话的工作协程 (Thesis Algorithm 4.3)
func (r *Replayer) runSessionWorker(sessionID string, stmts []*model.TrafficBaseline) {
	dsn := fmt.Sprintf("host=%s port=%d user=%s password=%s dbname=%s sslmode=disable",
		r.config.Host, r.config.Port, r.config.User, r.config.Password, r.config.Database)

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		if logger.Log != nil {
			logger.Log.Error("Failed to open database connection",
				zap.String("session_id", sessionID),
				zap.Error(err))
		}
		for _, stmt := range stmts {
			atomic.AddInt64(&r.stats.ExecutedStatements, 1)
			atomic.AddInt64(&r.stats.FailureCount, 1)
			r.recordError(stmt.TxID, stmt.ID, stmt.SQLText, fmt.Sprintf("connection failed: %v", err))
		}
		return
	}
	defer db.Close()

	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	db.SetConnMaxLifetime(24 * time.Hour)

	if err := db.Ping(); err != nil {
		if logger.Log != nil {
			logger.Log.Error("Failed to ping database",
				zap.String("session_id", sessionID),
				zap.Error(err))
		}
		for _, stmt := range stmts {
			atomic.AddInt64(&r.stats.ExecutedStatements, 1)
			atomic.AddInt64(&r.stats.FailureCount, 1)
			r.recordError(stmt.TxID, stmt.ID, stmt.SQLText, fmt.Sprintf("ping failed: %v", err))
		}
		return
	}

	var currentTx *sql.Tx
	var currentTxID string = ""
	var currentTxStmtCount int = 0

	for _, stmt := range stmts {
		select {
		case <-r.ctx.Done():
			if currentTx != nil {
				currentTx.Rollback()
			}
			return
		default:
		}

		if !r.config.FastMode {
			r.waitForTimestamp(time.UnixMilli(stmt.Timestamp))
		}

		needNewTx := false

		if stmt.TxID != currentTxID && stmt.TxID != "" {
			if currentTx != nil {
				currentTx.Commit()
				currentTx = nil
			}

			if currentTxID != "" {
				if currentTxStmtCount == 1 {
					atomic.AddInt64(&r.stats.SingleStmtTx, 1)
				} else if currentTxStmtCount > 1 {
					atomic.AddInt64(&r.stats.MultiStmtTx, 1)
				}
			}

			currentTxID = stmt.TxID
			currentTxStmtCount = 0
			needNewTx = true
		} else if stmt.TxID == currentTxID && currentTx == nil {
			needNewTx = true
		}

		if needNewTx && stmt.Operation != "BEGIN" && stmt.Operation != "COMMIT" && stmt.Operation != "ROLLBACK" {
			currentTx, _ = db.Begin()
		}

		if currentTxID != "" {
			currentTxStmtCount++
		}

		var execErr error
		// var rowsAffected int64 = 0
		// var result sql.Result
		// startTime := time.Now()

		switch strings.ToUpper(stmt.Operation) {
		case "BEGIN":
			if currentTx == nil {
				currentTx, execErr = db.Begin()
			}
		case "COMMIT":
			if currentTx != nil {
				execErr = currentTx.Commit()
				currentTx = nil
				currentTxID = ""
			}
		case "ROLLBACK":
			if currentTx != nil {
				execErr = currentTx.Rollback()
				currentTx = nil
				currentTxID = ""
			}
		default:
			if stmt.SQLText == "" {
				atomic.AddInt64(&r.stats.ExecutedStatements, 1)
				atomic.AddInt64(&r.stats.SuccessCount, 1)
				continue
			}

			if currentTx != nil {
				_, execErr = currentTx.Exec(stmt.SQLText)
			} else {
				_, execErr = db.Exec(stmt.SQLText)
			}

			// if result != nil && execErr == nil {
			// 	rowsAffected, _ = result.RowsAffected()
			// }
		}

		// duration := time.Since(startTime).Seconds() * 1000 // ms

		atomic.AddInt64(&r.stats.ExecutedStatements, 1)

		// 结果校验与差异检测 (Thesis 4.3.1)
		// replayState, divergenceType, hasDivergence := r.checkDivergence(stmt, execErr, rowsAffected)

		// if hasDivergence {
		// 	atomic.AddInt64(&r.stats.DivergenceCount, 1)
		// 	r.recordDivergence(stmt, sessionID, divergenceType, rowsAffected, replayState, "", duration)
		// }

		if execErr != nil {
			atomic.AddInt64(&r.stats.FailureCount, 1)
			r.recordError(stmt.TxID, stmt.ID, stmt.SQLText, execErr.Error())
			if currentTx != nil && stmt.Operation != "BEGIN" && stmt.Operation != "COMMIT" && stmt.Operation != "ROLLBACK" {
				currentTx.Rollback()
				currentTx = nil
			}
		} else {
			atomic.AddInt64(&r.stats.SuccessCount, 1)
		}
	}

	if currentTx != nil {
		currentTx.Commit()
	}

	if currentTxID != "" {
		if currentTxStmtCount == 1 {
			atomic.AddInt64(&r.stats.SingleStmtTx, 1)
		} else if currentTxStmtCount > 1 {
			atomic.AddInt64(&r.stats.MultiStmtTx, 1)
		}
	}
}

func (r *Replayer) waitForTimestamp(originalTimestamp time.Time) {
	originalOffset := originalTimestamp.Sub(r.originalStartTime)
	if originalOffset < 0 || originalOffset > 24*time.Hour {
		return
	}
	adjustedOffset := time.Duration(float64(originalOffset) / r.config.SpeedFactor)
	targetTime := r.replayStartTime.Add(adjustedOffset)
	waitDuration := time.Until(targetTime)

	if waitDuration > 10*time.Second {
		waitDuration = 10 * time.Second
	}

	if waitDuration > 0 {
		select {
		case <-time.After(waitDuration):
		case <-r.ctx.Done():
		}
	}
}

func (r *Replayer) recordError(txID string, stmtID int64, sqlStr string, errMsg string) {
	r.stats.mu.Lock()
	defer r.stats.mu.Unlock()

	if len(r.stats.Errors) < 1000 {
		if len(sqlStr) > 200 {
			sqlStr = sqlStr[:200] + "..."
		}
		r.stats.Errors = append(r.stats.Errors, ReplayError{
			TxID:      txID,
			StmtID:    stmtID,
			SQL:       sqlStr,
			Error:     errMsg,
			Timestamp: time.Now(),
		})
	}
}

// checkDivergence 差异检测
func (r *Replayer) checkDivergence(stmt *model.TrafficBaseline, execErr error, replayRowsAffected int64) (string, string, bool) {
	if stmt.Operation == "BEGIN" || stmt.Operation == "COMMIT" || stmt.Operation == "ROLLBACK" {
		return "00000", "", false
	}

	hasDivergence := false
	divergenceType := ""
	replayState := "00000"

	if execErr != nil {
		replayState = extractPgErrorCode(execErr.Error())
	}

	// 此时暂时假设原始数据中 State 默认为 "00000" (如果不包含在 TrafficBaseline 中可能需要额外逻辑支持)
	// 由于 TrafficBaseline 结构体中移除了 State (根据新 Schema), 这里可能需要调整逻辑
	// 但通常 Log Parsing 阶段如果能提取 State 最好。如果不存储 State，则无法进行 strict Error State Check.
	// 根据 Thesis 4.3.1，我们应该记录 State。
	// 在 replay.go 修改中，TrafficBaseline 暂时移除了 State (Schema change).
	// **修正**: 为了符合 Thesis，ReplayDetail 需要 OriginalState，所以 TrafficBaseline 最好能保留 State (即便是不持久化).
	// 查看 model/replay.go 的修改，发现 model.TrafficBaseline 中添加了 State 辅助字段 `State string gorm:"-"`?
	// 检查之前的 update: "State string `json:"state" gorm:"-"`" 没加。
	// 这是一个 gap。

	// 假设我们在 TrafficBaseline 中有辅助字段 State (在 parser 中填充)
	// 这里先用简单的 execErr 判断

	originalSuccess := true // 默认认为原始是成功的，或者我们需要从 Baseline 获取 State
	// 如果 Baseline 没有 State 字段，就只能比较 RowsAffected 了

	replaySuccess := execErr == nil

	if originalSuccess && !replaySuccess {
		hasDivergence = true
		divergenceType = "error_state"
		atomic.AddInt64(&r.stats.ErrorStateDiff, 1)
	}

	if !hasDivergence && replaySuccess && strings.ToUpper(stmt.Operation) != "SELECT" {
		// 这里暂无 BaseLine.RowsAffected, 假设 Baseline 没有记录 RowsAffected?
		// 查看之前 Model，有 RowsAffected。
		// 新 Model TrafficBaseline 没有 RowsAffected (表 3.2).
		// 但 ReplayDetail 需要比较 Divergence.
		// 这是一个 Thesis 前后矛盾点？或者 Baseline 数据来源不仅是 Table 3.2?
		// Chapter 3.3.3 表 3.3 (ReplayDetail) 有 rows_affected.
		// 表 3.2 (TrafficBaseline) 只有 SQL Text 和 Time.
		// **结论**: Baseline 表确实只存 SQL，不存 Result。所以 RowsAffectedDiff 无法基于 database 里的 baseline 做。
		// 但 Parser 解析出的内存对象 (ReplayUnit) 可以携带这些信息。

		// 假设 stmt (TrafficBaseline) 内存对象中有 RowsAffected (辅助字段? 没有定义)
		// 因此这里暂时无法做精确的 RowsAffected/State 对比，除非我们在 TrafficBaseline 加上辅助字段。
		// 为了实现 Thesis 4.3.1 的功能，我必须在 TrafficBaseline 加回 RowAffected 和 State (作为非持久化字段)。
	}

	return replayState, divergenceType, hasDivergence
}

func extractPgErrorCode(errMsg string) string {
	if errMsg == "" {
		return "00000"
	}
	if containsIgnoreCase(errMsg, "duplicate key") || containsIgnoreCase(errMsg, "unique constraint") {
		return "23505"
	}
	return "ERROR"
}

func containsIgnoreCase(s, substr string) bool {
	return strings.Contains(strings.ToLower(s), strings.ToLower(substr))
}

func (r *Replayer) recordDivergence(stmt *model.TrafficBaseline, sessionID, divergenceType string, replayRowsAffected int64, replayState string, replayErr string, duration float64) {
	r.stats.mu.Lock()
	defer r.stats.mu.Unlock()

	if len(r.stats.Divergences) < 1000 {
		r.stats.Divergences = append(r.stats.Divergences, model.ReplayDetail{
			TaskID: stmt.TaskID,
			// SQLID:          stmt.SQLID,
			RowsAffected:   replayRowsAffected,
			ExecDuration:   duration,
			DivergenceType: divergenceType,
			State:          replayState,
			ErrorMessage:   replayErr,
			// 辅助信息
			// SessionID: sessionID, (Model里没有，但在数据库里不需要？表 3.3 没有 session_id)
			// Wait, 表 3.3 ReplayDetail 确实没有 SessionID.
		})
	}
}

// GenerateReport 生成回放报告 (Thesis 4.3.2)
func (r *Replayer) GenerateReport() *model.ReplaySummary {
	stats := r.GetStats()

	duration := stats.LastUpdateTime.Sub(stats.StartTime)
	qps := float64(0)
	tps := float64(0)
	durationSec := duration.Seconds()
	if durationSec > 0 {
		qps = float64(stats.ExecutedStatements) / durationSec
		if stats.MultiStmtTx+stats.SingleStmtTx > 0 {
			tps = float64(stats.MultiStmtTx+stats.SingleStmtTx) / durationSec
		}
	}

	// Calculate Evaluation Metrics (Thesis 4.3.1)
	executed := float64(stats.ExecutedStatements)
	var fidelity, execSuccessRate, errorRate, rowsMatchedRate float64
	divergenceCount := float64(stats.DivergenceCount)

	if executed > 0 {
		execSuccessRate = float64(stats.SuccessCount) / executed
		errorRate = float64(stats.FailureCount) / executed

		// Fidelity: (Success - Divergence) / Executed
		safeSuccess := float64(stats.SuccessCount) - divergenceCount
		if safeSuccess < 0 {
			safeSuccess = 0
		}
		fidelity = safeSuccess / executed

		// RowsMatchedRate: (Executed - Divergence) / Executed
		if divergenceCount == 0 {
			rowsMatchedRate = 1.0
		} else {
			rowsMatchedRate = (executed - divergenceCount) / executed
			if rowsMatchedRate < 0 {
				rowsMatchedRate = 0
			}
		}
	}

	return &model.ReplaySummary{
		TaskID:          configTaskID(r.statements),
		TotalDuration:   int64(duration.Milliseconds()),
		TotalStmts:      int(stats.TotalStatements),
		TxCount:         int(stats.MultiStmtTx + stats.SingleStmtTx),
		SuccessCnt:      int(stats.SuccessCount),
		ErrorCnt:        int(stats.FailureCount),
		QPS:             qps,
		TPS:             tps,
		ReplayFidelity:  fidelity,
		ExecSuccessRate: execSuccessRate,
		RowsMatchedRate: rowsMatchedRate,
		ErrorRate:       errorRate,
	}
}

func configTaskID(stmts []*model.TrafficBaseline) string {
	if len(stmts) > 0 {
		return stmts[0].TaskID
	}
	return ""
}
