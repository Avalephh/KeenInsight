// @title           RUC DB Replay API
// @version         1.0
// @description     This is a database replay tool API server.

// @contact.name    API Support
// @contact.email   support@example.com

// @host            localhost:8080
// @BasePath        /api/v1

package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"ruc-db-replay/internal/config"
	"ruc-db-replay/internal/server"
	"ruc-db-replay/internal/service"
	"ruc-db-replay/pkg/logger"

	"go.uber.org/zap"
)

func main() {
	var configPath string
	flag.StringVar(&configPath, "c", "", "config file path")
	flag.Parse()

	// 1. Load Config
	cfg, err := config.Load(configPath)
	if err != nil {
		log.Fatalf("Failed to load config: %v", err)
	}

	// 2. Initialize Logger
	logger.Init(cfg.Logger)
	defer logger.Sync()
	logger.Log.Info("Config loaded successfully")

	// 3. Initialize Replay Service (使用 SQLite 存储解析后的日志)
	replaySvc, err := service.NewReplayService("data/replay.db", "data/uploads")
	if err != nil {
		log.Fatalf("Failed to initialize replay service: %v", err)
	}
	logger.Log.Info("Replay service initialized")

	// 4. Start Server
	srv := server.New(cfg.Server, replaySvc)
	srv.Start()

	// 5. Graceful Shutdown
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	logger.Log.Info("Shutting down server...")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := srv.Stop(ctx); err != nil {
		logger.Log.Fatal("Server forced to shutdown", zap.Error(err))
	}

	logger.Log.Info("Server exiting")
}
