/* Compile against the target server's own headers. Never guess ABI offsets. */
#include "postgres.h"
#include "executor/execdesc.h"
#include <stddef.h>
#include <stdio.h>
#undef printf

int main(void) {
    printf("{\"pg_version_num\":%d,\"pointer_bytes\":%zu,"
           "\"qd_plan\":%zu,\"qd_source\":%zu,\"ps_queryid\":%zu}\n",
           PG_VERSION_NUM, sizeof(void *), offsetof(QueryDesc, plannedstmt),
           offsetof(QueryDesc, sourceText), offsetof(PlannedStmt, queryId));
    return 0;
}
