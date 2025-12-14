package server

import (
	"ruc-db-replay/internal/handler"
	"ruc-db-replay/internal/service"
	"ruc-db-replay/pkg/response"

	_ "ruc-db-replay/docs" // Import swagger docs

	"github.com/gin-gonic/gin"
	swaggerFiles "github.com/swaggo/files"
	ginSwagger "github.com/swaggo/gin-swagger"
)

func setupRoutes(r *gin.Engine, replaySvc *service.ReplayService) {
	// Swagger
	r.GET("/swagger/*any", ginSwagger.WrapHandler(swaggerFiles.Handler))

	// Static files
	r.Static("/web", "./web")
	r.GET("/", func(c *gin.Context) {
		c.File("./web/index.html")
	})

	// Health Check
	r.GET("/health", func(c *gin.Context) {
		response.Success(c, gin.H{"status": "ok"})
	})

	api := r.Group("/api/v1")
	{
		api.GET("/hello", func(c *gin.Context) {
			response.Success(c, "Hello World")
		})

		// Replay routes
		replayHandler := handler.NewReplayHandler(replaySvc)
		api.POST("/replay/prepare", replayHandler.Prepare)
		api.POST("/replay/run", replayHandler.Run)
		api.POST("/replay/stop", replayHandler.Stop)
		api.GET("/replay/report", replayHandler.GetReport)
		api.GET("/replay/task", replayHandler.GetTask)
		api.GET("/replay/statements", replayHandler.GetStatements)
		api.GET("/replay/transactions", replayHandler.GetTransactions)
		api.GET("/replay/tx/statements", replayHandler.GetTxStatements)
		api.GET("/replay/divergences", replayHandler.GetDivergences)
		api.GET("/replay/progress", replayHandler.GetProgress)
	}
}
