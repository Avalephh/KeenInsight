package service

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"ruc-db-replay/internal/model"
	"ruc-db-replay/internal/parser"
	"ruc-db-replay/internal/replay"
	"ruc-db-replay/internal/repository"
	"ruc-db-replay/pkg/logger"
	"sync"
	"time"

	"github.com/google/uuid"
	"go.uber.org/zap"
)

// ReplayService 回放服务
type ReplayService struct {
	repo      *repository.ReplayRepository
	parser    *parser.PgAuditParser
	uploadDir string

	// 回放器管理
	replayers map[string]*replay.Replayer
	mu        sync.RWMutex
}

// NewReplayService 创建服务实例
func NewReplayService(dbPath string, uploadDir string) (*ReplayService, error) {
	repo, err := repository.NewReplayRepository(dbPath)
	if err != nil {
		return nil, fmt.Errorf("failed to create repository: %w", err)
	}

	// 确保上传目录存在
	if err := os.MkdirAll(uploadDir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create upload dir: %w", err)
	}

	return &ReplayService{
		repo:      repo,
		parser:    parser.NewPgAuditParser(),
		uploadDir: uploadDir,
		replayers: make(map[string]*replay.Replayer),
	}, nil
}

// Close 关闭服务
func (s *ReplayService) Close() error {
	return s.repo.Close()
}

// PrepareRequest 准备请求参数
type PrepareRequest struct {
	DBHost      string
	DBPort      int
	DBUser      string
	DBPassword  string
	DBName      string
	LogFile     io.Reader
	LogFileName string
	LogFileSize int64
}

// PrepareResult 准备结果
type PrepareResult struct {
	TaskID          string                 `json:"task_id"`
	Status          string                 `json:"status"`
	TotalStatements int64                  `json:"total_statements"`
	TotalTx         int64                  `json:"total_transactions"`
	Statistics      map[string]interface{} `json:"statistics"`
}

// Prepare 准备回放任务
func (s *ReplayService) Prepare(req *PrepareRequest) (*PrepareResult, error) {
	taskID := uuid.New().String()

	logger.Log.Info("Starting prepare task",
		zap.String("task_id", taskID),
		zap.String("log_file", req.LogFileName),
		zap.Int64("file_size", req.LogFileSize))

	// 1. 保存上传的日志文件
	logFilePath := filepath.Join(s.uploadDir, fmt.Sprintf("%s_%s", taskID, req.LogFileName))
	if err := s.saveUploadedFile(req.LogFile, logFilePath); err != nil {
		return nil, fmt.Errorf("failed to save log file: %w", err)
	}

	// 2. 创建任务记录
	task := &model.ReplayTask{
		ID:             taskID,
		Status:         "preparing",
		TargetHost:     req.DBHost,
		TargetPort:     req.DBPort,
		TargetUser:     req.DBUser,
		TargetPassword: req.DBPassword,
		TargetDatabase: req.DBName,
		LogFilePath:    logFilePath,
		CreatedAt:      time.Now(),
		UpdatedAt:      time.Now(),
	}

	if err := s.repo.CreateTask(task); err != nil {
		return nil, fmt.Errorf("failed to create task: %w", err)
	}

	// 3. 流式解析并入库
	// 每 2000 条语句批量入库一次
	batchSize := 2000

	callback := func(statements []*model.SQLStatement, transactions map[string]*model.Transaction) error {
		// 批量保存语句
		if len(statements) > 0 {
			if err := s.repo.BatchCreateStatements(statements, batchSize); err != nil {
				return fmt.Errorf("failed to batch create statements: %w", err)
			}
		}

		// 批量保存/更新事务
		if len(transactions) > 0 {
			txList := make([]*model.Transaction, 0, len(transactions))
			for _, t := range transactions {
				// 创建副本并重置 ID，防止 GORM 复用 ID 导致主键冲突
				// 我们希望依靠 (task_id, vxid) 的唯一索引来进行 Upsert
				txCopy := *t
				txCopy.ID = 0
				txList = append(txList, &txCopy)
			}
			if err := s.repo.BatchCreateTransactions(txList, batchSize); err != nil {
				return fmt.Errorf("failed to batch create transactions: %w", err)
			}
		}
		return nil
	}

	parseResult, err := s.parser.ParseStream(logFilePath, taskID, batchSize, callback)
	if err != nil {
		task.Status = "failed"
		task.ErrorMessage = err.Error()
		s.repo.UpdateTask(task)
		// 清理已插入的数据
		s.repo.DeleteTaskData(taskID)
		return nil, fmt.Errorf("failed to parse log file: %w", err)
	}

	// 4. 更新任务状态和统计信息
	task.TotalStatements = parseResult.ParsedLines
	task.TotalTx = int64(len(parseResult.Transactions))
	task.Status = "ready"
	task.UpdatedAt = time.Now()

	if err := s.repo.UpdateTask(task); err != nil {
		return nil, fmt.Errorf("failed to update task: %w", err)
	}

	// 5. 获取统计信息
	statistics, _ := s.repo.GetTaskStatistics(taskID)

	logger.Log.Info("Prepare task completed",
		zap.String("task_id", taskID),
		zap.Int64("statements", task.TotalStatements),
		zap.Int64("transactions", task.TotalTx))

	return &PrepareResult{
		TaskID:          taskID,
		Status:          task.Status,
		TotalStatements: task.TotalStatements,
		TotalTx:         task.TotalTx,
		Statistics:      statistics,
	}, nil
}

// saveUploadedFile 保存上传的文件
func (s *ReplayService) saveUploadedFile(src io.Reader, dstPath string) error {
	dst, err := os.Create(dstPath)
	if err != nil {
		return err
	}
	defer dst.Close()

	_, err = io.Copy(dst, src)
	return err
}

// GetTask 获取任务信息
func (s *ReplayService) GetTask(taskID string) (*model.ReplayTask, error) {
	return s.repo.GetTask(taskID)
}

// GetTaskStatistics 获取任务统计
func (s *ReplayService) GetTaskStatistics(taskID string) (map[string]interface{}, error) {
	return s.repo.GetTaskStatistics(taskID)
}

// GetStatementsByTask 获取任务的SQL语句
func (s *ReplayService) GetStatementsByTask(taskID string, offset, limit int) ([]*model.SQLStatement, error) {
	return s.repo.GetStatementsByTaskPaginated(taskID, offset, limit)
}

// GetTransactionsByTask 获取任务的事务
func (s *ReplayService) GetTransactionsByTask(taskID string) ([]*model.Transaction, error) {
	return s.repo.GetTransactionsByTask(taskID)
}

// GetStatementsByTx 获取事务的SQL语句
func (s *ReplayService) GetStatementsByTx(taskID string, txID int64) ([]*model.SQLStatement, error) {
	return s.repo.GetStatementsByTx(taskID, txID)
}

// GetProgress 获取回放进度
func (s *ReplayService) GetProgress(taskID string) (*model.ReplayProgress, error) {
	return s.repo.GetProgress(taskID)
}

// GetReport 获取回放报告
func (s *ReplayService) GetReport(taskID string) (*model.ReplayReport, error) {
	report, err := s.repo.GetReport(taskID)
	if err != nil {
		return nil, err
	}

	// 获取错误列表
	errors, _ := s.repo.GetErrorsByTask(taskID, 100)
	report.Errors = make([]model.ReplayError, len(errors))
	for i, e := range errors {
		report.Errors[i] = *e
	}

	// 获取差异列表
	divergences, _ := s.repo.GetDivergencesByTask(taskID, 100)
	report.Divergences = make([]model.ReplayDivergence, len(divergences))
	for i, d := range divergences {
		report.Divergences[i] = *d
	}

	return report, nil
}

// GetDivergencesPaginated 分页获取差异记录
func (s *ReplayService) GetDivergencesPaginated(taskID string, offset, limit int) ([]*model.ReplayDivergence, error) {
	return s.repo.GetDivergencesByTaskPaginated(taskID, offset, limit)
}

// ==================== 回放控制 ====================

// ReplayOptions 回放选项
type ReplayOptions struct {
	SpeedFactor float64 `json:"speed_factor"` // 回放速度因子
	MaxWorkers  int     `json:"max_workers"`  // 最大并发数
	FastMode    bool    `json:"fast_mode"`    // 快速模式
}

// StartReplay 启动回放
func (s *ReplayService) StartReplay(taskID string, opts *ReplayOptions) error {
	// 获取任务信息
	task, err := s.repo.GetTask(taskID)
	if err != nil {
		return fmt.Errorf("task not found: %w", err)
	}

	if task.Status != "ready" && task.Status != "completed" {
		return fmt.Errorf("task is not ready, current status: %s", task.Status)
	}

	// 检查是否已有运行中的回放器
	s.mu.RLock()
	if r, ok := s.replayers[taskID]; ok && r.IsRunning() {
		s.mu.RUnlock()
		return fmt.Errorf("replay is already running")
	}
	s.mu.RUnlock()

	// 获取所有 SQL 语句
	statements, err := s.repo.GetStatementsByTask(taskID)
	if err != nil {
		return fmt.Errorf("failed to get statements: %w", err)
	}

	if len(statements) == 0 {
		return fmt.Errorf("no statements to replay")
	}

	logger.Log.Info("Starting replay",
		zap.String("task_id", taskID),
		zap.Int("statements", len(statements)),
		zap.Float64("speed_factor", opts.SpeedFactor),
		zap.Bool("fast_mode", opts.FastMode))

	// 创建回放配置
	config := replay.ReplayConfig{
		Host:        task.TargetHost,
		Port:        task.TargetPort,
		User:        task.TargetUser,
		Password:    task.TargetPassword,
		Database:    task.TargetDatabase,
		SpeedFactor: opts.SpeedFactor,
		MaxWorkers:  opts.MaxWorkers,
		FastMode:    opts.FastMode,
	}

	if config.SpeedFactor < 0 {
		config.SpeedFactor = 1.0
	}

	// 创建回放器
	replayer := replay.NewReplayer(config, statements)

	// 更新任务状态
	now := time.Now()
	task.Status = "running"
	task.StartedAt = &now
	if err := s.repo.UpdateTask(task); err != nil {
		return fmt.Errorf("failed to update task status: %w", err)
	}

	// 创建进度记录
	progress := &model.ReplayProgress{
		TaskID:          taskID,
		TotalStatements: int64(len(statements)),
		StartTime:       now,
		LastUpdateTime:  now,
	}
	s.repo.CreateProgress(progress)

	// 保存回放器
	s.mu.Lock()
	s.replayers[taskID] = replayer
	s.mu.Unlock()

	// 启动回放
	if err := replayer.Start(context.Background()); err != nil {
		task.Status = "failed"
		task.ErrorMessage = err.Error()
		s.repo.UpdateTask(task)
		return fmt.Errorf("failed to start replay: %w", err)
	}

	// 启动后台协程监控回放完成
	go s.monitorReplayCompletion(taskID, replayer)

	return nil
}

// monitorReplayCompletion 监控回放完成
func (s *ReplayService) monitorReplayCompletion(taskID string, replayer *replay.Replayer) {
	replayer.Wait()

	// 回放完成，更新状态
	task, err := s.repo.GetTask(taskID)
	if err != nil {
		logger.Log.Error("Failed to get task after replay", zap.Error(err))
		return
	}

	now := time.Now()
	task.Status = "completed"
	task.CompletedAt = &now
	s.repo.UpdateTask(task)

	// 生成报告
	report := replayer.GenerateReport()
	report.TaskID = taskID
	report.TotalTx = task.TotalTx

	// 保存报告
	s.repo.CreateReport(report)

	// 保存错误记录
	for _, e := range report.Errors {
		errRecord := &model.ReplayError{
			TaskID:    taskID,
			TxID:      e.TxID,
			StmtID:    e.StmtID,
			SQL:       e.SQL,
			Error:     e.Error,
			Timestamp: e.Timestamp,
		}
		s.repo.CreateError(errRecord)
	}

	// 保存差异记录
	divergences := make([]*model.ReplayDivergence, len(report.Divergences))
	for i, d := range report.Divergences {
		divergences[i] = &model.ReplayDivergence{
			TaskID:               taskID,
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
	if len(divergences) > 0 {
		s.repo.BatchCreateDivergences(divergences, 100)
	}

	logger.Log.Info("Replay completed",
		zap.String("task_id", taskID),
		zap.Int64("executed", report.ExecutedStmts),
		zap.Int64("success", report.SuccessStmts),
		zap.Int64("failed", report.FailedStmts),
		zap.Int64("divergences", report.DivergenceCount),
		zap.Int64("rows_diff", report.RowsAffectedDiff),
		zap.Int64("error_diff", report.ErrorStateDiff))

	// 清理回放器
	s.mu.Lock()
	delete(s.replayers, taskID)
	s.mu.Unlock()
}

// StopReplay 停止回放
func (s *ReplayService) StopReplay(taskID string) error {
	s.mu.RLock()
	replayer, ok := s.replayers[taskID]
	s.mu.RUnlock()

	if !ok {
		return fmt.Errorf("no active replay found for task %s", taskID)
	}

	replayer.Stop()

	// 更新任务状态
	task, err := s.repo.GetTask(taskID)
	if err == nil {
		now := time.Now()
		task.Status = "stopped"
		task.CompletedAt = &now
		s.repo.UpdateTask(task)
	}

	return nil
}

// GetReplayProgress 获取实时回放进度
func (s *ReplayService) GetReplayProgress(taskID string) (*model.ReplayProgress, error) {
	s.mu.RLock()
	replayer, ok := s.replayers[taskID]
	s.mu.RUnlock()

	if ok && replayer.IsRunning() {
		// 从运行中的回放器获取实时进度
		stats := replayer.GetStats()
		progress := &model.ReplayProgress{
			TaskID:             taskID,
			TotalStatements:    stats.TotalStatements,
			ExecutedStatements: stats.ExecutedStatements,
			SuccessCount:       stats.SuccessCount,
			FailureCount:       stats.FailureCount,
			CurrentTxID:        stats.CurrentTxID,
			StartTime:          stats.StartTime,
			LastUpdateTime:     stats.LastUpdateTime,
		}

		// 更新数据库中的进度
		s.repo.UpdateProgress(progress)

		return progress, nil
	}

	// 从数据库获取最后的进度
	return s.repo.GetProgress(taskID)
}

// IsReplayRunning 检查回放是否正在运行
func (s *ReplayService) IsReplayRunning(taskID string) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()

	if replayer, ok := s.replayers[taskID]; ok {
		return replayer.IsRunning()
	}
	return false
}
