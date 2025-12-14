# RUC DB Replay Backend Service

## Project Structure

- `cmd/app`: Main entry point.
- `configs`: Configuration files.
- `internal`: Private application code.
  - `config`: Configuration loading logic.
  - `server`: HTTP server and router setup.
  - `handler`: API handlers and DTOs.
- `pkg`: Public library code.
  - `logger`: Zap logger wrapper.
  - `database`: GORM database initialization.
  - `response`: Standard API response format.
- `docs`: Swagger documentation.

## Getting Started

### Prerequisites

- Go 1.18+
- MySQL (Optional for startup, required for DB features)

### Running the application

1. Edit `configs/config.yaml` as needed.
2. Run the server:

```bash
go run cmd/app/main.go
```

### Build

```bash
go build -o bin/server cmd/app/main.go
```

### API Documentation (Swagger)

After running the server, visit:

[http://localhost:8080/swagger/index.html](http://localhost:8080/swagger/index.html)

### API Endpoints

- POST `/api/v1/replay/prepare`: Upload log file and DB config.
- POST `/api/v1/replay/run`: Start replay task.
- GET `/api/v1/replay/report`: Get replay report.
- GET `/health`: Health check.
