"""Database advisory-lock identifiers shared by background indexes."""

from __future__ import annotations

# A stable signed 64-bit PostgreSQL advisory-lock key.  Keeping corpus
# mutation and embedding insertion mutually exclusive prevents a long-running
# embedding batch from racing the RAG indexer's chunk replacement/pruning.
RAG_CORPUS_ADVISORY_LOCK = 704136221389174203
