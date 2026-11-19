FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml requirements.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends clang llvm libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY demo ./demo
COPY rag_sources ./rag_sources
COPY artifacts ./artifacts
COPY third_party ./third_party

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
