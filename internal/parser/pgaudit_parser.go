package parser

import (
	"bufio"
	"fmt"
	"hash/fnv"
	"io"
	"os"
	"ruc-db-replay/internal/model"
	"ruc-db-replay/pkg/logger"
	"strconv"
	"strings"
	"time"

	"go.uber.org/zap"
)

// DefaultLogLinePrefix 默认的 PostgreSQL log_line_prefix 配置
const DefaultLogLinePrefix = "%u,%d,%n,%e,%c,%v "

// PgAuditParser pgaudit 日志解析器
type PgAuditParser struct {
	// LogLinePrefix PostgreSQL 的 log_line_prefix 配置
	// 支持的占位符: %u, %d, %n, %e, %c, %v, %Q
	// 分隔符应为逗号(,)以便正确解析
	LogLinePrefix string

	// prefixFields 根据 LogLinePrefix 解析出的字段顺序
	// 例如: %u,%d,%n 解析为 ["u", "d", "n"]
	prefixFields []string
}

// Ensure PgAuditParser implements ReplayBase
var _ ReplayBase = (*PgAuditParser)(nil)

// NewPgAuditParser 创建解析器（使用默认 log_line_prefix）
func NewPgAuditParser() *PgAuditParser {
	return NewPgAuditParserWithPrefix(DefaultLogLinePrefix)
}

// NewPgAuditParserWithPrefix 使用指定的 log_line_prefix 创建解析器
func NewPgAuditParserWithPrefix(logLinePrefix string) *PgAuditParser {
	if logLinePrefix == "" {
		logLinePrefix = DefaultLogLinePrefix
	}
	p := &PgAuditParser{
		LogLinePrefix: logLinePrefix,
	}
	p.prefixFields = p.parseLogLinePrefix(logLinePrefix)
	return p
}

// parseLogLinePrefix 解析 log_line_prefix 字符串，提取字段顺序
// 例如: "%u,%d,%n,%e,%c,%v,%Q " -> ["u", "d", "n", "e", "c", "v", "Q"]
func (p *PgAuditParser) parseLogLinePrefix(prefix string) []string {
	var fields []string
	for i := 0; i < len(prefix); i++ {
		if prefix[i] == '%' && i+1 < len(prefix) {
			nextChar := prefix[i+1]
			// 跳过 %% 转义
			if nextChar == '%' {
				i++
				continue
			}
			// 记录有效的占位符字符
			switch nextChar {
			case 'u', 'd', 'n', 'e', 'c', 'v', 'Q', 'a', 'r', 'h', 'L', 'b', 'p', 'P', 't', 'm', 'i', 'l', 's', 'x', 'q':
				fields = append(fields, string(nextChar))
			}
			i++
		}
	}
	return fields
}

// getFieldIndex 获取指定字段在前缀中的索引位置
// 返回 -1 表示字段不存在
func (p *PgAuditParser) getFieldIndex(fieldChar string) int {
	for i, f := range p.prefixFields {
		if f == fieldChar {
			return i
		}
	}
	return -1
}

// ParseStream 流式解析日志文件
func (p *PgAuditParser) ParseStream(filePath string, taskID string, batchSize int, callback func(units []*ReplayUnit, txs map[string]*model.Transaction) error) (*ParseResult, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return nil, fmt.Errorf("failed to open file: %w", err)
	}
	defer file.Close()

	result := &ParseResult{
		Units:        make([]*ReplayUnit, 0),
		Transactions: make(map[string]*model.Transaction),
	}

	scanner := bufio.NewScanner(file)
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 10*1024*1024)

	vxidSeqMap := make(map[string]int)
	batchUnits := make([]*ReplayUnit, 0, batchSize)
	batchTxs := make(map[string]*model.Transaction)

	for scanner.Scan() {
		result.TotalLines++
		line := scanner.Text()

		unit, err := p.parseLine(line, taskID)
		if err != nil {
			result.ErrorLines++
			continue
		}

		if unit == nil {
			continue
		}

		// 设置事务内序号
		vxidSeqMap[unit.TxID]++
		unit.SeqInTx = vxidSeqMap[unit.TxID]

		result.ParsedLines++

		// 更新事务信息
		tx, exists := result.Transactions[unit.TxID]
		if !exists {
			tx = &model.Transaction{
				TxID:      unit.TxID,
				SessionID: unit.SessionID,
				StartTime: time.UnixMilli(unit.Timestamp),
				EndTime:   time.UnixMilli(unit.Timestamp),
			}
			result.Transactions[unit.TxID] = tx
		}

		if time.UnixMilli(unit.Timestamp).After(tx.EndTime) {
			tx.EndTime = time.UnixMilli(unit.Timestamp)
		}
		if time.UnixMilli(unit.Timestamp).Before(tx.StartTime) {
			tx.StartTime = time.UnixMilli(unit.Timestamp)
		}
		if strings.ToUpper(unit.Operation) == "COMMIT" {
			tx.Committed = true
		}
		tx.StmtCount++
		// Use TxID as key (it stores vxid from log)
		batchUnits = append(batchUnits, unit)
		batchTxs[unit.TxID] = tx

		if len(batchUnits) >= batchSize {
			if err := callback(batchUnits, batchTxs); err != nil {
				return result, fmt.Errorf("callback failed: %w", err)
			}
			batchUnits = make([]*ReplayUnit, 0, batchSize)
			batchTxs = make(map[string]*model.Transaction)
		}
	}

	if len(batchUnits) > 0 {
		if err := callback(batchUnits, batchTxs); err != nil {
			return result, fmt.Errorf("callback failed: %w", err)
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scanner error: %w", err)
	}

	return result, nil
}

// Parse 解析日志流 (实现接口)
func (p *PgAuditParser) Parse(reader io.Reader, taskID string) (*ParseResult, error) {
	result := &ParseResult{
		Units:        make([]*ReplayUnit, 0),
		Transactions: make(map[string]*model.Transaction),
	}

	scanner := bufio.NewScanner(reader)
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 10*1024*1024)

	vxidSeqMap := make(map[string]int)

	for scanner.Scan() {
		result.TotalLines++
		line := scanner.Text()

		unit, err := p.parseLine(line, taskID)
		if err != nil {
			result.ErrorLines++
			if result.ErrorLines <= 10 && logger.Log != nil {
				logger.Log.Warn("Failed to parse line",
					zap.Int64("line_num", result.TotalLines),
					zap.Error(err))
			}
			continue
		}

		if unit == nil {
			continue
		}

		vxidSeqMap[unit.TxID]++
		unit.SeqInTx = vxidSeqMap[unit.TxID]

		result.Units = append(result.Units, unit)
		result.ParsedLines++

		tx, exists := result.Transactions[unit.TxID]
		if !exists {
			tx = &model.Transaction{
				TxID:      unit.TxID,
				SessionID: unit.SessionID,
				StartTime: time.UnixMilli(unit.Timestamp),
				EndTime:   time.UnixMilli(unit.Timestamp),
			}
			result.Transactions[unit.TxID] = tx
		}

		if time.UnixMilli(unit.Timestamp).After(tx.EndTime) {
			tx.EndTime = time.UnixMilli(unit.Timestamp)
		}
		if time.UnixMilli(unit.Timestamp).Before(tx.StartTime) {
			tx.StartTime = time.UnixMilli(unit.Timestamp)
		}
		if strings.ToUpper(unit.Operation) == "COMMIT" {
			tx.Committed = true
		}
		tx.StmtCount++

	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scanner error: %w", err)
	}

	return result, nil
}

// parseLine 解析单行日志
// 根据 LogLinePrefix 配置动态解析前缀字段
func (p *PgAuditParser) parseLine(line string, taskID string) (*model.TrafficBaseline, error) {
	if strings.TrimSpace(line) == "" {
		return nil, nil
	}

	// 查找 LOG: AUDIT: 分隔点
	// 前缀末尾可能有空格（取决于 log_line_prefix 配置）
	prefixEnd := strings.Index(line, " LOG:  AUDIT:")
	if prefixEnd == -1 {
		return nil, nil
	}

	prefixStr := line[:prefixEnd]
	auditContent := line[prefixEnd+len(" LOG:  AUDIT:"):]
	auditContent = strings.TrimSpace(auditContent)

	fields := strings.Split(prefixStr, ",")

	// 根据 LogLinePrefix 动态获取各字段
	var username, database, timeEpochStr, sessionID, vxid string

	if idx := p.getFieldIndex("u"); idx >= 0 && idx < len(fields) {
		username = strings.TrimSpace(fields[idx])
	}
	if idx := p.getFieldIndex("d"); idx >= 0 && idx < len(fields) {
		database = strings.TrimSpace(fields[idx])
	}
	if idx := p.getFieldIndex("n"); idx >= 0 && idx < len(fields) {
		timeEpochStr = strings.TrimSpace(fields[idx])
	}
	if idx := p.getFieldIndex("c"); idx >= 0 && idx < len(fields) {
		sessionID = strings.TrimSpace(fields[idx])
	}
	if idx := p.getFieldIndex("v"); idx >= 0 && idx < len(fields) {
		vxid = strings.TrimSpace(fields[idx])
	}

	// 如果没有时间戳字段，尝试使用 %m (带毫秒的时间戳)
	var originTime time.Time
	var err error
	if timeEpochStr != "" {
		originTime, err = p.parseEpoch(timeEpochStr)
		if err != nil {
			return nil, fmt.Errorf("failed to parse epoch time '%s': %w", timeEpochStr, err)
		}
	} else if idx := p.getFieldIndex("m"); idx >= 0 && idx < len(fields) {
		// %m 格式: 2006-01-02 15:04:05.000 MST
		timeStr := strings.TrimSpace(fields[idx])
		originTime, err = p.parseTimestamp(timeStr)
		if err != nil {
			return nil, fmt.Errorf("failed to parse timestamp '%s': %w", timeStr, err)
		}
	} else if idx := p.getFieldIndex("t"); idx >= 0 && idx < len(fields) {
		// %t 格式: 2006-01-02 15:04:05 MST
		timeStr := strings.TrimSpace(fields[idx])
		originTime, err = p.parseTimestamp(timeStr)
		if err != nil {
			return nil, fmt.Errorf("failed to parse time '%s': %w", timeEpochStr, err)
		}
	}

	unit, err := p.parseAuditContent(auditContent)
	if err != nil {
		return nil, fmt.Errorf("failed to parse audit content: %w", err)
	}

	unit.TaskID = taskID
	unit.UserName = username
	unit.DBName = database
	unit.Timestamp = originTime.UnixMilli()
	unit.SessionID = sessionID
	unit.TxID = vxid

	return unit, nil
}

// parseEpoch 解析 Unix Timestamp (seconds.milliseconds)
func (p *PgAuditParser) parseEpoch(epochStr string) (time.Time, error) {
	parts := strings.Split(epochStr, ".")
	sec, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil {
		return time.Time{}, err
	}
	nsec := int64(0)
	if len(parts) > 1 {
		fracStr := parts[1]
		if len(fracStr) > 9 {
			fracStr = fracStr[:9]
		}
		fracVal, err := strconv.ParseInt(fracStr, 10, 64)
		if err == nil {
			// e.g. .123 -> 123000000
			multiplier := 1
			for i := 0; i < 9-len(fracStr); i++ {
				multiplier *= 10
			}
			nsec = fracVal * int64(multiplier)
		}
	}
	return time.Unix(sec, nsec), nil
}

// parseTimestamp 解析 PostgreSQL 时间戳格式
// 支持 %m 格式: 2006-01-02 15:04:05.000 MST
// 支持 %t 格式: 2006-01-02 15:04:05 MST
func (p *PgAuditParser) parseTimestamp(timeStr string) (time.Time, error) {
	// 尝试不同的时间格式
	layouts := []string{
		"2006-01-02 15:04:05.000 MST",    // %m with timezone
		"2006-01-02 15:04:05.000",        // %m without timezone
		"2006-01-02 15:04:05 MST",        // %t with timezone
		"2006-01-02 15:04:05",            // %t without timezone
		"2006-01-02 15:04:05.999999 MST", // %m with microseconds and timezone
		"2006-01-02 15:04:05.999999",     // %m with microseconds
	}

	for _, layout := range layouts {
		if t, err := time.Parse(layout, timeStr); err == nil {
			return t, nil
		}
	}

	return time.Time{}, fmt.Errorf("cannot parse timestamp: %s", timeStr)
}

func (p *PgAuditParser) hashSQL(sql string) string {
	h := fnv.New64a()
	h.Write([]byte(sql))
	return strconv.FormatUint(h.Sum64(), 10)
}

// parseAuditContent 解析 AUDIT 内容
func (p *PgAuditParser) parseAuditContent(content string) (*model.TrafficBaseline, error) {
	unit := &model.TrafficBaseline{}

	parts := p.splitAuditContent(content)
	if len(parts) < 8 {
		return nil, fmt.Errorf("invalid audit content format, got %d parts", len(parts))
	}

	unit.SQLType = parts[3]
	unit.Operation = parts[4]

	sqlStmt := parts[7]
	sqlStmt = strings.Trim(sqlStmt, "\"")

	var params string
	if len(parts) > 8 {
		params = parts[8]
		params = strings.Trim(params, "\"")
	}

	if params != "" && params != "<not logged>" && params != "<none>" {
		sqlStmt = p.fillParameters(sqlStmt, params)
	}

	unit.SQLText = sqlStmt

	return unit, nil
}

func (p *PgAuditParser) splitAuditContent(content string) []string {
	var parts []string
	var current strings.Builder
	inQuote := false
	quoteChar := rune(0)

	for i, ch := range content {
		switch ch {
		case '"', '\'':
			if !inQuote {
				inQuote = true
				quoteChar = ch
			} else if ch == quoteChar {
				if i+1 < len(content) && rune(content[i+1]) == ch {
					current.WriteRune(ch)
					continue
				}
				inQuote = false
			}
			current.WriteRune(ch)
		case ',':
			if inQuote {
				current.WriteRune(ch)
			} else {
				parts = append(parts, current.String())
				current.Reset()
			}
		default:
			current.WriteRune(ch)
		}
	}
	if current.Len() > 0 {
		parts = append(parts, current.String())
	}
	return parts
}

// fillParameters 将参数填充到 SQL 语句中 (Reverse Order Algorithm)
func (p *PgAuditParser) fillParameters(sql string, params string) string {
	params = strings.Trim(params, "\"")
	paramList := p.parseParams(params)

	// Reverse Order Replacement: 从 $N 替换到 $1，防止 $1 匹配到 $10 的前缀
	result := sql
	for i := len(paramList) - 1; i >= 0; i-- {
		placeholder := fmt.Sprintf("$%d", i+1)
		quotedParam := p.quoteParam(paramList[i])
		result = strings.Replace(result, placeholder, quotedParam, -1) // Replace all occurences
	}

	return result
}

func (p *PgAuditParser) parseParams(params string) []string {
	var result []string
	var current strings.Builder
	depth := 0

	for _, ch := range params {
		switch ch {
		case '(':
			depth++
			current.WriteRune(ch)
		case ')':
			depth--
			current.WriteRune(ch)
		case ',':
			if depth == 0 {
				result = append(result, current.String())
				current.Reset()
			} else {
				current.WriteRune(ch)
			}
		default:
			current.WriteRune(ch)
		}
	}
	if current.Len() > 0 {
		result = append(result, current.String())
	}
	return result
}

func (p *PgAuditParser) quoteParam(param string) string {
	param = strings.TrimSpace(param)
	if strings.ToUpper(param) == "NULL" || param == "<null>" {
		return "NULL"
	}
	if _, err := strconv.ParseInt(param, 10, 64); err == nil {
		return param
	}
	if _, err := strconv.ParseFloat(param, 64); err == nil {
		return param
	}
	lower := strings.ToLower(param)
	if lower == "true" || lower == "false" {
		return lower
	}
	escaped := strings.ReplaceAll(param, "'", "''")
	return fmt.Sprintf("'%s'", escaped)
}
