package parser

import (
	"io"
	"ruc-db-replay/internal/model"
)

// ReplayUnit 回放单元 (Chapter 4.1.1)
// 实际上直接复用 TrafficBaseline 以减少数据转换开销
type ReplayUnit = model.TrafficBaseline

// ParseResult 解析结果
type ParseResult struct {
	Units        []*ReplayUnit
	Transactions map[string]*model.Transaction
	TotalLines   int64
	ParsedLines  int64
	ErrorLines   int64
}

// ReplayBase 插件化解析器接口 (Chapter 4.1.2)
type ReplayBase interface {
	// ParseStream 流式解析
	// 参数:
	//   reader: 日志输入流
	//   taskID: 关联任务ID
	//   batchSize: 批处理大小
	//   callback: 每解析一批数据回调一次
	ParseStream(filePath string, taskID string, batchSize int, callback func(units []*ReplayUnit, txs map[string]*model.Transaction) error) (*ParseResult, error)

	// Parse 解析整个文件 (适用于小文件或测试)
	Parse(reader io.Reader, taskID string) (*ParseResult, error)
}
