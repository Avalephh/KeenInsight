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

// PgAuditParser pgaudit 日志解析器
type PgAuditParser struct {
}

// Ensure PgAuditParser implements ReplayBase
var _ ReplayBase = (*PgAuditParser)(nil)

// NewPgAuditParser 创建解析器
func NewPgAuditParser() *PgAuditParser {
	return &PgAuditParser{}
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
				StartTime: unit.OriginTime,
				EndTime:   unit.OriginTime,
			}
			result.Transactions[unit.TxID] = tx
		}

		if unit.OriginTime.After(tx.EndTime) {
			tx.EndTime = unit.OriginTime
		}
		if unit.OriginTime.Before(tx.StartTime) {
			tx.StartTime = unit.OriginTime
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
				StartTime: unit.OriginTime,
				EndTime:   unit.OriginTime,
			}
			result.Transactions[unit.TxID] = tx
		}

		if unit.OriginTime.After(tx.EndTime) {
			tx.EndTime = unit.OriginTime
		}
		if unit.OriginTime.Before(tx.StartTime) {
			tx.StartTime = unit.OriginTime
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
func (p *PgAuditParser) parseLine(line string, taskID string) (*model.TrafficBaseline, error) {
	if strings.TrimSpace(line) == "" {
		return nil, nil
	}

	// New format: user,db,time_epoch,state,session,vxid,qid LOG:  AUDIT: ...
	// Find the split point between prefix and audit log
	// The space before LOG is part of the prefix format we defined: "...%Q "
	prefixEnd := strings.Index(line, " LOG:  AUDIT:")
	if prefixEnd == -1 {
		return nil, nil
	}

	prefixStr := line[:prefixEnd]
	auditContent := line[prefixEnd+len(" LOG:  AUDIT:"):]
	auditContent = strings.TrimSpace(auditContent)

	fields := strings.Split(prefixStr, ",")
	if len(fields) < 7 {
		// Not matching expected CSV format
		return nil, nil
	}

	username := fields[0]
	database := fields[1]
	timeEpochStr := fields[2]
	// state := fields[3]
	sessionID := fields[4]
	vxid := fields[5]
	qidStr := fields[6]

	// Remove trailing space from qidStr if present (due to the space at the end of prefix in config)
	qidStr = strings.TrimSpace(qidStr)

	originTime, err := p.parseEpoch(timeEpochStr)
	if err != nil {
		return nil, fmt.Errorf("failed to parse time '%s': %w", timeEpochStr, err)
	}

	unit, err := p.parseAuditContent(auditContent)
	if err != nil {
		return nil, fmt.Errorf("failed to parse audit content: %w", err)
	}

	unit.TaskID = taskID
	unit.UserName = username
	unit.DBName = database
	unit.OriginTime = originTime
	unit.ExecTimestamp = originTime.UnixMilli()
	unit.SessionID = sessionID
	unit.TxID = vxid // Map PG vxid to system TxID

	// QID handling
	if qidStr != "0" && qidStr != "" {
		unit.SQLID = qidStr
	} else {
		// Fallback: Hash of SQL Text
		unit.SQLID = p.hashSQL(unit.SQLText)
	}

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
