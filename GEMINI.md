# RUC DB Replay

## 项目概览 (Project Overview)

`ruc-db-replay` 是一个专为数据库流量回放设计的后端服务。它专门用于解析源数据库的 `pgaudit` 审计日志，并在目标 PostgreSQL 数据库中回放 SQL 语句流。该系统旨在模拟复杂生产业务环境，维持事务的一致性，精确还原原始执行时序（并支持可选的按倍速调整），同时在此基础上实现高保真度回放，通过各项实验指标（如 QPS/TPS 吞吐量保真度、P99 时延分布等）有效评估数据库运维变更产生的影响。

## 技术栈 (Technology Stack)

*   **编程语言:** Go (v1.25.5)
*   **Web 框架:** [Gin](https://github.com/gin-gonic/gin)
*   **ORM:** [GORM](https://gorm.io/)
*   **配置管理:** [Viper](https://github.com/spf13/viper)
*   **日志记录:** [Zap](https://github.com/uber-go/zap)
*   **API 文档:** [Swagger](https://github.com/swaggo/swag)
*   **数据库驱动:** PostgreSQL (`lib/pq`, `pgx`), MySQL, SQLite.
*   **实验生态:** Python 环境用于基于 `TPC-C (benchmarksql)` 等负载的编排与结果作图分析。

## 架构与目录结构 (Architecture & Directory Structure)

*   **`cmd/app/`**: 包含 `main.go`，应用程序的入口点。
*   **`configs/`**: 配置文件目录（如 `config.yaml` 及其不同的压力配置变体）。
*   **`internal/`**: 核心应用逻辑层。
    *   **`config/`**: 配置项的加载与管理。
    *   **`handler/`**: HTTP 请求控制器 (Controllers)。
    *   **`model/`**: 定义 SQL 语句、事务、统计报告等核心数据结构。
    *   **`parser/`**: 解析 `pgaudit` CSV 日志的核心逻辑。
        *   `pgaudit_parser.go`: 基于哈希映射的单遍重组算法，进行异构审计日志解析与流式事务重组。
    *   **`replay/`**: 核心回放并发引擎。
        *   `replayer.go`: 负责管理回放会话、控制协程并发、处理时序调度和运行结果的差异检测。
*   **`pkg/`**: 共享的基础代码包。
    *   `database`: 数据库连接配置与初始化。
    *   `logger`: 系统日志配置。
    *   `response`: 标准化 API 响应的辅助方法。
*   **`thesis/`**: 包含毕业设计论文（包含基于实验生成的图表、实验设计理论等）的 LaTeX 源码及相关文字材料。
*   **`tools/`**: 辅助工具与测试脚本大全。
    *   内置 `benchmarksql` 等用于生成 TPC-C 测试流量。
    *   包含多个 Python 脚本（如 `thesis_experiment.py`, `run_dep_checkpoint_experiment.py`, `comprehensive_replay_test.py` 等），用以执行实验、提取 P99 CDF 及各类评估指标用于论文作图验证。

## 核心组件与特性 (Key Components & Features)

### 1. 异构日志解析器 (PgAudit Parser - `internal/parser`)
负责将海量乱序的原始 `pgaudit` 日志转换为被调度的 `SQLStatement` 序列对象。
*   **单遍重组 ($O(N)$):** 支持多格式（基于 `VxID`, `TxID` 或 CSV 格式），基于哈希映射精确重构会话分流与事务时序上下文，解决传统系统难以划定精准事务边界的痛点。
*   **参数化映射:** 自动识别并填充 `$1`, `$2` 等预编译参数占位符。

### 2. 高并发回放控制引擎 (Replayer - `internal/replay`)
在目标数据库中高效且精准地按原始时序重现被解析出的 SQL 语句流。
*   **并发模型 (\textbf{goroutine}):** 采用“每会话一协程 (per-session, per-goroutine)”的设计，大幅提升高并发环境下的业务保真模拟。
*   **时序与对齐控制:** 利用基于滑动时间窗口的时序对齐算法，提供绝对意义上的时间保真，亦支持 `SpeedFactor` 调速回放与 `FastMode`（剔除时间戳模拟极限压测）。
*   **增强型回放保护机制:**
    *   **依赖图发掘与并发控制:** 针对跨会话可能的行锁与表级读写冲突，梳理请求的操作依赖图（Dependency Graph）防止不一致报错或死锁重现失效。
    *   **检查点控制 (Checkpoint):** 提供定周期的恢复和检查点协调保障机制。
*   **多维度保真度检测体系:** 对比源库与目标库的运行执行情况，支持功能一致性对比、异常/错误代码精准比对提取，及定量的归一化均方根误差 (NRMSE) 与 K-S 检验等统计分析。

## 编译与运行 (Building and Running)

### 环境依赖
*   Go 1.18+
*   PostgreSQL (充当目标库)
*   Python 3.x (执行实验脚本和产生质量图表需要)

### 常用命令
*   **启动服务:** `go run cmd/app/main.go`
*   **构建执行文件:** `go build -o bin/server cmd/app/main.go`

## API 使用 (API Usage)

本服务提供了一套 RESTful API 侧供调度管理：
*   **POST** `/api/v1/replay/prepare`: 上传 `pgaudit` 日志或测试用例及数据库凭证，系统将预处理并返回 `task_id`。
*   **POST** `/api/v1/replay/run`: 启动并运行回放（可携带参数如 `task_id`, `speed_factor`, `max_workers`）。
*   **GET** `/api/v1/replay/progress`: 异步查询正在执行的回放任务实时统计与进度。
*   **GET** `/api/v1/replay/report`: 输出全面回放报告、各类统计 Divergence 数据。
*   **POST** `/api/v1/replay/stop`: 主动阻断正在进行中的回放任务。

## 开发与代码规范 (Development Conventions)

*   **错误处理:** 后端需使用 `pkg/response` 输出一致且标准的 JSON 接口格式。
*   **日志采集:** 贯彻使用 `pkg/logger` (Zap) 进行结构化信息跟踪。
*   **进程上下文隔离:** 请求和回放调用链须串联 `context.Context` 以保障优雅挂起、中断退出或设定执行超时约束。
*   **库类选用:** 数据层的自身元数据通过 GORM 管理，而涉及到 SQL 回放解析重试部分，必须原生下沉致 `database/sql` 保证语法最大限度的向下兼容还原。

## 语言规则 (Language Rules)

*   **所有面向用户的报告、Walkthrough、实现计划等文档必须使用中文撰写。**
