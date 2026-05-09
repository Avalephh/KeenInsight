#!/bin/bash
# pgbench runner for KeenInsight demo
# Usage: ./pgbench_runner.sh [normal|abnormal] [duration_seconds]

MODE=${1:-normal}
DURATION=${2:-30}
SCALE=10
HOST=${PGHOST:-localhost}
PORT=${PGPORT:-5432}
DBNAME=sbtest

case $MODE in
  normal)
    CLIENTS=5
    THREADS=2
    RATE="-T $DURATION"
    ;;
  abnormal)
    CLIENTS=50
    THREADS=8
    RATE="-T $DURATION"
    ;;
  stress)
    CLIENTS=100
    THREADS=16
    RATE="-T $DURATION"
    ;;
  *)
    CLIENTS=5
    THREADS=2
    RATE="-T $DURATION"
    ;;
esac

echo "Running pgbench in $MODE mode: clients=$CLIENTS threads=$THREADS duration=$DURATION"
su - postgres -c "pgbench -h $HOST -p $PORT -U postgres -d $DBNAME -c $CLIENTS -j $THREADS $RATE -r" 2>&1
