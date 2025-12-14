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

	// 启用 WAL 模式提高并发性能
	// db.Exec("PRAGMA journal_mode=WAL")
	// db.Exec("PRAGMA synchronous=NORMAL")
	// db.Exec("PRAGMA cache_size=10000")

	// 自动迁移表结构
	if err := db.AutoMigrate(
		&model.ReplayTask{},
		&model.Transaction{},
		&model.SQLStatement{},
		&model.ReplayProgress{},
		&model.ReplayReport{},
		&model.ReplayError{},
		&model.ReplayDivergence{},
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

// ==================== ReplayTask 操作 ====================

// CreateTask 创建回放任务
func (r *ReplayRepository) CreateTask(task *model.ReplayTask) error {
	return r.db.Create(task).Error
}

// GetTask 获取任务
func (r *ReplayRepository) GetTask(taskID string) (*model.ReplayTask, error) {
	var task model.ReplayTask
	if err := r.db.First(&task, "id = ?", taskID).Error; err != nil {
		return nil, err
	}
	return &task, nil
}

// UpdateTask 更新任务
func (r *ReplayRepository) UpdateTask(task *model.ReplayTask) error {
	return r.db.Save(task).Error
}

// UpdateTaskStatus 更新任务状态
func (r *ReplayRepository) UpdateTaskStatus(taskID string, status string) error {
	return r.db.Model(&model.ReplayTask{}).Where("id = ?", taskID).Update("status", status).Error
}

// ==================== Transaction 操作 ====================

// BatchCreateTransactions 批量创建或更新事务记录
func (r *ReplayRepository) BatchCreateTransactions(transactions []*model.Transaction, batchSize int) error {
	if len(transactions) == 0 {
		return nil
	}
	// 使用 Upsert: 如果 (task_id, vxid) 冲突，则更新字段
	return r.db.Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "task_id"}, {Name: "vxid"}},
		DoUpdates: clause.AssignmentColumns([]string{"end_time", "stmt_count", "committed"}),
	}).CreateInBatches(transactions, batchSize).Error
}

// DeleteTaskData 删除任务相关的所有数据
func (r *ReplayRepository) DeleteTaskData(taskID string) error {
	return r.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Where("task_id = ?", taskID).Delete(&model.SQLStatement{}).Error; err != nil {
			return err
		}
		if err := tx.Where("task_id = ?", taskID).Delete(&model.Transaction{}).Error; err != nil {
			return err
		}
		if err := tx.Where("task_id = ?", taskID).Delete(&model.ReplayProgress{}).Error; err != nil {
			return err
		}
		if err := tx.Where("task_id = ?", taskID).Delete(&model.ReplayReport{}).Error; err != nil {
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

// GetTransactionByTxID 根据事务ID获取事务
func (r *ReplayRepository) GetTransactionByTxID(taskID string, txID int64) (*model.Transaction, error) {
	var tx model.Transaction
	err := r.db.Where("task_id = ? AND tx_id = ?", taskID, txID).First(&tx).Error
	return &tx, err
}

// ==================== SQLStatement 操作 ====================

// BatchCreateStatements 批量创建SQL语句记录
func (r *ReplayRepository) BatchCreateStatements(statements []*model.SQLStatement, batchSize int) error {
	return r.db.CreateInBatches(statements, batchSize).Error
}

// GetStatementsByTask 获取任务的所有SQL语句
func (r *ReplayRepository) GetStatementsByTask(taskID string) ([]*model.SQLStatement, error) {
	var statements []*model.SQLStatement
	err := r.db.Where("task_id = ?", taskID).Order("timestamp, seq_in_tx").Find(&statements).Error
	return statements, err
}

// GetStatementsByTx 获取事务的所有SQL语句
func (r *ReplayRepository) GetStatementsByTx(taskID string, txID int64) ([]*model.SQLStatement, error) {
	var statements []*model.SQLStatement
	err := r.db.Where("task_id = ? AND tx_id = ?", taskID, txID).Order("seq_in_tx").Find(&statements).Error
	return statements, err
}

// GetStatementsByTaskPaginated 分页获取SQL语句
func (r *ReplayRepository) GetStatementsByTaskPaginated(taskID string, offset, limit int) ([]*model.SQLStatement, error) {
	var statements []*model.SQLStatement
	err := r.db.Where("task_id = ?", taskID).
		Order("timestamp, seq_in_tx").
		Offset(offset).
		Limit(limit).
		Find(&statements).Error
	return statements, err
}

// GetStatementCount 获取SQL语句数量
func (r *ReplayRepository) GetStatementCount(taskID string) (int64, error) {
	var count int64
	err := r.db.Model(&model.SQLStatement{}).Where("task_id = ?", taskID).Count(&count).Error
	return count, err
}

// GetTransactionCount 获取事务数量
func (r *ReplayRepository) GetTransactionCount(taskID string) (int64, error) {
	var count int64
	err := r.db.Model(&model.Transaction{}).Where("task_id = ?", taskID).Count(&count).Error
	return count, err
}

// ==================== ReplayProgress 操作 ====================

// CreateProgress 创建进度记录
func (r *ReplayRepository) CreateProgress(progress *model.ReplayProgress) error {
	return r.db.Create(progress).Error
}

// GetProgress 获取进度
func (r *ReplayRepository) GetProgress(taskID string) (*model.ReplayProgress, error) {
	var progress model.ReplayProgress
	if err := r.db.First(&progress, "task_id = ?", taskID).Error; err != nil {
		return nil, err
	}
	return &progress, nil
}

// UpdateProgress 更新进度
func (r *ReplayRepository) UpdateProgress(progress *model.ReplayProgress) error {
	return r.db.Save(progress).Error
}

// ==================== ReplayReport 操作 ====================

// CreateReport 创建报告
func (r *ReplayRepository) CreateReport(report *model.ReplayReport) error {
	return r.db.Create(report).Error
}

// GetReport 获取报告
func (r *ReplayRepository) GetReport(taskID string) (*model.ReplayReport, error) {
	var report model.ReplayReport
	if err := r.db.First(&report, "task_id = ?", taskID).Error; err != nil {
		return nil, err
	}
	return &report, nil
}

// UpdateReport 更新报告
func (r *ReplayRepository) UpdateReport(report *model.ReplayReport) error {
	return r.db.Save(report).Error
}

// ==================== ReplayError 操作 ====================

// CreateError 创建错误记录
func (r *ReplayRepository) CreateError(errRecord *model.ReplayError) error {
	return r.db.Create(errRecord).Error
}

// GetErrorsByTask 获取任务的所有错误
func (r *ReplayRepository) GetErrorsByTask(taskID string, limit int) ([]*model.ReplayError, error) {
	var errors []*model.ReplayError
	query := r.db.Where("task_id = ?", taskID).Order("timestamp desc")
	if limit > 0 {
		query = query.Limit(limit)
	}
	err := query.Find(&errors).Error
	return errors, err
}

// ==================== ReplayDivergence 操作 ====================

// CreateDivergence 创建差异记录
func (r *ReplayRepository) CreateDivergence(divergence *model.ReplayDivergence) error {
	return r.db.Create(divergence).Error
}

// BatchCreateDivergences 批量创建差异记录
func (r *ReplayRepository) BatchCreateDivergences(divergences []*model.ReplayDivergence, batchSize int) error {
	if len(divergences) == 0 {
		return nil
	}
	return r.db.CreateInBatches(divergences, batchSize).Error
}

// GetDivergencesByTask 获取任务的所有差异
func (r *ReplayRepository) GetDivergencesByTask(taskID string, limit int) ([]*model.ReplayDivergence, error) {
	var divergences []*model.ReplayDivergence
	query := r.db.Where("task_id = ?", taskID).Order("timestamp desc")
	if limit > 0 {
		query = query.Limit(limit)
	}
	err := query.Find(&divergences).Error
	return divergences, err
}

// GetDivergencesByTaskPaginated 分页获取差异
func (r *ReplayRepository) GetDivergencesByTaskPaginated(taskID string, offset, limit int) ([]*model.ReplayDivergence, error) {
	var divergences []*model.ReplayDivergence
	err := r.db.Where("task_id = ?", taskID).
		Order("timestamp, id").
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
	r.db.Model(&model.ReplayDivergence{}).Where("task_id = ?", taskID).Count(&totalCount)
	stats["total"] = totalCount

	// 按类型统计
	type TypeCount struct {
		DivergenceType string
		Count          int64
	}
	var typeCounts []TypeCount
	r.db.Model(&model.ReplayDivergence{}).
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
	r.db.Model(&model.SQLStatement{}).Where("task_id = ?", taskID).Count(&stmtCount)
	stats["total_statements"] = stmtCount

	// 事务总数
	var txCount int64
	r.db.Model(&model.Transaction{}).Where("task_id = ?", taskID).Count(&txCount)
	stats["total_transactions"] = txCount

	// 会话数
	var sessionCount int64
	r.db.Model(&model.SQLStatement{}).Where("task_id = ?", taskID).Distinct("session_id").Count(&sessionCount)
	stats["session_count"] = sessionCount

	// 单语句事务数
	var singleStmtTx int64
	r.db.Model(&model.Transaction{}).Where("task_id = ? AND stmt_count = 1", taskID).Count(&singleStmtTx)
	stats["single_stmt_tx"] = singleStmtTx

	// 多语句事务数
	var multiStmtTx int64
	r.db.Model(&model.Transaction{}).Where("task_id = ? AND stmt_count > 1", taskID).Count(&multiStmtTx)
	stats["multi_stmt_tx"] = multiStmtTx

	// 按类型统计
	type TypeCount struct {
		SQLType string
		Count   int64
	}
	var typeCounts []TypeCount
	r.db.Model(&model.SQLStatement{}).
		Select("sql_type, count(*) as count").
		Where("task_id = ?", taskID).
		Group("sql_type").
		Scan(&typeCounts)

	typeStats := make(map[string]int64)
	for _, tc := range typeCounts {
		typeStats[tc.SQLType] = tc.Count
	}
	stats["by_type"] = typeStats

	// 按操作统计
	var opCounts []TypeCount
	r.db.Model(&model.SQLStatement{}).
		Select("operation as sql_type, count(*) as count").
		Where("task_id = ?", taskID).
		Group("operation").
		Scan(&opCounts)

	opStats := make(map[string]int64)
	for _, oc := range opCounts {
		opStats[oc.SQLType] = oc.Count
	}
	stats["by_operation"] = opStats

	return stats, nil
}

// SaveParsedData 保存解析后的数据（事务性操作）
func (r *ReplayRepository) SaveParsedData(task *model.ReplayTask, statements []*model.SQLStatement, transactions map[string]*model.Transaction) error {
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
			zap.String("task_id", task.ID),
			zap.Int("transactions", len(txList)),
			zap.Int("statements", len(statements)))

		return nil
	})
}
