package server

import (
	"context"
	"fmt"
	"net/http"
	"ruc-db-replay/internal/config"
	"ruc-db-replay/internal/service"
	"ruc-db-replay/pkg/logger"
	"time"

	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
)

type Server struct {
	engine    *gin.Engine
	srv       *http.Server
	port      int
	replaySvc *service.ReplayService
}

func New(cfg config.Server, replaySvc *service.ReplayService) *Server {
	if cfg.Mode == "release" {
		gin.SetMode(gin.ReleaseMode)
	}

	engine := gin.New()

	// Add default recovery middleware
	engine.Use(gin.Recovery())

	// Add custom logger middleware
	engine.Use(loggerMiddleware())

	// 设置最大文件上传大小 (10GB)
	engine.MaxMultipartMemory = 10 << 30

	setupRoutes(engine, replaySvc)

	return &Server{
		engine:    engine,
		port:      cfg.Port,
		replaySvc: replaySvc,
		srv: &http.Server{
			Addr:    fmt.Sprintf(":%d", cfg.Port),
			Handler: engine,
		},
	}
}

func (s *Server) Start() {
	go func() {
		logger.Log.Info("Starting server...", zap.Int("port", s.port))
		if err := s.srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Log.Fatal("Listen: %s\n", zap.Error(err))
		}
	}()
}

func (s *Server) Stop(ctx context.Context) error {
	logger.Log.Info("Shutting down server...")

	// 关闭 replay service
	if s.replaySvc != nil {
		if err := s.replaySvc.Close(); err != nil {
			logger.Log.Error("Failed to close replay service", zap.Error(err))
		}
	}

	return s.srv.Shutdown(ctx)
}

func loggerMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()
		path := c.Request.URL.Path
		query := c.Request.URL.RawQuery

		c.Next()

		end := time.Now()
		latency := end.Sub(start)

		if len(c.Errors) > 0 {
			for _, e := range c.Errors.Errors() {
				logger.Log.Error(e)
			}
		} else {
			logger.Log.Info("Request",
				zap.Int("status", c.Writer.Status()),
				zap.String("method", c.Request.Method),
				zap.String("path", path),
				zap.String("query", query),
				zap.String("ip", c.ClientIP()),
				zap.String("user-agent", c.Request.UserAgent()),
				zap.Duration("latency", latency),
			)
		}
	}
}
