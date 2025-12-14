package model

import "time"

// SQLStatement 单条 SQL 语句
type SQLStatement struct {
	ID           int64     `json:"id" gorm:"primaryKey;autoIncrement"`
	TaskID       string    `json:"task_id" gorm:"index;not null"` // 任务ID
	TxID         int64     `json:"tx_id" gorm:"index"`            // 事务ID (PostgreSQL xid)
	VxID         string    `json:"vxid" gorm:"column:vxid;index"` // 虚拟事务ID (格式: procNumber/localXID，如 "4/12532")
	SessionID    string    `json:"session_id" gorm:"index"`       // 会话ID
	Timestamp    time.Time `json:"timestamp" gorm:"index"`        // 执行时间戳
	Database     string    `json:"database"`                      // 数据库名
	Username     string    `json:"username"`                      // 用户名
	SQLType      string    `json:"sql_type"`                      // SQL类型: READ, WRITE, DDL, MISC
	Operation    string    `json:"operation"`                     // 操作类型: SELECT, INSERT, UPDATE, DELETE, BEGIN, COMMIT...
	SQL          string    `json:"sql" gorm:"type:text"`          // 完整SQL语句（参数已填充）
	RowsAffected int       `json:"rows_affected"`                 // 影响行数
	State        string    `json:"state"`                         // 执行状态码 (00000 = success)
	SeqInTx      int       `json:"seq_in_tx"`                     // 在事务中的顺序
}

// Transaction 事务信息（用于按事务组织 SQL）
type Transaction struct {
	ID        int64     `json:"id" gorm:"primaryKey;autoIncrement"`
	TaskID    string    `json:"task_id" gorm:"index:idx_task_vxid,unique;not null"`
	TxID      int64     `json:"tx_id" gorm:"index"`
	VxID      string    `json:"vxid" gorm:"column:vxid;index:idx_task_vxid,unique"` // 虚拟事务ID (格式: procNumber/localXID)
	SessionID string    `json:"session_id" gorm:"index"`
	StartTime time.Time `json:"start_time"`
	EndTime   time.Time `json:"end_time"`
	StmtCount int       `json:"stmt_count"` // 语句数量
	Committed bool      `json:"committed"`  // 是否已提交
}

// ReplayTask 回放任务
type ReplayTask struct {
	ID              string     `json:"id" gorm:"primaryKey"`
	Status          string     `json:"status" gorm:"index"` // pending, preparing, ready, running, completed, failed
	TargetHost      string     `json:"target_host"`
	TargetPort      int        `json:"target_port"`
	TargetUser      string     `json:"target_user"`
	TargetPassword  string     `json:"target_password"`
	TargetDatabase  string     `json:"target_database"`
	LogFilePath     string     `json:"log_file_path"`
	TotalStatements int64      `json:"total_statements"`
	TotalTx         int64      `json:"total_tx"`
	CreatedAt       time.Time  `json:"created_at"`
	UpdatedAt       time.Time  `json:"updated_at"`
	StartedAt       *time.Time `json:"started_at,omitempty"`
	CompletedAt     *time.Time `json:"completed_at,omitempty"`
	ErrorMessage    string     `json:"error_message,omitempty"`
}

// ReplayProgress 回放进度
type ReplayProgress struct {
	TaskID             string    `json:"task_id" gorm:"primaryKey"`
	TotalStatements    int64     `json:"total_statements"`
	ExecutedStatements int64     `json:"executed_statements"`
	SuccessCount       int64     `json:"success_count"`
	FailureCount       int64     `json:"failure_count"`
	CurrentTxID        int64     `json:"current_tx_id"`
	StartTime          time.Time `json:"start_time"`
	LastUpdateTime     time.Time `json:"last_update_time"`
}

// ReplayReport 回放报告
type ReplayReport struct {
	TaskID          string  `json:"task_id" gorm:"primaryKey"`
	TotalStatements int64   `json:"total_statements"`
	TotalTx         int64   `json:"total_tx"`
	ExecutedStmts   int64   `json:"executed_stmts"`
	SuccessStmts    int64   `json:"success_stmts"`
	FailedStmts     int64   `json:"failed_stmts"`
	SkippedStmts    int64   `json:"skipped_stmts"`
	SuccessRate     float64 `json:"success_rate"`
	Duration        string  `json:"duration"`
	DurationSeconds float64 `json:"duration_seconds"`
	AvgLatencyMs    float64 `json:"avg_latency_ms"`
	MaxLatencyMs    float64 `json:"max_latency_ms"`
	MinLatencyMs    float64 `json:"min_latency_ms"`

	// 详细统计
	SessionCount int64 `json:"session_count"`
	SingleStmtTx int64 `json:"single_stmt_tx"`
	MultiStmtTx  int64 `json:"multi_stmt_tx"`

	StartTime time.Time `json:"start_time"`
	EndTime   time.Time `json:"end_time"`
	// 差异统计
	DivergenceCount  int64              `json:"divergence_count"`   // 总差异数
	RowsAffectedDiff int64              `json:"rows_affected_diff"` // 影响行数不同的语句数
	ErrorStateDiff   int64              `json:"error_state_diff"`   // 错误状态不同的语句数
	DivergenceRate   float64            `json:"divergence_rate"`    // 差异率
	Errors           []ReplayError      `json:"errors,omitempty" gorm:"-"`
	Divergences      []ReplayDivergence `json:"divergences,omitempty" gorm:"-"`
}

// ReplayError 回放错误记录
type ReplayError struct {
	ID        int64     `json:"id" gorm:"primaryKey;autoIncrement"`
	TaskID    string    `json:"task_id" gorm:"index"`
	TxID      int64     `json:"tx_id"`
	VxID      string    `json:"vxid"` // 虚拟事务ID
	StmtID    int64     `json:"stmt_id"`
	SQL       string    `json:"sql" gorm:"type:text"`
	Error     string    `json:"error" gorm:"type:text"`
	Timestamp time.Time `json:"timestamp"`
}

// ReplayDivergence 回放差异记录
type ReplayDivergence struct {
	ID                   int64     `json:"id" gorm:"primaryKey;autoIncrement"`
	TaskID               string    `json:"task_id" gorm:"index"`
	StmtID               int64     `json:"stmt_id" gorm:"index"`
	TxID                 int64     `json:"tx_id"`
	VxID                 string    `json:"vxid"` // 虚拟事务ID
	SessionID            string    `json:"session_id"`
	SQL                  string    `json:"sql" gorm:"type:text"`
	DivergenceType       string    `json:"divergence_type"` // rows_affected, error_state, error_code
	OriginalRowsAffected int       `json:"original_rows_affected"`
	ReplayRowsAffected   int64     `json:"replay_rows_affected"`
	OriginalState        string    `json:"original_state"` // 原始状态码 (00000 = success)
	ReplayState          string    `json:"replay_state"`   // 回放状态码
	OriginalError        string    `json:"original_error"` // 原始错误信息
	ReplayError          string    `json:"replay_error"`   // 回放错误信息
	Timestamp            time.Time `json:"timestamp"`
}

// TableName 指定表名
func (SQLStatement) TableName() string {
	return "sql_statements"
}

func (Transaction) TableName() string {
	return "transactions"
}

func (ReplayTask) TableName() string {
	return "replay_tasks"
}

func (ReplayProgress) TableName() string {
	return "replay_progress"
}

func (ReplayReport) TableName() string {
	return "replay_reports"
}

func (ReplayError) TableName() string {
	return "replay_errors"
}

func (ReplayDivergence) TableName() string {
	return "replay_divergences"
}
