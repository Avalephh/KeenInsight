package repository

import (
	"fmt"
	"ruc-db-replay/internal/model"
	"ruc-db-replay/pkg/logger"

	"go.uber.org/zap"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	gormlogger "gorm.io/gorm/logger"
)

// ReplayRepository 回放数据存储仓库
type ReplayRepository struct {
	db *gorm.DB
}

// NewReplayRepository 创建仓库实例
func NewReplayRepository(dbPath string) (*ReplayRepository, error) {
	db, err := gorm.Open(sqlite.Open(dbPath), &gorm.Config{
		Logger: gormlogger.Default.LogMode(gormlogger.Silent),
	})
	if err != nil {
		return nil, fmt.Errorf("failed to open database: %w", err)
	}

	// 自动迁移表结构
	if err := db.AutoMigrate(
		&model.TaskInfo{},
		&model.TrafficBaseline{},
		&model.ReplayDetail{},
		&model.ReplaySummary{},
		&model.ReplayAggregation{},
		// Note: Transaction struct is primarily in-memory, but if we persist it for resume/stats:
		&model.Transaction{},
		// ReplayError moved inside ReplaySummary or ReplayDetail in thesis?
		// Thesis doesn't explicitly mention t_replay_error table.
		// ReplayDetail includes ErrorMessage.
		// But replayer.go uses ReplayError struct for stats.
		// We'll keep ReplayError if needed or map it to ReplayDetail.
		// Detailed lookup shows ReplayDetail handles errors.
	); err != nil {
		return nil, fmt.Errorf("failed to migrate database: %w", err)
	}

	return &ReplayRepository{db: db}, nil
}

// Close 关闭数据库连接
func (r *ReplayRepository) Close() error {
	sqlDB, err := r.db.DB()
	if err != nil {
		return err
	}
	return sqlDB.Close()
}

// ==================== TaskInfo (ReplayTask) 操作 ====================

// CreateTask 创建回放任务
func (r *ReplayRepository) CreateTask(task *model.TaskInfo) error {
	return r.db.Create(task).Error
}

// GetTask 获取任务
func (r *ReplayRepository) GetTask(taskID string) (*model.TaskInfo, error) {
	var task model.TaskInfo
	if err := r.db.First(&task, "task_id = ?", taskID).Error; err != nil {
		return nil, err
	}
	return &task, nil
}

// UpdateTask 更新任务
func (r *ReplayRepository) UpdateTask(task *model.TaskInfo) error {
	return r.db.Save(task).Error
}

// UpdateTaskStatus 更新任务状态
func (r *ReplayRepository) UpdateTaskStatus(taskID string, status string) error {
	return r.db.Model(&model.TaskInfo{}).Where("task_id = ?", taskID).Update("status", status).Error
}

// ==================== Transaction 操作 ====================

// BatchCreateTransactions 批量创建或更新事务记录
func (r *ReplayRepository) BatchCreateTransactions(transactions []*model.Transaction, batchSize int) error {
	if len(transactions) == 0 {
		return nil
	}
	// Transaction table schema might differ from Thesis (which focuses on memory).
	// Assuming we still persist for progress tracking.
	// TxID is string now.
	return r.db.Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "task_id"}, {Name: "tx_id"}},
		DoUpdates: clause.AssignmentColumns([]string{"end_time", "stmt_count", "committed"}),
	}).CreateInBatches(transactions, batchSize).Error
}

// DeleteTaskData 删除任务相关的所有数据
func (r *ReplayRepository) DeleteTaskData(taskID string) error {
	return r.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Where("task_id = ?", taskID).Delete(&model.TrafficBaseline{}).Error; err != nil {
			return err
		}
		if err := tx.Where("task_id = ?", taskID).Delete(&model.Transaction{}).Error; err != nil {
			return err
		}
		if err := tx.Where("task_id = ?", taskID).Delete(&model.ReplayDetail{}).Error; err != nil {
			return err
		}
		if err := tx.Where("task_id = ?", taskID).Delete(&model.ReplaySummary{}).Error; err != nil {
			return err
		}
		return nil
	})
}

// GetTransactionsByTask 获取任务的所有事务
func (r *ReplayRepository) GetTransactionsByTask(taskID string) ([]*model.Transaction, error) {
	var transactions []*model.Transaction
	err := r.db.Where("task_id = ?", taskID).Order("start_time").Find(&transactions).Error
	return transactions, err
}

// ==================== TrafficBaseline (SQLStatement) 操作 ====================

// BatchCreateStatements 批量创建SQL语句记录
func (r *ReplayRepository) BatchCreateStatements(statements []*model.TrafficBaseline, batchSize int) error {
	return r.db.CreateInBatches(statements, batchSize).Error
}

// GetStatementsByTask 获取任务的所有SQL语句
func (r *ReplayRepository) GetStatementsByTask(taskID string) ([]*model.TrafficBaseline, error) {
	var statements []*model.TrafficBaseline
	err := r.db.Where("task_id = ?", taskID).Order("exec_timestamp").Find(&statements).Error
	return statements, err
}

// GetStatementsByTaskPaginated 分页获取SQL语句
func (r *ReplayRepository) GetStatementsByTaskPaginated(taskID string, offset, limit int) ([]*model.TrafficBaseline, error) {
	var statements []*model.TrafficBaseline
	err := r.db.Where("task_id = ?", taskID).
		Order("exec_timestamp").
		Offset(offset).
		Limit(limit).
		Find(&statements).Error
	return statements, err
}

// GetStatementsByTx 获取事务的所有SQL语句
func (r *ReplayRepository) GetStatementsByTx(taskID string, txID string) ([]*model.TrafficBaseline, error) {
	var statements []*model.TrafficBaseline
	err := r.db.Where("task_id = ? AND tx_id = ?", taskID, txID).Order("exec_timestamp").Find(&statements).Error
	return statements, err
}

// GetStatementCount 获取SQL语句数量
func (r *ReplayRepository) GetStatementCount(taskID string) (int64, error) {
	var count int64
	err := r.db.Model(&model.TrafficBaseline{}).Where("task_id = ?", taskID).Count(&count).Error
	return count, err
}

// GetTransactionCount 获取事务数量
func (r *ReplayRepository) GetTransactionCount(taskID string) (int64, error) {
	var count int64
	err := r.db.Model(&model.Transaction{}).Where("task_id = ?", taskID).Count(&count).Error
	return count, err
}

// ==================== ReplayProgress 操作 ====================
// ReplayProgress model was not in the new model file. Assuming we keep it or it was removed?
// Thesis doesn't mention ReplayProgress table explicitly, maybe part of TaskInfo or transient?
// Let's assume we use TaskInfo for status updates, or keep it if defined in model (I didn't see it in my model update).
// Checking model definition... I removed ReplayProgress from model/replay.go.
// So I should remove it here too and use TaskInfo or transient structure.
// WE WILL REMOVE ReplayProgress methods for now as they are not in the Thesis schema.

// ==================== ReplaySummary (ReplayReport) 操作 ====================

// CreateReport 创建报告
func (r *ReplayRepository) CreateReport(report *model.ReplaySummary) error {
	return r.db.Create(report).Error
}

// GetReport 获取报告
func (r *ReplayRepository) GetReport(taskID string) (*model.ReplaySummary, error) {
	var report model.ReplaySummary
	if err := r.db.First(&report, "task_id = ?", taskID).Error; err != nil {
		return nil, err
	}
	return &report, nil
}

// ==================== ReplayDetail (ReplayDivergence) 操作 ====================

// CreateDivergence 创建差异记录
func (r *ReplayRepository) CreateDivergence(divergence *model.ReplayDetail) error {
	return r.db.Create(divergence).Error
}

// BatchCreateDivergences 批量创建差异记录
func (r *ReplayRepository) BatchCreateDivergences(divergences []*model.ReplayDetail, batchSize int) error {
	if len(divergences) == 0 {
		return nil
	}
	return r.db.CreateInBatches(divergences, batchSize).Error
}

// GetDivergencesByTask 获取任务的所有差异
func (r *ReplayRepository) GetDivergencesByTask(taskID string, limit int) ([]*model.ReplayDetail, error) {
	var divergences []*model.ReplayDetail
	query := r.db.Where("task_id = ?", taskID).Order("sql_id") // No timestamp in ReplayDetail per Thesis? Table 3.3.
	// But generated models usually have CreatedAt/UpdatedAt gorm.Model if embedded?
	// In my model update, ReplayDetail struct has no timestamp.
	// So order by ID (auto increment) or SQLID?
	// Let's assume order by ID if GORM adds it?
	// My model definition: type ReplayDetail struct { ID int64 ... }
	query = query.Order("id desc")

	if limit > 0 {
		query = query.Limit(limit)
	}
	err := query.Find(&divergences).Error
	return divergences, err
}

// GetDivergencesByTaskPaginated 分页获取差异
func (r *ReplayRepository) GetDivergencesByTaskPaginated(taskID string, offset, limit int) ([]*model.ReplayDetail, error) {
	var divergences []*model.ReplayDetail
	err := r.db.Where("task_id = ?", taskID).
		Order("id desc").
		Offset(offset).
		Limit(limit).
		Find(&divergences).Error
	return divergences, err
}

// GetDivergenceStats 获取差异统计
func (r *ReplayRepository) GetDivergenceStats(taskID string) (map[string]int64, error) {
	stats := make(map[string]int64)

	// 总差异数
	var totalCount int64
	r.db.Model(&model.ReplayDetail{}).Where("task_id = ?", taskID).Count(&totalCount)
	stats["total"] = totalCount

	// 按类型统计
	type TypeCount struct {
		DivergenceType string
		Count          int64
	}
	var typeCounts []TypeCount
	r.db.Model(&model.ReplayDetail{}).
		Select("divergence_type, count(*) as count").
		Where("task_id = ?", taskID).
		Group("divergence_type").
		Scan(&typeCounts)

	for _, tc := range typeCounts {
		stats[tc.DivergenceType] = tc.Count
	}

	return stats, nil
}

// ==================== 统计操作 ====================

// GetTaskStatistics 获取任务统计信息
func (r *ReplayRepository) GetTaskStatistics(taskID string) (map[string]interface{}, error) {
	stats := make(map[string]interface{})

	// 语句总数
	var stmtCount int64
	r.db.Model(&model.TrafficBaseline{}).Where("task_id = ?", taskID).Count(&stmtCount)
	stats["total_statements"] = stmtCount

	// 事务总数
	var txCount int64
	r.db.Model(&model.Transaction{}).Where("task_id = ?", taskID).Count(&txCount)
	stats["total_transactions"] = txCount

	// 会话数
	var sessionCount int64
	r.db.Model(&model.TrafficBaseline{}).Where("task_id = ?", taskID).Distinct("session_id").Count(&sessionCount)
	stats["session_count"] = sessionCount

	// SQL类型分布 (by_type)
	type TypeCount struct {
		SQLType string `gorm:"column:sql_type"`
		Count   int64  `gorm:"column:count"`
	}
	var typeCounts []TypeCount
	r.db.Model(&model.TrafficBaseline{}).
		Select("sql_type, count(*) as count").
		Where("task_id = ?", taskID).
		Group("sql_type").
		Scan(&typeCounts)

	byType := make(map[string]int64)
	for _, tc := range typeCounts {
		if tc.SQLType != "" {
			byType[tc.SQLType] = tc.Count
		}
	}
	stats["by_type"] = byType

	// 操作类型分布 (by_operation)
	type OpCount struct {
		Operation string `gorm:"column:operation"`
		Count     int64  `gorm:"column:count"`
	}
	var opCounts []OpCount
	r.db.Model(&model.TrafficBaseline{}).
		Select("operation, count(*) as count").
		Where("task_id = ?", taskID).
		Group("operation").
		Scan(&opCounts)

	byOperation := make(map[string]int64)
	for _, oc := range opCounts {
		if oc.Operation != "" {
			byOperation[oc.Operation] = oc.Count
		}
	}
	stats["by_operation"] = byOperation

	// 单语句事务和多语句事务统计
	type TxTypeCount struct {
		StmtCount int64 `gorm:"column:stmt_count"`
		TxCount   int64 `gorm:"column:tx_count"`
	}
	var singleStmtTx, multiStmtTx int64
	var txTypeCounts []TxTypeCount
	r.db.Model(&model.Transaction{}).
		Select("stmt_count, count(*) as tx_count").
		Where("task_id = ?", taskID).
		Group("stmt_count").
		Scan(&txTypeCounts)

	for _, tc := range txTypeCounts {
		if tc.StmtCount == 1 {
			singleStmtTx = tc.TxCount
		} else if tc.StmtCount > 1 {
			multiStmtTx += tc.TxCount
		}
	}
	stats["single_stmt_tx"] = singleStmtTx
	stats["multi_stmt_tx"] = multiStmtTx

	return stats, nil
}

// SaveParsedData 保存解析后的数据（事务性操作）
func (r *ReplayRepository) SaveParsedData(task *model.TaskInfo, statements []*model.TrafficBaseline, transactions map[string]*model.Transaction) error {
	return r.db.Transaction(func(tx *gorm.DB) error {
		// 保存任务
		if err := tx.Save(task).Error; err != nil {
			return fmt.Errorf("failed to save task: %w", err)
		}

		// 批量保存事务
		txList := make([]*model.Transaction, 0, len(transactions))
		for _, t := range transactions {
			txList = append(txList, t)
		}
		if len(txList) > 0 {
			if err := tx.CreateInBatches(txList, 1000).Error; err != nil {
				return fmt.Errorf("failed to save transactions: %w", err)
			}
		}

		// 批量保存语句
		if len(statements) > 0 {
			if err := tx.CreateInBatches(statements, 1000).Error; err != nil {
				return fmt.Errorf("failed to save statements: %w", err)
			}
		}

		logger.Log.Info("Saved parsed data",
			zap.String("task_id", task.TaskID),
			zap.Int("transactions", len(txList)),
			zap.Int("statements", len(statements)))

		return nil
	})
}

// ==================== ReplayAggregation 操作 ====================

// CreateAggregation 创建回放聚合信息
func (r *ReplayRepository) CreateAggregation(agg *model.ReplayAggregation) error {
	return r.db.Create(agg).Error
}

// GetAggregation 获取回放聚合信息
func (r *ReplayRepository) GetAggregation(taskID string) (*model.ReplayAggregation, error) {
	var agg model.ReplayAggregation
	if err := r.db.First(&agg, "task_id = ?", taskID).Error; err != nil {
		return nil, err
	}
	return &agg, nil
}
