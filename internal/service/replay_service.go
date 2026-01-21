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
	Status          int                    `json:"status"` // int
	TotalStatements int64                  `json:"total_statements"`
	TotalTx         int64                  `json:"total_transactions"`
	Statistics      map[string]interface{} `json:"statistics"`
}

// Prepare 准备回放任务
func (s *ReplayService) Prepare(req *PrepareRequest) (*PrepareResult, error) {
	strTaskID := uuid.New().String()

	logger.Log.Info("Starting prepare task",
		zap.String("task_id", strTaskID),
		zap.String("log_file", req.LogFileName),
		zap.Int64("file_size", req.LogFileSize))

	// 1. 保存上传的日志文件
	logFilePath := filepath.Join(s.uploadDir, fmt.Sprintf("%s_%s", strTaskID, req.LogFileName))
	if err := s.saveUploadedFile(req.LogFile, logFilePath); err != nil {
		return nil, fmt.Errorf("failed to save log file: %w", err)
	}

	// 2. 创建任务记录 (Use TaskInfo)
	task := &model.TaskInfo{
		TaskID:      strTaskID,
		Status:      model.TaskStatusPreparing,
		DstIP:       req.DBHost,
		DstPort:     req.DBPort,
		DstUser:     req.DBUser,
		DstPass:     req.DBPassword,
		LogFilePath: logFilePath,
		CreateTime:  time.Now(),
		UpdateTime:  time.Now(),
	}

	if err := s.repo.CreateTask(task); err != nil {
		return nil, fmt.Errorf("failed to create task: %w", err)
	}

	// 3. 流式解析并入库
	batchSize := 2000

	callback := func(units []*parser.ReplayUnit, transactions map[string]*model.Transaction) error {
		// 批量保存语句
		if len(units) > 0 {
			if err := s.repo.BatchCreateStatements(units, batchSize); err != nil {
				return fmt.Errorf("failed to batch create statements: %w", err)
			}
		}

		// 批量保存/更新事务
		if len(transactions) > 0 {
			txList := make([]*model.Transaction, 0, len(transactions))
			for _, t := range transactions {
				t.TaskID = strTaskID
				txList = append(txList, t)
			}
			if err := s.repo.BatchCreateTransactions(txList, batchSize); err != nil {
				return fmt.Errorf("failed to batch create transactions: %w", err)
			}
		}
		return nil
	}

	parseResult, err := s.parser.ParseStream(logFilePath, strTaskID, batchSize, callback)
	if err != nil {
		task.Status = model.TaskStatusFailed
		s.repo.UpdateTask(task)
		// 清理已插入的数据
		s.repo.DeleteTaskData(strTaskID)
		return nil, fmt.Errorf("failed to parse log file: %w", err)
	}

	// 4. 更新任务状态
	task.Status = model.TaskStatusReady
	task.UpdateTime = time.Now()

	if err := s.repo.UpdateTask(task); err != nil {
		return nil, fmt.Errorf("failed to update task: %w", err)
	}

	// 5. 获取统计信息
	statistics, _ := s.repo.GetTaskStatistics(strTaskID)

	logger.Log.Info("Prepare task completed",
		zap.String("task_id", strTaskID),
		zap.Int64("statements", parseResult.TotalLines),
		zap.Int("transactions", len(parseResult.Transactions)))

	return &PrepareResult{
		TaskID:          strTaskID,
		Status:          task.Status,
		TotalStatements: parseResult.ParsedLines,
		TotalTx:         int64(len(parseResult.Transactions)),
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
func (s *ReplayService) GetTask(taskID string) (*model.TaskInfo, error) {
	return s.repo.GetTask(taskID)
}

// GetTaskStatistics 获取任务统计
func (s *ReplayService) GetTaskStatistics(taskID string) (map[string]interface{}, error) {
	return s.repo.GetTaskStatistics(taskID)
}

// GetStatementsByTask 获取任务的SQL语句
func (s *ReplayService) GetStatementsByTask(taskID string, offset, limit int) ([]*model.TrafficBaseline, error) {
	return s.repo.GetStatementsByTaskPaginated(taskID, offset, limit)
}

// GetTransactionsByTask 获取任务的事务列表
func (s *ReplayService) GetTransactionsByTask(taskID string) ([]*model.Transaction, error) {
	return s.repo.GetTransactionsByTask(taskID)
}

// GetStatementsByTx 获取事务的SQL语句
func (s *ReplayService) GetStatementsByTx(taskID string, txID int64) ([]*model.TrafficBaseline, error) {
	return s.repo.GetStatementsByTx(taskID, fmt.Sprintf("%d", txID))
}

// GetProgress 获取回放进度 (In-Memory or Transient)
type ReplayProgressDTO struct {
	TaskID             string    `json:"task_id"`
	TotalStatements    int64     `json:"total_statements"`
	ExecutedStatements int64     `json:"executed_statements"`
	SuccessCount       int64     `json:"success_count"`
	FailureCount       int64     `json:"failure_count"`
	StartTime          time.Time `json:"start_time"`
	LastUpdateTime     time.Time `json:"last_update_time"`
	CurrentTxID        string    `json:"current_tx_id"` // Added back for UI
}

func (s *ReplayService) GetProgress(taskID string) (*ReplayProgressDTO, error) {
	s.mu.RLock()
	replayer, ok := s.replayers[taskID]
	s.mu.RUnlock()

	if ok && replayer.IsRunning() {
		stats := replayer.GetStats()
		return &ReplayProgressDTO{
			TaskID:             taskID,
			TotalStatements:    stats.TotalStatements,
			ExecutedStatements: stats.ExecutedStatements,
			SuccessCount:       stats.SuccessCount,
			FailureCount:       stats.FailureCount,
			StartTime:          stats.StartTime,
			LastUpdateTime:     stats.LastUpdateTime,
			// CurrentTxID:        stats.CurrentTxID, // Assuming stats has it
		}, nil
	}

	return nil, fmt.Errorf("replay not running for task %s", taskID)
}

// GetReport 获取回放报告
func (s *ReplayService) GetReport(taskID string) (*model.ReplaySummary, error) {
	return s.repo.GetReport(taskID)
}

// GetDivergencesPaginated 分页获取差异记录
func (s *ReplayService) GetDivergencesPaginated(taskID string, offset, limit int) ([]*model.ReplayDetail, error) {
	return s.repo.GetDivergencesByTaskPaginated(taskID, offset, limit)
}

// ==================== 回放控制 ====================

// ReplayOptions 回放选项
type ReplayOptions struct {
	SpeedFactor float64 `json:"speed_factor"` // 回放速度因子
	MaxWorkers  int     `json:"max_workers"`  // 最大并发数
	FastMode    bool    `json:"fast_mode"`    // 快速模式
	TargetDB    string  `json:"target_db"`    // Target Database Name
}

// StartReplay 启动回放
func (s *ReplayService) StartReplay(taskID string, opts *ReplayOptions) error {
	// 获取任务信息
	task, err := s.repo.GetTask(taskID)
	if err != nil {
		return fmt.Errorf("task not found: %w", err)
	}

	if task.Status != model.TaskStatusReady && task.Status != model.TaskStatusCompleted {
		return fmt.Errorf("task is not ready, current status: %d", task.Status)
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
		Host:        task.DstIP,
		Port:        task.DstPort, // int
		User:        task.DstUser,
		Password:    task.DstPass,
		Database:    opts.TargetDB, // Pass via Options
		SpeedFactor: opts.SpeedFactor,
		MaxWorkers:  opts.MaxWorkers,
		FastMode:    opts.FastMode,
	}

	if config.Database == "" {
		config.Database = "postgres" // default
	}

	if config.SpeedFactor < 0 {
		config.SpeedFactor = 1.0
	}

	// 创建回放器
	replayer := replay.NewReplayer(config, statements)

	// 更新任务状态
	task.Status = model.TaskStatusRunning
	if err := s.repo.UpdateTask(task); err != nil {
		return fmt.Errorf("failed to update task status: %w", err)
	}

	// 保存回放器
	s.mu.Lock()
	s.replayers[taskID] = replayer
	s.mu.Unlock()

	// 启动回放
	if err := replayer.Start(context.Background()); err != nil {
		task.Status = model.TaskStatusFailed
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

	task.Status = model.TaskStatusCompleted
	s.repo.UpdateTask(task)

	// 生成报告
	report := replayer.GenerateReport()
	report.TaskID = taskID

	// 保存报告
	s.repo.CreateReport(report)

	// 保存差异记录
	// replayer.GetStats().Divergences is in memory.
	stats := replayer.GetStats()
	if len(stats.Divergences) > 0 {
		// Need generic slice conversion
		divergences := make([]*model.ReplayDetail, len(stats.Divergences))
		for i, d := range stats.Divergences {
			divergences[i] = &model.ReplayDetail{
				TaskID:         taskID,
				SQLID:          d.SQLID,
				RowsAffected:   d.RowsAffected,
				ExecDuration:   d.ExecDuration,
				DivergenceType: d.DivergenceType,
				State:          d.State,
				ErrorMessage:   d.ErrorMessage,
				Round:          1,
			}
		}
		s.repo.BatchCreateDivergences(divergences, 100)
	}

	logger.Log.Info("Replay completed",
		zap.String("task_id", taskID),
		zap.Int("executed", int(report.SuccessCnt+report.ErrorCnt)),
		zap.Int("success", report.SuccessCnt),
		zap.Int("failed", report.ErrorCnt),
		zap.Float64("fidelity", report.ReplayFidelity))

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
		task.Status = model.TaskStatusStopped
		s.repo.UpdateTask(task)
	}

	return nil
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
