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
	MaxWorkers  int     // 最大并发会话数（0 表示不限制）
	FastMode    bool    // 快速模式：忽略时间戳，尽可能快地执行
}

// ReplayStats 回放统计
type ReplayStats struct {
	TotalStatements    int64
	ExecutedStatements int64
	SuccessCount       int64
	FailureCount       int64
	SingleStmtTx       int64 // 单语句事务数
	MultiStmtTx        int64 // 多语句事务数
	DivergenceCount    int64 // 差异总数
	RowsAffectedDiff   int64 // 影响行数不同
	ErrorStateDiff     int64 // 错误状态不同
	StartTime          time.Time
	LastUpdateTime     time.Time
	CurrentTxID        int64
	CurrentVxID        string // 当前虚拟事务ID
	ActiveWorkers      int32
	Errors             []ReplayError
	Divergences        []ReplayDivergence
	mu                 sync.Mutex
}

// ReplayError 回放错误
type ReplayError struct {
	TxID      int64
	VxID      string // 虚拟事务ID
	StmtID    int64
	SQL       string
	Error     string
	Timestamp time.Time
}

// ReplayDivergence 回放差异
type ReplayDivergence struct {
	StmtID               int64
	TxID                 int64
	VxID                 string // 虚拟事务ID
	SessionID            string
	SQL                  string
	DivergenceType       string // rows_affected, error_state
	OriginalRowsAffected int
	ReplayRowsAffected   int64
	OriginalState        string
	ReplayState          string
	OriginalError        string
	ReplayError          string
	Timestamp            time.Time
}

// Replayer 回放器
type Replayer struct {
	config     ReplayConfig
	stats      *ReplayStats
	statements []*model.SQLStatement

	// 按 session 分组的语句
	sessionStmts map[string][]*model.SQLStatement

	// 回放起始时间（原始日志中最早的时间戳）
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
func NewReplayer(config ReplayConfig, statements []*model.SQLStatement) *Replayer {
	r := &Replayer{
		config:       config,
		statements:   statements,
		sessionStmts: make(map[string][]*model.SQLStatement),
		stats: &ReplayStats{
			TotalStatements: int64(len(statements)),
			Errors:          make([]ReplayError, 0),
			Divergences:     make([]ReplayDivergence, 0),
		},
	}

	// 按 session 分组语句
	r.groupBySession()

	// 找到最早和最晚的时间戳
	if len(statements) > 0 {
		r.originalStartTime = statements[0].Timestamp
		r.originalEndTime = statements[0].Timestamp
		for _, stmt := range statements {
			if stmt.Timestamp.Before(r.originalStartTime) {
				r.originalStartTime = stmt.Timestamp
			}
			if stmt.Timestamp.After(r.originalEndTime) {
				r.originalEndTime = stmt.Timestamp
			}
		}
	}

	if r.config.SpeedFactor < 0 {
		r.config.SpeedFactor = 1.0
	}

	// 如果 SpeedFactor 为 0 或非常大，启用快速模式
	if r.config.SpeedFactor == 0 || r.config.SpeedFactor > 1000 {
		r.config.FastMode = true
	}

	// 设置并发控制
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

	// 每个 session 内部排序：
	// 1. 首先按 VxID 分组，确保同一事务的语句连续执行
	// 2. 事务之间按第一条语句的时间戳排序
	// 3. 事务内按 SeqInTx 排序
	for sessionID := range r.sessionStmts {
		stmts := r.sessionStmts[sessionID]

		// 按 VxID 分组
		vxidGroups := make(map[string][]*model.SQLStatement)
		vxidFirstTime := make(map[string]time.Time)

		for _, stmt := range stmts {
			vxidGroups[stmt.VxID] = append(vxidGroups[stmt.VxID], stmt)
			// 记录每个事务的第一条语句时间
			if firstTime, exists := vxidFirstTime[stmt.VxID]; !exists || stmt.Timestamp.Before(firstTime) {
				vxidFirstTime[stmt.VxID] = stmt.Timestamp
			}
		}

		// 获取所有 VxID 并按第一条语句时间排序
		vxids := make([]string, 0, len(vxidGroups))
		for vxid := range vxidGroups {
			vxids = append(vxids, vxid)
		}
		sort.Slice(vxids, func(i, j int) bool {
			return vxidFirstTime[vxids[i]].Before(vxidFirstTime[vxids[j]])
		})

		// 重建语句列表：按事务顺序，每个事务内按 SeqInTx 排序
		sortedStmts := make([]*model.SQLStatement, 0, len(stmts))
		for _, vxid := range vxids {
			txStmts := vxidGroups[vxid]
			// 事务内按 SeqInTx 排序
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
			zap.Duration("original_duration", originalDuration),
			zap.Time("original_start", r.originalStartTime),
			zap.Time("original_end", r.originalEndTime))
	}

	// 为每个 session 启动一个 worker
	for sessionID, stmts := range r.sessionStmts {
		r.wg.Add(1)
		atomic.AddInt32(&r.stats.ActiveWorkers, 1)

		// 如果设置了最大 worker 数，等待信号量
		if r.workerSem != nil {
			r.workerSem <- struct{}{}
		}

		go func(sid string, s []*model.SQLStatement) {
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

// Wait 等待回放完成
func (r *Replayer) Wait() {
	r.wg.Wait()
	atomic.StoreInt32(&r.running, 0)
}

// Stop 停止回放
func (r *Replayer) Stop() {
	if r.cancelFunc != nil {
		r.cancelFunc()
	}
	r.Wait()
}

// GetStats 获取统计信息
func (r *Replayer) GetStats() *ReplayStats {
	r.stats.mu.Lock()
	defer r.stats.mu.Unlock()
	r.stats.LastUpdateTime = time.Now()
	return r.stats
}

// IsRunning 是否正在运行
func (r *Replayer) IsRunning() bool {
	return atomic.LoadInt32(&r.running) == 1
}

// runSessionWorker 运行单个会话的工作协程
func (r *Replayer) runSessionWorker(sessionID string, stmts []*model.SQLStatement) {
	// 创建数据库连接
	dsn := fmt.Sprintf("host=%s port=%d user=%s password=%s dbname=%s sslmode=disable",
		r.config.Host, r.config.Port, r.config.User, r.config.Password, r.config.Database)

	db, err := sql.Open("postgres", dsn)
	if err != nil {
		if logger.Log != nil {
			logger.Log.Error("Failed to open database connection",
				zap.String("session_id", sessionID),
				zap.Error(err))
		}
		// 记录所有语句为失败
		for _, stmt := range stmts {
			atomic.AddInt64(&r.stats.ExecutedStatements, 1)
			atomic.AddInt64(&r.stats.FailureCount, 1)
			r.recordError(stmt.TxID, stmt.VxID, stmt.ID, stmt.SQL, fmt.Sprintf("connection failed: %v", err))
		}
		return
	}
	defer db.Close()

	// 设置连接参数
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	db.SetConnMaxLifetime(5 * time.Minute)

	// 测试连接
	if err := db.Ping(); err != nil {
		if logger.Log != nil {
			logger.Log.Error("Failed to ping database",
				zap.String("session_id", sessionID),
				zap.Error(err))
		}
		for _, stmt := range stmts {
			atomic.AddInt64(&r.stats.ExecutedStatements, 1)
			atomic.AddInt64(&r.stats.FailureCount, 1)
			r.recordError(stmt.TxID, stmt.VxID, stmt.ID, stmt.SQL, fmt.Sprintf("ping failed: %v", err))
		}
		return
	}

	if logger.Log != nil {
		logger.Log.Debug("Session worker started",
			zap.String("session_id", sessionID),
			zap.Int("statements", len(stmts)))
	}

	var currentTx *sql.Tx
	var currentVxID string = "" // 使用 VxID 识别事务边界
	var currentTxStmtCount int = 0

	for _, stmt := range stmts {
		select {
		case <-r.ctx.Done():
			// 如果有未提交的事务，回滚
			if currentTx != nil {
				currentTx.Rollback()
			}
			return
		default:
		}

		// 计算需要等待的时间（除非是快速模式）
		if !r.config.FastMode {
			r.waitForTimestamp(stmt.Timestamp)
		}

		// 处理事务边界：使用 VxID 识别事务
		// VxID 变化时，说明进入了新的事务
		needNewTx := false

		if stmt.VxID != currentVxID && stmt.VxID != "" {
			// VxID 变化，需要开始新事务
			// 如果有之前的事务，先提交（如果没有显式 COMMIT）
			if currentTx != nil {
				if err := currentTx.Commit(); err != nil {
					if logger.Log != nil {
						logger.Log.Warn("Failed to commit previous transaction",
							zap.String("session_id", sessionID),
							zap.String("vxid", currentVxID),
							zap.Error(err))
					}
				}
				currentTx = nil
			}

			// 统计上一事务
			if currentVxID != "" {
				if currentTxStmtCount == 1 {
					atomic.AddInt64(&r.stats.SingleStmtTx, 1)
				} else if currentTxStmtCount > 1 {
					atomic.AddInt64(&r.stats.MultiStmtTx, 1)
				}
			}

			currentVxID = stmt.VxID
			currentTxStmtCount = 0
			needNewTx = true
		} else if stmt.VxID == currentVxID && currentTx == nil {
			// 同一个 VxID 但事务被回滚了，需要重新开始事务
			needNewTx = true
		}

		// 自动开始新事务
		// 但如果当前语句是 BEGIN/COMMIT/ROLLBACK，则跳过
		if needNewTx && stmt.Operation != "BEGIN" && stmt.Operation != "COMMIT" && stmt.Operation != "ROLLBACK" {
			var err error
			currentTx, err = db.Begin()
			if err != nil {
				if logger.Log != nil {
					logger.Log.Error("Failed to begin auto transaction",
						zap.String("session_id", sessionID),
						zap.String("vxid", stmt.VxID),
						zap.Error(err))
				}
			} else if logger.Log != nil {
				logger.Log.Debug("Started auto transaction",
					zap.String("session_id", sessionID),
					zap.String("vxid", stmt.VxID),
					zap.Int64("stmt_id", stmt.ID))
			}
		}

		// 增加当前事务语句计数
		if currentVxID != "" {
			currentTxStmtCount++
		}

		// 执行 SQL 并记录结果
		var execErr error
		var rowsAffected int64 = 0
		var result sql.Result

		switch stmt.Operation {
		case "BEGIN":
			if currentTx == nil {
				currentTx, execErr = db.Begin()
			}
		case "COMMIT":
			if currentTx != nil {
				execErr = currentTx.Commit()
				currentTx = nil
				currentVxID = ""
			}
		case "ROLLBACK":
			if currentTx != nil {
				execErr = currentTx.Rollback()
				currentTx = nil
				currentVxID = ""
			}
		default:
			// 普通 SQL 语句
			if stmt.SQL == "" {
				// 跳过空 SQL
				atomic.AddInt64(&r.stats.ExecutedStatements, 1)
				atomic.AddInt64(&r.stats.SuccessCount, 1)
				continue
			}

			if currentTx != nil {
				result, execErr = currentTx.Exec(stmt.SQL)
			} else {
				// 警告：没有事务上下文，使用独立事务
				if logger.Log != nil && stmt.SeqInTx > 1 {
					logger.Log.Warn("Executing without transaction context",
						zap.String("session_id", sessionID),
						zap.String("vxid", stmt.VxID),
						zap.Int("seq_in_tx", stmt.SeqInTx),
						zap.Int64("stmt_id", stmt.ID))
				}
				result, execErr = db.Exec(stmt.SQL)
			}

			// 获取影响行数
			if result != nil && execErr == nil {
				rowsAffected, _ = result.RowsAffected()
			}
		}

		// 更新统计
		atomic.AddInt64(&r.stats.ExecutedStatements, 1)

		// 检测差异
		r.checkDivergence(stmt, execErr, rowsAffected, sessionID)

		if execErr != nil {
			atomic.AddInt64(&r.stats.FailureCount, 1)
			r.recordError(stmt.TxID, stmt.VxID, stmt.ID, stmt.SQL, execErr.Error())

			// 如果事务中的语句失败，回滚事务
			// 但保留 currentVxID，以便下一条同 vxid 的语句可以正确处理
			if currentTx != nil && stmt.Operation != "BEGIN" && stmt.Operation != "COMMIT" && stmt.Operation != "ROLLBACK" {
				currentTx.Rollback()
				currentTx = nil
				// 不清空 currentVxID，这样下一条同 vxid 的语句会开始新事务
			}
		} else {
			atomic.AddInt64(&r.stats.SuccessCount, 1)
		}

		atomic.StoreInt64(&r.stats.CurrentTxID, stmt.TxID)
	}

	// 清理未提交的事务
	if currentTx != nil {
		currentTx.Commit()
	}

	// 统计最后一个事务
	if currentVxID != "" {
		if currentTxStmtCount == 1 {
			atomic.AddInt64(&r.stats.SingleStmtTx, 1)
		} else if currentTxStmtCount > 1 {
			atomic.AddInt64(&r.stats.MultiStmtTx, 1)
		}
	}

	if logger.Log != nil {
		logger.Log.Debug("Session worker finished",
			zap.String("session_id", sessionID))
	}
}

// waitForTimestamp 等待到指定的时间戳（相对于回放开始时间）
func (r *Replayer) waitForTimestamp(originalTimestamp time.Time) {
	// 计算原始时间相对于原始开始时间的偏移
	originalOffset := originalTimestamp.Sub(r.originalStartTime)

	// 如果偏移为负或者非常大，跳过等待
	if originalOffset < 0 || originalOffset > 24*time.Hour {
		return
	}

	// 根据速度因子调整偏移
	adjustedOffset := time.Duration(float64(originalOffset) / r.config.SpeedFactor)

	// 计算目标时间（回放开始时间 + 调整后的偏移）
	targetTime := r.replayStartTime.Add(adjustedOffset)

	// 计算需要等待的时间
	waitDuration := time.Until(targetTime)

	// 限制最大等待时间为 10 秒
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

// recordError 记录错误
func (r *Replayer) recordError(txID int64, vxID string, stmtID int64, sqlStr string, errMsg string) {
	r.stats.mu.Lock()
	defer r.stats.mu.Unlock()

	// 限制错误记录数量
	if len(r.stats.Errors) < 1000 {
		// 截断过长的 SQL
		if len(sqlStr) > 200 {
			sqlStr = sqlStr[:200] + "..."
		}
		r.stats.Errors = append(r.stats.Errors, ReplayError{
			TxID:      txID,
			VxID:      vxID,
			StmtID:    stmtID,
			SQL:       sqlStr,
			Error:     errMsg,
			Timestamp: time.Now(),
		})
	}
}

// checkDivergence 检测回放差异
func (r *Replayer) checkDivergence(stmt *model.SQLStatement, execErr error, replayRowsAffected int64, sessionID string) {
	// 跳过 BEGIN, COMMIT, ROLLBACK 等控制语句的差异检测
	if stmt.Operation == "BEGIN" || stmt.Operation == "COMMIT" || stmt.Operation == "ROLLBACK" {
		return
	}

	hasDivergence := false
	divergenceType := ""
	replayState := "00000" // 成功状态码
	replayErrMsg := ""

	// 检查错误状态差异
	if execErr != nil {
		replayState = extractPgErrorCode(execErr.Error())
		replayErrMsg = execErr.Error()
	}

	originalSuccess := stmt.State == "00000" || stmt.State == ""
	replaySuccess := execErr == nil

	// 1. 错误状态差异：原来成功但回放失败，或者错误码不同
	if originalSuccess && !replaySuccess {
		// 原来成功，回放失败
		hasDivergence = true
		divergenceType = "error_state"
		atomic.AddInt64(&r.stats.ErrorStateDiff, 1)
	} else if !originalSuccess && replaySuccess {
		// 原来失败，回放成功（也算差异）
		hasDivergence = true
		divergenceType = "error_state"
		atomic.AddInt64(&r.stats.ErrorStateDiff, 1)
	} else if !originalSuccess && !replaySuccess && stmt.State != replayState {
		// 都失败但错误码不同
		hasDivergence = true
		divergenceType = "error_code"
		atomic.AddInt64(&r.stats.ErrorStateDiff, 1)
	}

	// 2. 影响行数差异（只检查成功执行的语句，且不是 SELECT）
	if !hasDivergence && replaySuccess && stmt.Operation != "SELECT" {
		if int64(stmt.RowsAffected) != replayRowsAffected {
			hasDivergence = true
			divergenceType = "rows_affected"
			atomic.AddInt64(&r.stats.RowsAffectedDiff, 1)
		}
	}

	// 记录差异
	if hasDivergence {
		atomic.AddInt64(&r.stats.DivergenceCount, 1)
		r.recordDivergence(stmt, sessionID, divergenceType, replayRowsAffected, replayState, replayErrMsg)
	}
}

// extractPgErrorCode 从 PostgreSQL 错误消息中提取错误码
func extractPgErrorCode(errMsg string) string {
	// PostgreSQL 错误格式通常是 "pq: ERROR: ... (SQLSTATE XXXXX)"
	// 或者 "pq: duplicate key value violates unique constraint"
	// 默认返回一个通用错误码
	if errMsg == "" {
		return "00000"
	}

	// 尝试提取 SQLSTATE
	// 这里简化处理，根据错误类型返回常见的错误码
	if containsIgnoreCase(errMsg, "duplicate key") || containsIgnoreCase(errMsg, "unique constraint") {
		return "23505" // unique_violation
	}
	if containsIgnoreCase(errMsg, "foreign key") {
		return "23503" // foreign_key_violation
	}
	if containsIgnoreCase(errMsg, "not-null") || containsIgnoreCase(errMsg, "null value") {
		return "23502" // not_null_violation
	}
	if containsIgnoreCase(errMsg, "syntax error") {
		return "42601" // syntax_error
	}
	if containsIgnoreCase(errMsg, "does not exist") {
		return "42P01" // undefined_table
	}
	if containsIgnoreCase(errMsg, "permission denied") {
		return "42501" // insufficient_privilege
	}
	if containsIgnoreCase(errMsg, "deadlock") {
		return "40P01" // deadlock_detected
	}
	if containsIgnoreCase(errMsg, "serialization") {
		return "40001" // serialization_failure
	}

	return "ERROR" // 通用错误
}

// containsIgnoreCase 检查字符串是否包含子串（不区分大小写）
func containsIgnoreCase(s, substr string) bool {
	return strings.Contains(strings.ToLower(s), strings.ToLower(substr))
}

// recordDivergence 记录差异
func (r *Replayer) recordDivergence(stmt *model.SQLStatement, sessionID, divergenceType string, replayRowsAffected int64, replayState, replayErrMsg string) {
	r.stats.mu.Lock()
	defer r.stats.mu.Unlock()

	// 限制差异记录数量
	if len(r.stats.Divergences) < 1000 {
		sqlStr := stmt.SQL
		if len(sqlStr) > 200 {
			sqlStr = sqlStr[:200] + "..."
		}

		r.stats.Divergences = append(r.stats.Divergences, ReplayDivergence{
			StmtID:               stmt.ID,
			TxID:                 stmt.TxID,
			VxID:                 stmt.VxID,
			SessionID:            sessionID,
			SQL:                  sqlStr,
			DivergenceType:       divergenceType,
			OriginalRowsAffected: stmt.RowsAffected,
			ReplayRowsAffected:   replayRowsAffected,
			OriginalState:        stmt.State,
			ReplayState:          replayState,
			OriginalError:        "", // 原始错误信息不在当前数据模型中
			ReplayError:          replayErrMsg,
			Timestamp:            time.Now(),
		})
	}
}

// GenerateReport 生成回放报告
func (r *Replayer) GenerateReport() *model.ReplayReport {
	stats := r.GetStats()

	duration := stats.LastUpdateTime.Sub(stats.StartTime)
	successRate := float64(0)
	divergenceRate := float64(0)
	if stats.ExecutedStatements > 0 {
		successRate = float64(stats.SuccessCount) / float64(stats.ExecutedStatements) * 100
		divergenceRate = float64(stats.DivergenceCount) / float64(stats.ExecutedStatements) * 100
	}

	report := &model.ReplayReport{
		TotalStatements:  stats.TotalStatements,
		ExecutedStmts:    stats.ExecutedStatements,
		SuccessStmts:     stats.SuccessCount,
		FailedStmts:      stats.FailureCount,
		SuccessRate:      successRate,
		Duration:         duration.String(),
		DurationSeconds:  duration.Seconds(),
		StartTime:        stats.StartTime,
		EndTime:          stats.LastUpdateTime,
		SessionCount:     int64(len(r.sessionStmts)),
		SingleStmtTx:     stats.SingleStmtTx,
		MultiStmtTx:      stats.MultiStmtTx,
		DivergenceCount:  stats.DivergenceCount,
		RowsAffectedDiff: stats.RowsAffectedDiff,
		ErrorStateDiff:   stats.ErrorStateDiff,
		DivergenceRate:   divergenceRate,
	}

	// 转换错误记录
	report.Errors = make([]model.ReplayError, len(stats.Errors))
	for i, e := range stats.Errors {
		report.Errors[i] = model.ReplayError{
			TxID:      e.TxID,
			StmtID:    e.StmtID,
			SQL:       e.SQL,
			Error:     e.Error,
			Timestamp: e.Timestamp,
		}
	}

	// 转换差异记录
	report.Divergences = make([]model.ReplayDivergence, len(stats.Divergences))
	for i, d := range stats.Divergences {
		report.Divergences[i] = model.ReplayDivergence{
			StmtID:               d.StmtID,
			TxID:                 d.TxID,
			SessionID:            d.SessionID,
			SQL:                  d.SQL,
			DivergenceType:       d.DivergenceType,
			OriginalRowsAffected: d.OriginalRowsAffected,
			ReplayRowsAffected:   d.ReplayRowsAffected,
			OriginalState:        d.OriginalState,
			ReplayState:          d.ReplayState,
			OriginalError:        d.OriginalError,
			ReplayError:          d.ReplayError,
			Timestamp:            d.Timestamp,
		}
	}

	return report
}
