package parser

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"regexp"
	"ruc-db-replay/internal/model"
	"ruc-db-replay/pkg/logger"
	"strconv"
	"strings"
	"time"

	"go.uber.org/zap"
)

// PgAuditParser pgaudit 日志解析器
type PgAuditParser struct {
	// 日志行正则表达式（支持两种 vxid 格式）
	// 格式1: user=%u db=%d time=%m state=%e session=%c xid=%x vxid=%v LOG:  AUDIT: ... (xid 在 vxid 前)
	// 格式2: user=%u db=%d time=%m state=%e session=%c vxid=%v tx=%x LOG:  AUDIT: ... (vxid 在 tx 前)
	lineRegexXidVxid *regexp.Regexp // xid=... vxid=... 格式
	lineRegexVxidTx  *regexp.Regexp // vxid=... tx=... 格式
	// AUDIT 内容正则表达式
	auditRegex *regexp.Regexp
}

// NewPgAuditParser 创建解析器
func NewPgAuditParser() *PgAuditParser {
	return &PgAuditParser{
		// 匹配 xid=... vxid=... 格式
		// user=test db=test time=2025-12-13 19:46:16.161 CST state=00000 session=693d5208.8382 xid=0 vxid=4/690 LOG:  AUDIT:
		lineRegexXidVxid: regexp.MustCompile(`user=(\S+)\s+db=(\S+)\s+time=([^\s]+\s+[^\s]+\s+\S+)\s+state=(\S+)\s+session=(\S+)\s+xid=(\d+)\s+vxid=(\S+)\s+LOG:\s+AUDIT:\s+(.+)`),
		// 匹配 vxid=... tx=... 格式
		// user=test db=test time=2025-12-09 22:03:41.664 CST state=00000 session=69382c1f.bd84 vxid=4/12532 tx=15723349 LOG:  AUDIT:
		lineRegexVxidTx: regexp.MustCompile(`user=(\S+)\s+db=(\S+)\s+time=([^\s]+\s+[^\s]+\s+\S+)\s+state=(\S+)\s+session=(\S+)\s+vxid=(\S+)\s+tx=(\d+)\s+LOG:\s+AUDIT:\s+(.+)`),
		// 匹配 AUDIT 内容
		// SESSION,28717,1,WRITE,INSERT,,,"INSERT INTO sbtest7 ...",<params>,1
		auditRegex: regexp.MustCompile(`^(\w+),(\d+),(\d+),(\w+),(\w+),([^,]*),([^,]*),(.+)$`),
	}
}

// ParseResult 解析结果
type ParseResult struct {
	Statements   []*model.SQLStatement
	Transactions map[string]*model.Transaction // 使用 VxID (string) 作为 key
	TotalLines   int64
	ParsedLines  int64
	ErrorLines   int64
}

// ParseStreamCallback 流式解析的回调函数类型
// 返回 error 会中断解析
type ParseStreamCallback func(statements []*model.SQLStatement, transactions map[string]*model.Transaction) error

// ParseFile 解析日志文件
func (p *PgAuditParser) ParseFile(filePath string, taskID string) (*ParseResult, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return nil, fmt.Errorf("failed to open file: %w", err)
	}
	defer file.Close()

	return p.Parse(file, taskID)
}

// ParseStream 流式解析日志文件
// batchSize: 每批次处理的语句数量
func (p *PgAuditParser) ParseStream(filePath string, taskID string, batchSize int, callback ParseStreamCallback) (*ParseResult, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return nil, fmt.Errorf("failed to open file: %w", err)
	}
	defer file.Close()

	result := &ParseResult{
		// 流式模式下，Statements 不再保存所有数据，只用于统计
		Statements:   make([]*model.SQLStatement, 0),
		Transactions: make(map[string]*model.Transaction),
	}

	scanner := bufio.NewScanner(file)
	// 增加缓冲区大小以处理超长行
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 10*1024*1024) // 最大 10MB 一行

	vxidSeqMap := make(map[string]int)

	// 当前批次的数据
	batchStmts := make([]*model.SQLStatement, 0, batchSize)
	batchTxs := make(map[string]*model.Transaction)

	for scanner.Scan() {
		result.TotalLines++
		line := scanner.Text()

		stmt, err := p.parseLine(line, taskID)
		if err != nil {
			result.ErrorLines++
			continue
		}

		if stmt == nil {
			continue
		}

		// 设置事务内序号
		vxidSeqMap[stmt.VxID]++
		stmt.SeqInTx = vxidSeqMap[stmt.VxID]

		result.ParsedLines++

		// 更新全局事务信息（用于最终统计和状态跟踪）
		// 注意：Transactions map 可能会很大，如果内存不够，这里也需要优化，
		// 但通常事务元数据比 SQL 文本小得多，先保留在内存中以便合并事务状态
		tx, exists := result.Transactions[stmt.VxID]
		if !exists {
			tx = &model.Transaction{
				TaskID:    taskID,
				TxID:      stmt.TxID,
				VxID:      stmt.VxID,
				SessionID: stmt.SessionID,
				StartTime: stmt.Timestamp,
				EndTime:   stmt.Timestamp,
				StmtCount: 0,
			}
			result.Transactions[stmt.VxID] = tx
		}

		tx.StmtCount++
		if stmt.Timestamp.After(tx.EndTime) {
			tx.EndTime = stmt.Timestamp
		}
		if stmt.Timestamp.Before(tx.StartTime) {
			tx.StartTime = stmt.Timestamp
		}
		if strings.ToUpper(stmt.Operation) == "COMMIT" {
			tx.Committed = true
		}

		// 添加到当前批次
		batchStmts = append(batchStmts, stmt)

		// 将当前事务状态快照放入批次（注意：这只是为了回调能拿到关联事务，
		// 实际上事务可能会跨多个批次，回调处理时需注意 update 而不是 create）
		batchTxs[stmt.VxID] = tx

		// 达到批次大小，触发回调
		if len(batchStmts) >= batchSize {
			if err := callback(batchStmts, batchTxs); err != nil {
				return result, fmt.Errorf("callback failed: %w", err)
			}
			// 清空批次
			batchStmts = make([]*model.SQLStatement, 0, batchSize)
			batchTxs = make(map[string]*model.Transaction)
		}
	}

	// 处理剩余数据
	if len(batchStmts) > 0 {
		if err := callback(batchStmts, batchTxs); err != nil {
			return result, fmt.Errorf("callback failed: %w", err)
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scanner error: %w", err)
	}

	return result, nil
}

// Parse 解析日志流
func (p *PgAuditParser) Parse(reader io.Reader, taskID string) (*ParseResult, error) {
	result := &ParseResult{
		Statements:   make([]*model.SQLStatement, 0),
		Transactions: make(map[string]*model.Transaction), // 使用 VxID 作为 key
	}

	scanner := bufio.NewScanner(reader)
	// 增加缓冲区大小以处理超长行
	buf := make([]byte, 0, 64*1024)
	scanner.Buffer(buf, 10*1024*1024) // 最大 10MB 一行

	vxidSeqMap := make(map[string]int) // 记录每个事务(vxid)的语句序号

	for scanner.Scan() {
		result.TotalLines++
		line := scanner.Text()

		stmt, err := p.parseLine(line, taskID)
		if err != nil {
			result.ErrorLines++
			if result.ErrorLines <= 10 && logger.Log != nil {
				logger.Log.Warn("Failed to parse line",
					zap.Int64("line_num", result.TotalLines),
					zap.Error(err))
			}
			continue
		}

		if stmt == nil {
			continue // 跳过非 AUDIT 行
		}

		// 设置事务内序号
		vxidSeqMap[stmt.VxID]++
		stmt.SeqInTx = vxidSeqMap[stmt.VxID]

		result.Statements = append(result.Statements, stmt)
		result.ParsedLines++

		// 更新事务信息（使用 VxID 分组）
		tx, exists := result.Transactions[stmt.VxID]
		if !exists {
			tx = &model.Transaction{
				TaskID:    taskID,
				TxID:      stmt.TxID,
				VxID:      stmt.VxID,
				SessionID: stmt.SessionID,
				StartTime: stmt.Timestamp,
				EndTime:   stmt.Timestamp,
				StmtCount: 0,
			}
			result.Transactions[stmt.VxID] = tx
		}

		tx.StmtCount++
		if stmt.Timestamp.After(tx.EndTime) {
			tx.EndTime = stmt.Timestamp
		}
		if stmt.Timestamp.Before(tx.StartTime) {
			tx.StartTime = stmt.Timestamp
		}

		// 检查是否是 COMMIT
		if strings.ToUpper(stmt.Operation) == "COMMIT" {
			tx.Committed = true
		}
	}

	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("scanner error: %w", err)
	}

	if logger.Log != nil {
		logger.Log.Info("Parse completed",
			zap.Int64("total_lines", result.TotalLines),
			zap.Int64("parsed_lines", result.ParsedLines),
			zap.Int64("error_lines", result.ErrorLines),
			zap.Int("transactions", len(result.Transactions)))
	}

	return result, nil
}

// parseLine 解析单行日志
func (p *PgAuditParser) parseLine(line string, taskID string) (*model.SQLStatement, error) {
	// 跳过空行
	if strings.TrimSpace(line) == "" {
		return nil, nil
	}

	var username, database, timeStr, state, sessionID, vxid, txIDStr, auditContent string

	// 尝试匹配 xid=... vxid=... 格式
	matches := p.lineRegexXidVxid.FindStringSubmatch(line)
	if matches != nil {
		username = matches[1]
		database = matches[2]
		timeStr = matches[3]
		state = matches[4]
		sessionID = matches[5]
		txIDStr = matches[6] // xid
		vxid = matches[7]
		auditContent = matches[8]
	} else {
		// 尝试匹配 vxid=... tx=... 格式
		matches = p.lineRegexVxidTx.FindStringSubmatch(line)
		if matches == nil {
			return nil, nil // 非 AUDIT 日志行，跳过
		}
		username = matches[1]
		database = matches[2]
		timeStr = matches[3]
		state = matches[4]
		sessionID = matches[5]
		vxid = matches[6]
		txIDStr = matches[7] // tx
		auditContent = matches[8]
	}

	// 解析时间戳
	timestamp, err := p.parseTimestamp(timeStr)
	if err != nil {
		return nil, fmt.Errorf("failed to parse timestamp '%s': %w", timeStr, err)
	}

	// 解析事务ID
	txID, err := strconv.ParseInt(txIDStr, 10, 64)
	if err != nil {
		return nil, fmt.Errorf("failed to parse tx_id '%s': %w", txIDStr, err)
	}

	// 解析 AUDIT 内容
	stmt, err := p.parseAuditContent(auditContent)
	if err != nil {
		return nil, fmt.Errorf("failed to parse audit content: %w", err)
	}

	stmt.TaskID = taskID
	stmt.Username = username
	stmt.Database = database
	stmt.Timestamp = timestamp
	stmt.State = state
	stmt.SessionID = sessionID
	stmt.TxID = txID
	stmt.VxID = vxid

	return stmt, nil
}

// parseTimestamp 解析时间戳
func (p *PgAuditParser) parseTimestamp(timeStr string) (time.Time, error) {
	// 格式: 2025-12-09 22:03:41.664 CST
	layouts := []string{
		"2006-01-02 15:04:05.000 MST",
		"2006-01-02 15:04:05.000000 MST",
		"2006-01-02 15:04:05 MST",
		"2006-01-02 15:04:05.000",
		"2006-01-02 15:04:05",
	}

	for _, layout := range layouts {
		if t, err := time.Parse(layout, timeStr); err == nil {
			return t, nil
		}
	}

	return time.Time{}, fmt.Errorf("unable to parse time: %s", timeStr)
}

// parseAuditContent 解析 AUDIT 内容
func (p *PgAuditParser) parseAuditContent(content string) (*model.SQLStatement, error) {
	// 格式: SESSION,28717,1,WRITE,INSERT,,,"INSERT INTO sbtest7 (id, k, c, pad) VALUES ($1, $2, $3, $4)","params",1
	// 或者: SESSION,28717,1,MISC,BEGIN,,,BEGIN,<not logged>,1

	stmt := &model.SQLStatement{}

	// 使用更智能的解析方式处理带引号的字段
	parts := p.splitAuditContent(content)
	if len(parts) < 8 {
		return nil, fmt.Errorf("invalid audit content format, got %d parts: %s", len(parts), content)
	}

	// parts[0]: AUDIT_TYPE (SESSION/OBJECT)
	// parts[1]: statement_id
	// parts[2]: substatement_id
	// parts[3]: class (READ/WRITE/DDL/MISC)
	// parts[4]: command (SELECT/INSERT/UPDATE/DELETE/BEGIN/COMMIT...)
	// parts[5]: object_type
	// parts[6]: object_name
	// parts[7]: statement (SQL)
	// parts[8]: parameter (可选)
	// parts[9]: rows_affected (可选)

	stmt.SQLType = parts[3]
	stmt.Operation = parts[4]

	// 获取 SQL 语句
	sqlStmt := parts[7]
	sqlStmt = strings.Trim(sqlStmt, "\"")

	// 获取参数（如果有）
	var params string
	if len(parts) > 8 {
		params = parts[8]
		params = strings.Trim(params, "\"")
	}

	// 获取影响行数
	if len(parts) > 9 {
		if rows, err := strconv.Atoi(parts[9]); err == nil {
			stmt.RowsAffected = rows
		}
	}

	// 填充参数到 SQL
	if params != "" && params != "<not logged>" && params != "<none>" {
		sqlStmt = p.fillParameters(sqlStmt, params)
	}

	stmt.SQL = sqlStmt

	return stmt, nil
}

// splitAuditContent 智能分割 AUDIT 内容（处理引号内的逗号）
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
				// 检查是否是转义的引号
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

	// 添加最后一个部分
	if current.Len() > 0 {
		parts = append(parts, current.String())
	}

	return parts
}

// fillParameters 将参数填充到 SQL 语句中
func (p *PgAuditParser) fillParameters(sql string, params string) string {
	// 参数格式: "value1,value2,value3" 或 value1,value2,value3
	params = strings.Trim(params, "\"")

	// 解析参数列表（考虑参数值中可能包含逗号）
	paramList := p.parseParams(params)

	// 替换 $1, $2, $3 ... 为实际值
	result := sql
	for i, param := range paramList {
		placeholder := fmt.Sprintf("$%d", i+1)
		// 对字符串参数添加引号
		quotedParam := p.quoteParam(param)
		result = strings.Replace(result, placeholder, quotedParam, 1)
	}

	return result
}

// parseParams 解析参数列表
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

// quoteParam 为参数添加适当的引号
func (p *PgAuditParser) quoteParam(param string) string {
	param = strings.TrimSpace(param)

	// NULL 值
	if strings.ToUpper(param) == "NULL" || param == "<null>" {
		return "NULL"
	}

	// 数字类型不需要引号
	if _, err := strconv.ParseInt(param, 10, 64); err == nil {
		return param
	}
	if _, err := strconv.ParseFloat(param, 64); err == nil {
		return param
	}

	// 布尔类型
	lower := strings.ToLower(param)
	if lower == "true" || lower == "false" {
		return lower
	}

	// 字符串类型：转义单引号并添加引号
	escaped := strings.ReplaceAll(param, "'", "''")
	return fmt.Sprintf("'%s'", escaped)
}
