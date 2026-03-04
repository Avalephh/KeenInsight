# RUC DB Replay

## Project Overview

`ruc-db-replay` is a backend service designed for database traffic replay. It specializes in parsing `pgaudit` logs from a source database and replaying the SQL statements against a target PostgreSQL database. The system is built to maintain transactional consistency, respect original execution timing (with optional speed adjustments), and detect divergences in execution results.

## Technology Stack

*   **Language:** Go (v1.25.5)
*   **Web Framework:** [Gin](https://github.com/gin-gonic/gin)
*   **ORM:** [GORM](https://gorm.io/)
*   **Configuration:** [Viper](https://github.com/spf13/viper)
*   **Logging:** [Zap](https://github.com/uber-go/zap)
*   **Documentation:** [Swagger](https://github.com/swaggo/swag)
*   **Database Drivers:** PostgreSQL (`lib/pq`, `pgx`), MySQL, SQLite.

## Architecture & Directory Structure

*   **`cmd/app/`**: Contains `main.go`, the entry point for the application.
*   **`configs/`**: Configuration files (e.g., `config.yaml`).
*   **`internal/`**: Core application logic.
    *   **`config/`**: Configuration loading and management.
    *   **`handler/`**: HTTP request handlers (Controllers).
    *   **`model/`**: Data structures for SQL statements, transactions, and reports.
    *   **`parser/`**: Logic for parsing `pgaudit` CSV logs.
        *   `pgaudit_parser.go`: Implements the parser, handling `VxID`, `TxID`, and `CSV` log formats.
    *   **`replay/`**: Core replay engine.
        *   `replayer.go`: Manages replay sessions, concurrency, speed control, and divergence detection.
    *   **`repository/`**: Data access layer.
    *   **`server/`**: HTTP server setup and router configuration.
    *   **`service/`**: Business logic layer bridging handlers and core components.
*   **`pkg/`**: Shared packages.
    *   `database`: Database connection initialization.
    *   `logger`: Logging configuration.
    *   `response`: Standardized API response helpers.
*   **`tools/`**: Auxiliary scripts.
    *   `monitor_and_run.py`: Python script for monitoring and running tests.
    *   `sysbench_lua/`: Sysbench scripts for generating test traffic.

## Key Components

### 1. PgAudit Parser (`internal/parser`)
The parser is responsible for converting raw `pgaudit` log lines into structured `SQLStatement` objects.
*   **Features:**
    *   Supports multiple log line formats (xid/vxid ordering).
    *   Handles CSV parsing for the `AUDIT` message content.
    *   Reconstructs SQL statements by filling in parameters (replacing `$1`, `$2` placeholders).
    *   Groups statements by Virtual Transaction ID (`VxID`).

### 2. Replayer (`internal/replay`)
The engine that executes the parsed statements against the target database.
*   **Concurrency:** Creates a worker goroutine for each unique `SessionID`.
*   **Transaction Management:** Respects `VxID` boundaries. Automatically manages transactions (`BEGIN`, `COMMIT`, `ROLLBACK`) if explicit control statements are missing or to maintain consistency.
*   **Speed Control:** Supports `SpeedFactor` (e.g., 2.0x speed) and `FastMode` (ignore timestamps).
*   **Divergence Detection:** Compares execution results (rows affected, error codes) between the original log and the replay.

## Building and Running

### Prerequisites
*   Go 1.18+
*   PostgreSQL (target database)

### Configuration
Edit `configs/config.yaml` to configure the server port, log levels, and default database connections.

### Commands
*   **Run:** `go run cmd/app/main.go`
*   **Build:** `go build -o bin/server cmd/app/main.go`

## API Usage

The service exposes a RESTful API for managing replay tasks.

*   **POST** `/api/v1/replay/prepare`: Upload a `pgaudit` log file and target DB credentials. Returns a `task_id`.
*   **POST** `/api/v1/replay/run`: Start a replay task. Accepts `task_id`, `speed_factor`, `max_workers`, etc.
*   **GET** `/api/v1/replay/progress`: Check the real-time progress of a running task.
*   **GET** `/api/v1/replay/report`: Get the final replay report, including divergence statistics.
*   **POST** `/api/v1/replay/stop`: Stop a running task.

## Development Conventions

*   **Error Handling:** Use `pkg/response` for consistent JSON error responses.
*   **Logging:** Use `pkg/logger` (Zap) for structured logging.
*   **Context:** Pass `context.Context` through layers for cancellation and timeouts.
*   **Database:** Use GORM for internal metadata storage (if applicable) and `database/sql` for the actual replay execution to ensure raw SQL compatibility.

## 语言规则

*   **所有面向用户的报告、Walkthrough、实现计划等文档必须使用中文撰写。**
