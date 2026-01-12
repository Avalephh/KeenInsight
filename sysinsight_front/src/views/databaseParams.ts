// databaseParams.ts - 数据库参数配置文件

export interface DatabaseParam {
    value: string;          // 参数ID
    label: string;          // 显示名称
    desc: string;           // 描述
    category: string;       // 分类（性能、内存、网络等）
    defaultValue: string;   // 默认值
    currentValue?: string;  // 当前值（从数据库获取）
    suggestedRange?: {      // 建议范围
        min: string;
        max: string;
        unit: string;         // 单位：B, KB, MB, GB, %, 等
    };
}

// 常用MySQL数据库参数
export const mockDatabaseParams: DatabaseParam[] = [
    // 性能参数
    {
        value: 'innodb_buffer_pool_size',
        label: 'InnoDB缓冲池大小',
        desc: 'InnoDB存储引擎用于缓存数据和索引的内存大小',
        category: '性能',
        defaultValue: '128M',
        currentValue: '128M',
        suggestedRange: { min: '128M', max: '8G', unit: 'B' }
    },
    {
        value: 'innodb_log_file_size',
        label: 'InnoDB日志文件大小',
        desc: '每个InnoDB日志文件的大小',
        category: '性能',
        defaultValue: '48M',
        currentValue: '48M',
        suggestedRange: { min: '4M', max: '2G', unit: 'B' }
    },
    {
        value: 'max_connections',
        label: '最大连接数',
        desc: 'MySQL服务器允许的最大并发连接数',
        category: '连接',
        defaultValue: '151',
        currentValue: '151',
        suggestedRange: { min: '100', max: '10000', unit: '' }
    },
    {
        value: 'key_buffer_size',
        label: 'MyISAM键缓冲大小',
        desc: 'MyISAM表索引的缓冲区大小',
        category: '内存',
        defaultValue: '8M',
        currentValue: '8M',
        suggestedRange: { min: '8M', max: '4G', unit: 'B' }
    },
    {
        value: 'tmp_table_size',
        label: '临时表大小',
        desc: '内部内存临时表的最大大小',
        category: '性能',
        defaultValue: '16M',
        currentValue: '16M',
        suggestedRange: { min: '1M', max: '1G', unit: 'B' }
    },
    {
        value: 'max_heap_table_size',
        label: '内存表最大大小',
        desc: '用户创建的MEMORY表允许的最大大小',
        category: '内存',
        defaultValue: '16M',
        currentValue: '16M',
        suggestedRange: { min: '16M', max: '1G', unit: 'B' }
    },
    {
        value: 'innodb_flush_log_at_trx_commit',
        label: '事务提交刷盘策略',
        desc: '控制事务提交时日志刷盘的频率',
        category: '持久化',
        defaultValue: '1',
        currentValue: '1',
        suggestedRange: { min: '0', max: '2', unit: '' }
    },
    {
        value: 'sync_binlog',
        label: '二进制日志同步',
        desc: '控制二进制日志同步到磁盘的频率',
        category: '复制',
        defaultValue: '1',
        currentValue: '1',
        suggestedRange: { min: '0', max: '1000', unit: '' }
    },
    {
        value: 'thread_cache_size',
        label: '线程缓存大小',
        desc: '缓存线程的数量，减少线程创建的开销',
        category: '连接',
        defaultValue: '8',
        currentValue: '8',
        suggestedRange: { min: '0', max: '1000', unit: '' }
    },
    {
        value: 'table_open_cache',
        label: '表缓存大小',
        desc: '所有线程打开表的数量',
        category: '缓存',
        defaultValue: '2000',
        currentValue: '2000',
        suggestedRange: { min: '400', max: '524288', unit: '' }
    },
    {
        value: 'innodb_io_capacity',
        label: 'InnoDB IO容量',
        desc: 'InnoDB后台任务使用的IOPS',
        category: 'IO',
        defaultValue: '200',
        currentValue: '200',
        suggestedRange: { min: '100', max: '20000', unit: '' }
    },
    {
        value: 'innodb_read_io_threads',
        label: 'InnoDB读IO线程数',
        desc: 'InnoDB读操作的IO线程数量',
        category: 'IO',
        defaultValue: '4',
        currentValue: '4',
        suggestedRange: { min: '1', max: '64', unit: '' }
    },
    {
        value: 'innodb_write_io_threads',
        label: 'InnoDB写IO线程数',
        desc: 'InnoDB写操作的IO线程数量',
        category: 'IO',
        defaultValue: '4',
        currentValue: '4',
        suggestedRange: { min: '1', max: '64', unit: '' }
    },
    {
        value: 'innodb_log_buffer_size',
        label: 'InnoDB日志缓冲大小',
        desc: 'InnoDB重做日志缓冲区的大小',
        category: '日志',
        defaultValue: '16M',
        currentValue: '16M',
        suggestedRange: { min: '1M', max: '1G', unit: 'B' }
    },
    {
        value: 'innodb_buffer_pool_instances',
        label: '缓冲池实例数',
        desc: 'InnoDB缓冲池的分区数量',
        category: '性能',
        defaultValue: '8',
        currentValue: '8',
        suggestedRange: { min: '1', max: '64', unit: '' }
    },
    {
        value: 'sort_buffer_size',
        label: '排序缓冲大小',
        desc: '每个线程排序时分配的缓冲区大小',
        category: '内存',
        defaultValue: '256K',
        currentValue: '256K',
        suggestedRange: { min: '32K', max: '8M', unit: 'B' }
    },
    {
        value: 'join_buffer_size',
        label: '连接缓冲大小',
        desc: '没有使用索引的连接操作的缓冲区大小',
        category: '内存',
        defaultValue: '256K',
        currentValue: '256K',
        suggestedRange: { min: '128K', max: '8M', unit: 'B' }
    },
    {
        value: 'read_buffer_size',
        label: '读缓冲大小',
        desc: '每个线程连续扫描时为表分配的缓冲区大小',
        category: '内存',
        defaultValue: '128K',
        currentValue: '128K',
        suggestedRange: { min: '128K', max: '8M', unit: 'B' }
    },
    {
        value: 'read_rnd_buffer_size',
        label: '随机读缓冲大小',
        desc: '排序后读取行时分配的缓冲区大小',
        category: '内存',
        defaultValue: '256K',
        currentValue: '256K',
        suggestedRange: { min: '256K', max: '8M', unit: 'B' }
    },
    {
        value: 'binlog_cache_size',
        label: '二进制日志缓存大小',
        desc: '事务期间用于存储二进制日志更改的缓存大小',
        category: '日志',
        defaultValue: '32K',
        currentValue: '32K',
        suggestedRange: { min: '4096', max: '1G', unit: 'B' }
    },
    {
        value: 'max_allowed_packet',
        label: '最大允许数据包大小',
        desc: '服务器和客户端之间传输的最大数据包大小',
        category: '网络',
        defaultValue: '64M',
        currentValue: '64M',
        suggestedRange: { min: '1024', max: '1G', unit: 'B' }
    },
    {
        value: 'innodb_flush_method',
        label: 'InnoDB刷盘方法',
        desc: '控制InnoDB数据文件和日志文件的刷盘方式',
        category: 'IO',
        defaultValue: 'fsync',
        currentValue: 'fsync',
        suggestedRange: { min: 'fsync', max: 'O_DIRECT', unit: '' }
    },
    {
        value: 'innodb_file_per_table',
        label: '独立表空间',
        desc: '是否为每个InnoDB表创建独立的表空间文件',
        category: '存储',
        defaultValue: 'ON',
        currentValue: 'ON',
        suggestedRange: { min: 'ON', max: 'OFF', unit: '' }
    },
    {
        value: 'innodb_stats_on_metadata',
        label: '元数据统计',
        desc: '访问元数据时是否更新统计信息',
        category: '统计',
        defaultValue: 'OFF',
        currentValue: 'OFF',
        suggestedRange: { min: 'ON', max: 'OFF', unit: '' }
    },
    {
        value: 'innodb_adaptive_hash_index',
        label: '自适应哈希索引',
        desc: '是否启用InnoDB自适应哈希索引',
        category: '性能',
        defaultValue: 'ON',
        currentValue: 'ON',
        suggestedRange: { min: 'ON', max: 'OFF', unit: '' }
    },
    {
        value: 'innodb_doublewrite',
        label: '双写缓冲',
        desc: '是否启用InnoDB双写缓冲，防止数据损坏',
        category: '持久化',
        defaultValue: 'ON',
        currentValue: 'ON',
        suggestedRange: { min: 'ON', max: 'OFF', unit: '' }
    },
    {
        value: 'query_cache_type',
        label: '查询缓存类型',
        desc: '查询缓存的类型（MySQL 8.0已移除）',
        category: '缓存',
        defaultValue: 'OFF',
        currentValue: 'OFF',
        suggestedRange: { min: '0', max: '2', unit: '' }
    },
    {
        value: 'slow_query_log',
        label: '慢查询日志',
        desc: '是否启用慢查询日志',
        category: '日志',
        defaultValue: 'OFF',
        currentValue: 'OFF',
        suggestedRange: { min: 'ON', max: 'OFF', unit: '' }
    },
    {
        value: 'long_query_time',
        label: '慢查询阈值',
        desc: '定义慢查询的时间阈值（秒）',
        category: '日志',
        defaultValue: '10',
        currentValue: '10',
        suggestedRange: { min: '0', max: '10', unit: '' }
    },
    {
        value: 'log_queries_not_using_indexes',
        label: '记录未使用索引查询',
        desc: '是否记录未使用索引的查询',
        category: '日志',
        defaultValue: 'OFF',
        currentValue: 'OFF',
        suggestedRange: { min: 'ON', max: 'OFF', unit: '' }
    }
];

// 获取数据库参数的函数（在实际应用中，这里会调用API获取实时参数）
export const fetchDatabaseParams = async (): Promise<DatabaseParam[]> => {
    // 实际应用中，这里会调用后端API获取数据库当前参数
    return mockDatabaseParams;
};

// 更新参数当前值的函数
export const updateCurrentValues = async (params: DatabaseParam[]): Promise<DatabaseParam[]> => {
    // 实际应用中，这里会调用后端API获取数据库当前参数值
    // 这里模拟更新
    return params.map(param => ({
        ...param,
        currentValue: param.currentValue || param.defaultValue
    }));
};