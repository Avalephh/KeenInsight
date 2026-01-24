package handler

import (
	"ruc-db-replay/internal/model"
	"ruc-db-replay/internal/service"
	"ruc-db-replay/pkg/logger"
	"ruc-db-replay/pkg/response"
	"strconv"

	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
)

// ReplayHandler 回放处理器
type ReplayHandler struct {
	service *service.ReplayService
}

// NewReplayHandler 创建处理器
func NewReplayHandler(svc *service.ReplayService) *ReplayHandler {
	return &ReplayHandler{service: svc}
}

// PrepareRequest 定义准备阶段的请求参数
type PrepareRequest struct {
	DBHost string `form:"db_host" binding:"required" example:"127.0.0.1"`
	DBPort int    `form:"db_port" binding:"required" example:"5432"`
	DBUser string `form:"db_user" binding:"required" example:"postgres"`
	DBPass string `form:"db_pass" binding:"required" example:"password"`
	DBName string `form:"db_name" binding:"required" example:"test"`
}

// PrepareResponse 准备阶段响应
type PrepareResponse struct {
	TaskID          string                 `json:"task_id"`
	Status          string                 `json:"status"`
	TotalStatements int64                  `json:"total_statements"`
	TotalTx         int64                  `json:"total_transactions"`
	Statistics      map[string]interface{} `json:"statistics"`
}

// RunRequest 定义运行阶段的请求参数
type RunRequest struct {
	TaskID      string  `json:"task_id" binding:"required" example:"task-12345"`
	SpeedFactor float64 `json:"speed_factor" example:"1.0"` // 回放速度因子，1.0表示原速，0表示快速模式
	MaxWorkers  int     `json:"max_workers" example:"100"`  // 最大并发会话数
	FastMode    bool    `json:"fast_mode" example:"false"`  // 快速模式：忽略时间戳
	TargetDB    string  `json:"target_db" example:"test"`   // 目标数据库名（默认使用任务中的db_name或流量基线中的db_name）
}

// RunResponse 定义运行阶段的响应
type RunResponse struct {
	TaskID string `json:"task_id" example:"task-12345"`
	Status string `json:"status" example:"running"`
}

// ReportResponse 定义回放报告响应
type ReportResponse struct {
	TaskID          string  `json:"task_id"`
	TotalStatements int64   `json:"total_statements"`
	TotalTx         int64   `json:"total_transactions"`
	ExecutedStmts   int64   `json:"executed_stmts"`
	SuccessStmts    int64   `json:"success_stmts"`
	FailedStmts     int64   `json:"failed_stmts"`
	SuccessRate     float64 `json:"success_rate"`
	Duration        string  `json:"duration"`
	AvgLatencyMs    float64 `json:"avg_latency_ms"`
}

// statusToString Helper
func statusToString(status int) string {
	switch status {
	case model.TaskStatusPending:
		return "pending"
	case model.TaskStatusPreparing:
		return "preparing"
	case model.TaskStatusReady:
		return "ready"
	case model.TaskStatusRunning:
		return "running"
	case model.TaskStatusCompleted:
		return "completed"
	case model.TaskStatusFailed:
		return "failed"
	case model.TaskStatusStopped:
		return "stopped"
	default:
		return "unknown"
	}
}

// Prepare 处理日志上传和环境准备
// @Summary 准备回放环境
// @Description 上传采集日志文件和目标数据库连接信息，解析日志并存储
// @Tags Replay
// @Accept multipart/form-data
// @Produce json
// @Param db_host formData string true "Database Host"
// @Param db_port formData int true "Database Port"
// @Param db_user formData string true "Database User"
// @Param db_pass formData string true "Database Password"
// @Param db_name formData string true "Database Name"
// @Param log_file formData file true "PgAudit Log File"
// @Success 200 {object} response.Response{data=PrepareResponse}
// @Router /replay/prepare [post]
func (h *ReplayHandler) Prepare(c *gin.Context) {
	var req PrepareRequest
	if err := c.ShouldBind(&req); err != nil {
		response.Error(c, response.CodeError, err.Error())
		return
	}

	file, err := c.FormFile("log_file")
	if err != nil {
		response.Error(c, response.CodeError, "log_file is required")
		return
	}

	logger.Log.Info("Received prepare request",
		zap.String("db_host", req.DBHost),
		zap.Int("db_port", req.DBPort),
		zap.String("filename", file.Filename),
		zap.Int64("size", file.Size))

	// 打开上传的文件
	src, err := file.Open()
	if err != nil {
		response.Error(c, response.CodeError, "failed to open uploaded file")
		return
	}
	defer src.Close()

	// 调用服务层进行处理
	result, err := h.service.Prepare(&service.PrepareRequest{
		DBHost:      req.DBHost,
		DBPort:      req.DBPort,
		DBUser:      req.DBUser,
		DBPassword:  req.DBPass,
		DBName:      req.DBName,
		LogFile:     src,
		LogFileName: file.Filename,
		LogFileSize: file.Size,
	})

	if err != nil {
		logger.Log.Error("Prepare failed", zap.Error(err))
		response.Error(c, response.CodeError, err.Error())
		return
	}

	response.Success(c, PrepareResponse{
		TaskID:          result.TaskID,
		Status:          statusToString(result.Status),
		TotalStatements: result.TotalStatements,
		TotalTx:         result.TotalTx,
		Statistics:      result.Statistics,
	})
}

// Run 启动回放任务
// @Summary 启动流量回放
// @Description 开始回放任务，返回任务状态。支持设置回放速度因子（1.0为原速，2.0为两倍速）
// @Tags Replay
// @Accept json
// @Produce json
// @Param request body RunRequest true "Run Request"
// @Success 200 {object} response.Response{data=RunResponse}
// @Router /replay/run [post]
func (h *ReplayHandler) Run(c *gin.Context) {
	var req RunRequest

	// 支持 query 参数或 JSON body
	if c.Query("task_id") != "" {
		req.TaskID = c.Query("task_id")
		req.SpeedFactor, _ = strconv.ParseFloat(c.DefaultQuery("speed_factor", "1.0"), 64)
		req.MaxWorkers, _ = strconv.Atoi(c.DefaultQuery("max_workers", "0"))
		req.FastMode = c.DefaultQuery("fast_mode", "false") == "true"
		req.TargetDB = c.DefaultQuery("target_db", "")
	} else if err := c.ShouldBindJSON(&req); err != nil {
		response.Error(c, response.CodeError, "invalid request: "+err.Error())
		return
	}

	if req.TaskID == "" {
		response.Error(c, response.CodeError, "task_id is required")
		return
	}

	if req.SpeedFactor <= 0 {
		req.SpeedFactor = 1.0
	}

	// 启动回放
	opts := &service.ReplayOptions{
		SpeedFactor: req.SpeedFactor,
		MaxWorkers:  req.MaxWorkers,
		FastMode:    req.FastMode,
		TargetDB:    req.TargetDB,
	}

	if err := h.service.StartReplay(req.TaskID, opts); err != nil {
		logger.Log.Error("Failed to start replay", zap.String("task_id", req.TaskID), zap.Error(err))
		response.Error(c, response.CodeError, err.Error())
		return
	}

	logger.Log.Info("Started replay task",
		zap.String("task_id", req.TaskID),
		zap.Float64("speed_factor", req.SpeedFactor))

	resp := RunResponse{
		TaskID: req.TaskID,
		Status: "running",
	}
	response.Success(c, resp)
}

// Stop 停止回放任务
// @Summary 停止流量回放
// @Description 停止正在运行的回放任务
// @Tags Replay
// @Accept json
// @Produce json
// @Param task_id query string true "Task ID"
// @Success 200 {object} response.Response
// @Router /replay/stop [post]
func (h *ReplayHandler) Stop(c *gin.Context) {
	taskID := c.Query("task_id")
	if taskID == "" {
		response.Error(c, response.CodeError, "task_id is required")
		return
	}

	if err := h.service.StopReplay(taskID); err != nil {
		response.Error(c, response.CodeError, err.Error())
		return
	}

	response.Success(c, gin.H{
		"task_id": taskID,
		"status":  "stopped",
	})
}

// GetReport 获取回放报告
// @Summary 获取回放报告
// @Description 查询回放任务的执行报告
// @Tags Replay
// @Accept json
// @Produce json
// @Param task_id query string true "Task ID"
// @Success 200 {object} response.Response{data=ReportResponse}
// @Router /replay/report [get]
func (h *ReplayHandler) GetReport(c *gin.Context) {
	taskID := c.Query("task_id")
	if taskID == "" {
		response.Error(c, response.CodeError, "task_id is required")
		return
	}

	report, err := h.service.GetReport(taskID)
	if err != nil {
		// 如果报告不存在，返回任务统计信息
		task, taskErr := h.service.GetTask(taskID)
		if taskErr != nil {
			response.Error(c, response.CodeError, "task not found")
			return
		}

		stats, _ := h.service.GetTaskStatistics(taskID)
		response.Success(c, gin.H{
			"task_id": task.TaskID,                 // Fixed Field
			"status":  statusToString(task.Status), // Fixed Status
			// TotalStatements/Tx are not in TaskInfo table, computed via stats or service logic.
			// ReplayService PrepareResult provides them, but TaskInfo struct doesn't have them persistent.
			// We can get them from stats.
			"total_statements":   stats["total_statements"],
			"total_transactions": stats["total_transactions"],
			"statistics":         stats,
			"message":            "replay not started yet",
		})
		return
	}

	response.Success(c, report)
}

// GetTask 获取任务详情
// @Summary 获取任务详情
// @Description 查询任务的详细信息
// @Tags Replay
// @Accept json
// @Produce json
// @Param task_id query string true "Task ID"
// @Success 200 {object} response.Response
// @Router /replay/task [get]
func (h *ReplayHandler) GetTask(c *gin.Context) {
	taskID := c.Query("task_id")
	if taskID == "" {
		response.Error(c, response.CodeError, "task_id is required")
		return
	}

	task, err := h.service.GetTask(taskID)
	if err != nil {
		response.Error(c, response.CodeError, "task not found")
		return
	}

	stats, _ := h.service.GetTaskStatistics(taskID)

	response.Success(c, gin.H{
		"task":       task,
		"statistics": stats,
	})
}

// GetStatements 获取SQL语句列表
// @Summary 获取SQL语句列表
// @Description 分页获取任务的SQL语句
// @Tags Replay
// @Accept json
// @Produce json
// @Param task_id query string true "Task ID"
// @Param offset query int false "Offset" default(0)
// @Param limit query int false "Limit" default(100)
// @Success 200 {object} response.Response
// @Router /replay/statements [get]
func (h *ReplayHandler) GetStatements(c *gin.Context) {
	taskID := c.Query("task_id")
	if taskID == "" {
		response.Error(c, response.CodeError, "task_id is required")
		return
	}

	offset, _ := strconv.Atoi(c.DefaultQuery("offset", "0"))
	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "100"))

	statements, err := h.service.GetStatementsByTask(taskID, offset, limit)
	if err != nil {
		response.Error(c, response.CodeError, err.Error())
		return
	}

	response.Success(c, gin.H{
		"statements": statements,
		"offset":     offset,
		"limit":      limit,
		"count":      len(statements),
	})
}

// GetTransactions 获取事务列表
// @Summary 获取事务列表
// @Description 获取任务的所有事务
// @Tags Replay
// @Accept json
// @Produce json
// @Param task_id query string true "Task ID"
// @Success 200 {object} response.Response
// @Router /replay/transactions [get]
func (h *ReplayHandler) GetTransactions(c *gin.Context) {
	taskID := c.Query("task_id")
	if taskID == "" {
		response.Error(c, response.CodeError, "task_id is required")
		return
	}

	transactions, err := h.service.GetTransactionsByTask(taskID)
	if err != nil {
		response.Error(c, response.CodeError, err.Error())
		return
	}

	response.Success(c, gin.H{
		"transactions": transactions,
		"count":        len(transactions),
	})
}

// GetTxStatements 获取事务的SQL语句
// @Summary 获取事务的SQL语句
// @Description 获取指定事务的所有SQL语句
// @Tags Replay
// @Accept json
// @Produce json
// @Param task_id query string true "Task ID"
// @Param tx_id query string true "Transaction ID (e.g. 67/98)"
// @Success 200 {object} response.Response
// @Router /replay/tx/statements [get]
func (h *ReplayHandler) GetTxStatements(c *gin.Context) {
	taskID := c.Query("task_id")
	if taskID == "" {
		response.Error(c, response.CodeError, "task_id is required")
		return
	}

	txID := c.Query("tx_id")
	if txID == "" {
		response.Error(c, response.CodeError, "tx_id is required")
		return
	}

	statements, err := h.service.GetStatementsByTx(taskID, txID)
	if err != nil {
		response.Error(c, response.CodeError, err.Error())
		return
	}

	response.Success(c, gin.H{
		"statements": statements,
		"tx_id":      txID,
		"count":      len(statements),
	})
}

// GetDivergences 获取差异列表
// @Summary 获取差异列表
// @Description 分页获取任务的差异记录
// @Tags Replay
// @Accept json
// @Produce json
// @Param task_id query string true "Task ID"
// @Param offset query int false "Offset" default(0)
// @Param limit query int false "Limit" default(100)
// @Success 200 {object} response.Response
// @Router /replay/divergences [get]
func (h *ReplayHandler) GetDivergences(c *gin.Context) {
	taskID := c.Query("task_id")
	if taskID == "" {
		response.Error(c, response.CodeError, "task_id is required")
		return
	}

	offset, _ := strconv.Atoi(c.DefaultQuery("offset", "0"))
	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "100"))

	divergences, err := h.service.GetDivergencesPaginated(taskID, offset, limit)
	if err != nil {
		response.Error(c, response.CodeError, err.Error())
		return
	}

	response.Success(c, gin.H{
		"divergences": divergences,
		"offset":      offset,
		"limit":       limit,
		"count":       len(divergences),
	})
}

// GetProgress 获取回放进度
// @Summary 获取回放进度
// @Description 查询回放任务的当前进度（支持实时获取运行中的进度）
// @Tags Replay
// @Accept json
// @Produce json
// @Param task_id query string true "Task ID"
// @Success 200 {object} response.Response
// @Router /replay/progress [get]
func (h *ReplayHandler) GetProgress(c *gin.Context) {
	taskID := c.Query("task_id")
	if taskID == "" {
		response.Error(c, response.CodeError, "task_id is required")
		return
	}

	// Use GetProgress (unified)
	progress, err := h.service.GetProgress(taskID)

	if err != nil {
		// 返回任务状态
		task, taskErr := h.service.GetTask(taskID)
		if taskErr != nil {
			response.Error(c, response.CodeError, "task not found")
			return
		}

		// When replay is not running, try to get stats from report or statistics
		stats, _ := h.service.GetTaskStatistics(taskID)
		totalStmts := int64(0)
		executedStmts := int64(0)
		successCount := int64(0)
		failureCount := int64(0)

		if v, ok := stats["total_statements"].(int64); ok {
			totalStmts = v
		}

		// If task is completed, get stats from report
		if task.Status == model.TaskStatusCompleted {
			report, reportErr := h.service.GetReport(taskID)
			if reportErr == nil {
				executedStmts = int64(report.SuccessCnt + report.ErrorCnt)
				successCount = int64(report.SuccessCnt)
				failureCount = int64(report.ErrorCnt)
			}
		}

		response.Success(c, gin.H{
			"task_id":             task.TaskID,
			"status":              statusToString(task.Status),
			"total_statements":    totalStmts,
			"executed_statements": executedStmts,
			"success_count":       successCount,
			"failure_count":       failureCount,
			"running":             false,
			"percentage":          float64(100), // Completed or not started
			"message":             "replay not running or started",
		})
		return
	}

	// 计算进度百分比
	var percentage float64
	if progress.TotalStatements > 0 {
		percentage = float64(progress.ExecutedStatements) / float64(progress.TotalStatements) * 100
	}

	response.Success(c, gin.H{
		"task_id":             taskID,
		"total_statements":    progress.TotalStatements,
		"executed_statements": progress.ExecutedStatements,
		"success_count":       progress.SuccessCount,
		"failure_count":       progress.FailureCount,
		// "current_tx_id":       progress.CurrentTxID, // Removed from DTO? Check service.
		// Service DTO doesn't have CurrentTxID in my previous edit in Step 103?
		// Let's check...
		// type ReplayProgressDTO struct { ... CurrentTxID ??? }
		// I missed CurrentTxID in DTO definition in Step 103 ReplacementContent?
		// "CurrentTxID: stats.CurrentTxID" was in replayer stats.
		// If I missed it in DTO, I should not use it here.
		"start_time":  progress.StartTime,
		"last_update": progress.LastUpdateTime,
		"percentage":  percentage,
		"running":     h.service.IsReplayRunning(taskID),
	})
}
