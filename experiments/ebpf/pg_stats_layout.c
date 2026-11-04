/* Build against the SELECTED server headers, not a guessed UDP wire ABI. */
#include "postgres.h"
#include "pgstat.h"
#include <stddef.h>
#include <stdio.h>
#undef printf

int main(void)
{
    printf("{\"pg_version_num\":%d,\"pointer_bytes\":%zu,"
           "\"header_bytes\":%zu,\"tab_header_bytes\":%zu,"
           "\"database_offset\":%zu,\"entries_offset\":%zu,"
           "\"commits_offset\":%zu,\"rollbacks_offset\":%zu,"
           "\"read_us_offset\":%zu,\"write_us_offset\":%zu,"
           "\"entry_bytes\":%zu,\"entries\":%zu,"
           "\"returned_offset\":%zu,\"fetched_offset\":%zu,"
           "\"blocks_offset\":%zu,\"hit_offset\":%zu,"
           "\"temp_bytes\":%zu,\"temp_size_offset\":%zu,"
           "\"tab_type\":%d,\"temp_type\":%d,\"deadlock_type\":%d}\n",
           PG_VERSION_NUM, sizeof(void *), sizeof(PgStat_MsgHdr),
           offsetof(PgStat_MsgTabstat, m_entry),
           offsetof(PgStat_MsgTabstat, m_databaseid),
           offsetof(PgStat_MsgTabstat, m_nentries),
           offsetof(PgStat_MsgTabstat, m_xact_commit),
           offsetof(PgStat_MsgTabstat, m_xact_rollback),
           offsetof(PgStat_MsgTabstat, m_block_read_time),
           offsetof(PgStat_MsgTabstat, m_block_write_time),
           sizeof(PgStat_TableEntry),
           (size_t) PGSTAT_NUM_TABENTRIES,
           offsetof(PgStat_TableEntry, t_counts.t_tuples_returned),
           offsetof(PgStat_TableEntry, t_counts.t_tuples_fetched),
           offsetof(PgStat_TableEntry, t_counts.t_blocks_fetched),
           offsetof(PgStat_TableEntry, t_counts.t_blocks_hit),
           sizeof(PgStat_MsgTempFile),
           offsetof(PgStat_MsgTempFile, m_filesize),
           PGSTAT_MTYPE_TABSTAT, PGSTAT_MTYPE_TEMPFILE,
           PGSTAT_MTYPE_DEADLOCK);
    return 0;
}
