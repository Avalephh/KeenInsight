package model

import "time"

// TaskStatus 任务状态枚举
const (
	TaskStatusPending   = 0 // 未开始
	TaskStatusPreparing = 1 // 准备中
	TaskStatusReady     = 2 // 就绪
	TaskStatusRunning   = 3 // 进行中
	TaskStatusCompleted = 4 // 完成
	TaskStatusFailed    = 5 // 失败
	TaskStatusStopped   = 6 // 停止
)

// TaskInfo 任务信息表 (t_task_info)
type TaskInfo struct {
	TaskID      string    `json:"task_id" gorm:"primaryKey;type:char(36)"`
	DstIP       string    `json:"dst_ip" gorm:"column:dst_ip;type:varchar(255)"`
	DstPort     int       `json:"dst_port" gorm:"column:dst_port;type:int"`
	DstUser     string    `json:"dst_user" gorm:"column:dst_user;type:varchar(64)"`
	DstPass     string    `json:"dst_pass" gorm:"column:dst_pass;type:varchar(255)"`
	Status      int       `json:"status" gorm:"type:smallint"` // 0:Pending, 1:Preparing, 2:Ready, 3:Running, 4:Completed, 5:Failed, 6:Stopped
	CreateTime  time.Time `json:"create_time"`
	UpdateTime  time.Time `json:"update_time"`
	LogFilePath string    `json:"log_file_path" gorm:"-"` // 辅助字段，不存库或根据实际情况调整
	SpeedFactor float64   `json:"speed_factor" gorm:"-"`
	MaxWorkers  int       `json:"max_workers" gorm:"-"`
}

func (TaskInfo) TableName() string {
	return "t_task_info"
}

// TrafficBaseline 流量基线数据表 (t_traffic_baseline)
type TrafficBaseline struct {
	ID            int64  `json:"id" gorm:"primaryKey;autoIncrement"`
	TaskID        string `json:"task_id" gorm:"index:idx_task_sql;type:char(36)"`
	SQLID         string `json:"sql_id" gorm:"index:idx_task_sql;type:varchar(64)"`
	ExecTimestamp int64  `json:"exec_timestamp" gorm:"type:bigint"` // 原始执行时间戳 (ms)
	SessionID     string `json:"session_id" gorm:"type:varchar(64)"`
	SQLText       string `json:"sql_text" gorm:"type:text"`
	DBName        string `json:"db_name" gorm:"type:varchar(64)"`
	UserName      string `json:"user_name" gorm:"type:varchar(64)"`
	TxID          string `json:"tx_id" gorm:"type:varchar(64)"` // 对应 thesis 的 tx_id (Stores PG vxid)
	// 辅助字段
	OriginTime time.Time `json:"origin_time" gorm:"-"`
	SQLType    string    `json:"sql_type" gorm:"-"`
	Operation  string    `json:"operation" gorm:"-"`
	SeqInTx    int       `json:"seq_in_tx" gorm:"-"`
}

func (TrafficBaseline) TableName() string {
	return "t_traffic_baseline"
}

// ReplayDetail 回放明细表 (t_replay_detail)
type ReplayDetail struct {
	ID           int64   `json:"id" gorm:"primaryKey;autoIncrement"`
	TaskID       string  `json:"task_id" gorm:"index:idx_task_round_sql;type:char(36)"`
	Round        int     `json:"round" gorm:"index:idx_task_round_sql"`
	SQLID        string  `json:"sql_id" gorm:"index:idx_task_round_sql;type:varchar(64)"`
	RowsAffected int64   `json:"rows_affected" gorm:"type:bigint"`
	RowsReturned int64   `json:"rows_returned" gorm:"type:bigint"`
	RowsScanned  int64   `json:"rows_scanned" gorm:"type:bigint"`
	ExecDuration float64 `json:"exec_duration" gorm:"type:decimal(10,3)"`
	// 辅助字段用于记录差异
	DivergenceType string `json:"divergence_type" gorm:"-"`
	ErrorMessage   string `json:"error_message" gorm:"-"`
	State          string `json:"state" gorm:"-"`
}

func (ReplayDetail) TableName() string {
	return "t_replay_detail"
}

// ReplaySummary 回放轮次总览表 (t_replay_summary)
type ReplaySummary struct {
	ID              int64   `json:"id" gorm:"primaryKey;autoIncrement"`
	TaskID          string  `json:"task_id" gorm:"type:char(36)"`
	Round           int     `json:"round"`
	TotalDuration   int64   `json:"total_duration" gorm:"type:bigint"` // ms
	TotalStmts      int     `json:"total_stmts"`
	TxCount         int     `json:"tx_count"`
	SuccessCnt      int     `json:"success_cnt"`
	ErrorCnt        int     `json:"error_cnt"`
	QPS             float64 `json:"qps" gorm:"type:decimal(10,2)"`
	TPS             float64 `json:"tps" gorm:"type:decimal(10,2)"`
	ReplayFidelity  float64 `json:"replay_fidelity" gorm:"type:decimal(5,4)"`
	ExecSuccessRate float64 `json:"exec_success_rate" gorm:"type:decimal(5,4)"`
	RowsMatchedRate float64 `json:"rows_matched_rate" gorm:"type:decimal(5,4)"`
	ErrorRate       float64 `json:"error_rate" gorm:"type:decimal(5,4)"`
}

func (ReplaySummary) TableName() string {
	return "t_replay_summary"
}

// ReplayAggregation 回放聚合分析表 (t_replay_aggregation)
type ReplayAggregation struct {
	ID          int64   `json:"id" gorm:"primaryKey;autoIncrement"`
	TaskID      string  `json:"task_id" gorm:"index:idx_agg_task_round_digest;type:char(36)"`
	Round       int     `json:"round" gorm:"index:idx_agg_task_round_digest"`
	SQLDigest   string  `json:"sql_digest" gorm:"index:idx_agg_task_round_digest;type:varchar(64)"`
	SQLTemplate string  `json:"sql_template" gorm:"type:text"`
	AvgLatency  float64 `json:"avg_latency" gorm:"type:decimal(10,3)"`
	P95Latency  float64 `json:"p95_latency" gorm:"type:decimal(10,3)"`
	P99Latency  float64 `json:"p99_latency" gorm:"type:decimal(10,3)"`
}

func (ReplayAggregation) TableName() string {
	return "t_replay_aggregation"
}

// Transaction 内存对象，用于事务重组
type Transaction struct {
	ID        int64  `json:"id" gorm:"primaryKey;autoIncrement"` // GORM needs ID for upsert
	TaskID    string `json:"task_id" gorm:"index:idx_task_txid"` // Added for persistence
	TxID      string `gorm:"index:idx_task_txid"`
	SessionID string
	StartTime time.Time
	EndTime   time.Time
	StmtCount int                `json:"stmt_count"` // Added for stats
	Stmts     []*TrafficBaseline `gorm:"foreignKey:TxID;references:TxID"`
	Committed bool
}
