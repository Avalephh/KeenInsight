/*
 * Live adapter for the original English.zip pages.
 *
 * The HTML and page-specific styling in /static/legacy are intentionally
 * shipped unchanged.  This small adapter replaces the fixture data after the
 * original page has rendered, keeping the supplied visual design while making
 * every value come from the PostgreSQL evidence API.
 */
(function () {
    'use strict';

    const page = (window.location.pathname.split('/').pop() || 'template.html').toLowerCase();
    const state = {
        meta: null,
        dashboard: [],
        incidents: [],
        queries: [],
        rules: [],
        knowledge: [],
        experiences: [],
        sourceRows: [],
        logs: [],
        chart: { range: '24h', items: [] },
        flameGraph: null
    };

    const escapeHtml = (value) => String(value === null || value === undefined ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');

    const valueOrDash = (value, suffix) => {
        if (value === null || value === undefined || value === '') return '—';
        return `${value}${suffix || ''}`;
    };

    const numberOrNull = (value) => {
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    };

    const fixed = (value, digits = 2) => {
        const number = numberOrNull(value);
        return number === null ? '—' : number.toFixed(digits);
    };

    const percent = (value) => {
        const number = numberOrNull(value);
        return number === null ? null : Math.max(0, Math.min(100, number));
    };

    const formatDate = (value) => {
        if (!value) return '—';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value).replace('T', ' ').slice(0, 19);
        return date.toLocaleString([], { hour12: false }).replace(',', '');
    };

    const formatBytes = (value) => {
        const number = numberOrNull(value);
        if (number === null) return '—';
        if (number >= 1024 ** 3) return `${(number / 1024 ** 3).toFixed(1)} GB`;
        if (number >= 1024 ** 2) return `${(number / 1024 ** 2).toFixed(1)} MB`;
        if (number >= 1024) return `${(number / 1024).toFixed(1)} KB`;
        return `${Math.round(number)} B`;
    };

    const api = async (path, options) => {
        const response = await fetch(`/api${path}`, Object.assign({
            headers: { 'Content-Type': 'application/json' }
        }, options || {}));
        let payload = null;
        try {
            payload = await response.json();
        } catch (error) {
            payload = {};
        }
        if (!response.ok) {
            throw new Error(payload.detail || payload.error || `HTTP ${response.status}`);
        }
        return payload;
    };

    const optionalApi = async (path, fallback) => {
        try { return await api(path); } catch (error) { return fallback === undefined ? {} : fallback; }
    };

    const post = (path, body) => api(path, {
        method: 'POST',
        body: body === undefined ? undefined : JSON.stringify(body)
    });

    const notify = (message, kind) => {
        let toast = document.querySelector('.ki-live-toast');
        if (!toast) {
            toast = document.createElement('div');
            toast.className = 'ki-live-toast';
            document.body.appendChild(toast);
        }
        toast.textContent = message;
        toast.classList.toggle('error', kind === 'error');
        toast.classList.add('show');
        window.clearTimeout(toast._hideTimer);
        toast._hideTimer = window.setTimeout(() => toast.classList.remove('show'), 3200);
    };

    const showLoading = (element, message) => {
        if (!element) return;
        const content = `<div class="ki-live-loading">${escapeHtml(message || '正在读取 PostgreSQL 实时证据…')}</div>`;
        element.innerHTML = element.tagName === 'TBODY' ? `<tr><td colspan="11">${content}</td></tr>` : content;
    };

    const showError = (element, error) => {
        if (!element) return;
        element.classList.remove('ki-live-pending');
        const content = `<div class="ki-live-error">实时证据不可用：${escapeHtml(error.message || error)}</div>`;
        element.innerHTML = element.tagName === 'TBODY' ? `<tr><td colspan="11">${content}</td></tr>` : content;
    };

    const sourceTag = (text) => `<span class="ki-source-tag">${escapeHtml(text)}</span>`;

    // The supplied HTML is intentionally kept as an archival visual reference.
    // Translate its interface at runtime so the live page is readable in
    // Chinese without rewriting the original artifact or its embedded code.
    const legacyTextMap = {
        'KeenInsight - Host List': 'KeenInsight - 主机列表',
        'KeenInsight - Real-time Monitoring': 'KeenInsight - 实时监控',
        'KeenInsight - Container Services': 'KeenInsight - 容器服务',
        'KeenInsight - Alert List': 'KeenInsight - 异常列表',
        'KeenInsight - Alert Details': 'KeenInsight - 异常详情',
        'KeenInsight - Alert Rules': 'KeenInsight - 异常规则',
        'KeenInsight - SQL Logs': 'KeenInsight - SQL 日志',
        'KeenInsight - SQL Details': 'KeenInsight - SQL 详情',
        'KeenInsight - Diagnosis Rules': 'KeenInsight - 诊断规则',
        'KeenInsight - Code Source': 'KeenInsight - 源码分析',
        'KeenInsight - Manual': 'KeenInsight - 使用手册',
        'KeenInsight - Tuning-derived Knowledge': 'KeenInsight - 调优经验知识',
        'Host List': '实例列表',
        'SQL Detail': 'SQL 详情',
        'Back to List': '返回列表',
        'Related Knowledge Base': '相关知识库',
        'Maunal': '手册',
        'Upload': '导入',
        '+ New Rule': '+ 新建规则',
        'Security Audit': '安全审计',
        'Capacity Planning': '容量规划',
        'Cloud Servers': '云服务器',
        'Network': '网络',
        'Storage': '存储',
        'Cluster': '实例',
        'Rule Category': '规则类别',
        'Function Category': '函数类别',
        'Code Source - Database Function Flow and Data Flow Analysis': '源码分析 · 数据库函数流与数据流',
        'Container Services': '实例服务',
        'Root Cause Analysis': '根因分析',
        'Exception Diagnosis': '异常诊断',
        'Configuration Validation and Deployment': '配置校验与部署',
        'Function Name': '函数名称',
        'Function ID': '函数 ID',
        'Rule Name': '规则名称',
        'Description': '说明',
        'Alert Field': '告警字段',
        'Check Period': '检查周期',
        'Edit': '编辑',
        'Copy': '复制',
        'Delete': '删除',
        'Details': '详情',
        'Condition': '触发条件',
        'Cluster:': '聚类：',
        'records': '条记录',
        'Recommendation': '建议',
        'Action': '操作',
        'Confidence': '置信度',
        'avg gain': '平均收益',
        'No knowledge snippets yet. Valuable insights from your documents will appear here': '暂时没有知识片段；来自文档的有效洞察会显示在这里',
        'Delete functionality coming soon!': '删除功能即将提供',
        'Buffer Management': '缓冲区管理',
        'I/O Operations': 'I/O 操作',
        'Lock Management': '锁管理',
        'Query Processing': '查询处理',
        'Transaction': '事务',
        'High': '高',
        'Medium': '中',
        'Low': '低',
        'Evidence-driven operations': '证据驱动运维',
        'Database Real-time Monitoring': '数据库实时监控',
        'System Resources Overview': '系统资源概览',
        'Database Performance Metrics': '数据库性能指标',
        'Database sessions': '数据库会话',
        'Transactions / sec': '事务 / 秒',
        'Collector cycles': '采集器周期数',
        'CPU usage': 'CPU 使用率',
        'Values above are read from persisted snapshots and the current PostgreSQL connection.': '以上数值来自持久化快照和当前 PostgreSQL 连接。',
        'Detailed alerts, SQL evidence, source analysis, and the measured tuning loop are available below.': '下方提供异常、SQL 证据、源码分析和实测调优闭环。',
        'Open Cluster': '打开实例',
        'Real-time Monitor': '实时监控',
        'Real-time Monitoring': '实时监控',
        'Console': '控制台',
        'Anomaly lab': '异常实验室',
        'Run a real PostgreSQL pressure workload and inspect automatic recovery': '运行真实 PostgreSQL 压力负载并查看自动恢复',
        'Connecting': '正在连接',
        'Loading live host metadata…': '正在读取当前实例信息…',
        'Loading live PostgreSQL evidence…': '正在读取 PostgreSQL 实时证据…',
        'Connecting to the monitored PostgreSQL instance…': '正在连接被监控的 PostgreSQL 实例…',
        'No collected snapshots yet': '尚未采集到快照',
        'No live host matches these filters': '没有符合筛选条件的当前实例',
        'Table view selected': '已切换到表格视图',
        'Card view is available in the evidence console.': '卡片视图可在证据控制台中查看。',
        'This instance is configured by DATABASE_URL; add-host provisioning is intentionally outside the demo scope.': '当前实例由 DATABASE_URL 配置；添加外部主机不在本演示范围内。',
        'Batch operations are not applied to the monitored database by this read-only host view.': '只读主机视图不会对被监控数据库执行批量操作。',
        'Local container': '当前监控实例',
        'Host:': '实例：',
        'IP:': '地址：',
        'Type:': '类型：',
        'Legend': '图例',
        'persisted samples': '个持久化样本',
        'PostgreSQL demo': '当前 PostgreSQL 实例',
        'PostgreSQL API': 'PostgreSQL API',
        'KeenInsight Container Services': 'KeenInsight 实例服务',
        'Welcome to KeenInsight Container Services': '欢迎使用 KeenInsight 实例服务',
        'Performance Monitoring': '性能监控',
        'Live PostgreSQL evidence': 'PostgreSQL 实时证据',
        'Live PostgreSQL configuration': 'PostgreSQL 实时配置',
        'Compiler-grounded PostgreSQL flow': '编译器定位的 PostgreSQL 流',
        'Knowledge Snippets': '知识片段',
        'Measured feedback': '实测反馈',
        'Compiler-grounded source record': '编译器定位的源码记录',
        'Retrieved PostgreSQL evidence': '检索到的 PostgreSQL 证据',
        'Connection saturation': '连接数饱和',
        'Lock wait': '锁等待',
        'Slow query': '慢查询',
        'Temporary spill': '临时空间溢出',
        'Cache hit ratio': '缓存命中率',
        'No persisted incidents match these filters': '当前筛选条件没有匹配的持久化异常',
        'No persisted PostgreSQL statements match these filters': '当前筛选条件没有匹配的 PostgreSQL 语句',
        'Evidence retained': '已保留证据',
        'Run diagnosis': '运行诊断',
        'Resolve incident': '解决异常',
        'Open full detail': '打开完整详情',
        'Could not load incident': '无法加载异常',
        'Diagnosis completed and typed proposals were persisted.': '诊断完成，类型化建议已持久化。',
        'Diagnosis failed': '诊断失败',
        'Incident resolved.': '异常已解决。',
        'Resolve failed': '解决失败',
        'SQL lifecycle (eBPF)': 'SQL 生命周期（eBPF）',
        'observed function': '观测函数',
        'No duration-bearing eBPF event is linked to this query.': '没有与该查询关联的带耗时 eBPF 事件。',
        'Observed result': '观测结果',
        'Observed slow statement': '观测到慢语句',
        'Observed temporary spill': '观测到临时空间溢出',
        'Observed successful statement': '观测到成功语句',
        'No tuning action has been created for this incident.': '当前异常尚未创建调优操作。',
        'Rule-based differential profiling': '基于规则的差分画像',
        'Rule Matching and Recommendations': '规则匹配与建议',
        'Matched Diagnosis Rules': '匹配的诊断规则',
        'Tuning Actions': '调优操作',
        'Tuning-derived Knowledge': '调优经验知识',
        'No tuning-derived knowledge yet. Insights will appear as tuning records accumulate': '暂时没有调优经验知识；随着调优记录累积，洞察会显示在这里',
        'No matching persisted knowledge passages were returned.': '没有返回匹配的持久化知识段落。',
        'No matching persisted knowledge': '没有匹配的持久化知识',
        'No duration-bearing': '没有带耗时的',
        'SQL evidence not found': '没有找到 SQL 证据',
        'not measured': '尚未测量',
        'not recorded': '未记录',
        'configured rule': '已配置规则',
        'collector cycle': '采集周期',
        'runtime evidence': '运行时证据',
        'source reference': '源码引用',
        'source evidence': '源码证据',
        'database behavior': '数据库行为',
        'PostgreSQL monitoring views': 'PostgreSQL 监控视图',
        'High Availability': '高可用',
        'Monitoring & Alerts': '监控与异常',
        'Rule configuration': '规则配置',
        'Severity': '级别',
        'Changes are stored in keeninsight.alert_rules.': '变更已存储在 keeninsight.alert_rules。',
        'Adjust only through the validated rule endpoint.': '请通过已校验的规则接口调整。',
        'Compiler/source locator persisted': '已持久化编译器源码定位',
        'source context': '源码上下文',
        'Function Flow': '函数流',
        'Data Flow': '数据流',
        'Evidence chunk': '证据段落',
        'Knowledge entry': '知识条目',
        'PostgreSQL knowledge': 'PostgreSQL 知识',
        'verified': '已验证',
        'unverified': '未验证',
        'Database signal': '数据库信号',
        'Disk IO': '磁盘 I/O',
        'Network In': '网络入流量',
        'Network Out': '网络出流量',
        'QPS': 'QPS',
        'Slow Queries': '慢查询数',
        'Lock Wait Time': '锁等待时间',
        'Clusters': '实例',
        'System level': '系统层',
        'SQL level': 'SQL 层',
        'System Settings': '系统设置',
        'Knowledge Base': '知识库',
        'Tuning-derived Knowledge': '调优经验知识',
        'User Management': '用户管理',
        'System Config': '系统配置',
        'System Settings': '系统设置',
        'Diagnosis Rules': '诊断规则',
        'Alert Details': '异常详情',
        'Alert List': '异常列表',
        'Alert Rules': '异常规则',
        'Real-time Monitor': '实时监控',
        'SQL Logs': 'SQL 日志',
        'SQL Log': 'SQL 日志',
        'Code Source': '源码分析',
        'Knowledge Base': '知识库',
        'Overview': '总览',
        'Clusters': '实例',
        'Console': '控制台',
        'Help': '帮助',
        'User': '用户',
        'Manual': '使用手册',
        'System level': '系统层',
        'SQL level': 'SQL 层',
        'Filter Conditions': '筛选条件',
        'Host Name': '主机名',
        'IP Address': 'IP 地址',
        'Database Type': '数据库类型',
        'Database': '数据库',
        'CPU Usage': 'CPU 使用率',
        'Memory Usage': '内存使用率',
        'Disk Usage': '磁盘使用率',
        'Created At': '创建时间',
        'Actions': '操作',
        'Region': '区域',
        'All Regions': '全部区域',
        'All Status': '全部状态',
        'All Types': '全部类型',
        'Enter host name or ID': '输入主机名或 ID',
        'Enter IP address': '输入 IP 地址',
        'Reset': '重置',
        'Search': '搜索',
        'Batch Export': '批量导出',
        'Batch Start': '批量启动',
        'Batch Stop': '批量停止',
        'Add Host': '添加主机',
        'Refresh': '刷新',
        'Running': '运行中',
        'Warning': '警告',
        'Stopped': '已停止',
        'Monitor': '监控',
        'Previous': '上一页',
        'Next': '下一页',
        'items/page': '条/页',
        'Total': '总计',
        'Today': '今天',
        'Last 1 Hour': '最近 1 小时',
        'Last 6 Hours': '最近 6 小时',
        'Last 7 Days': '最近 7 天',
        'last day': '最近 1 天',
        'Legend': '图例',
        'Alert ID': '异常 ID',
        'Alert Level': '异常级别',
        'Detected At': '发现时间',
        'Trigger Reason': '触发原因',
        'Active': '活跃',
        'Resolved': '已解决',
        'Critical': '严重',
        'Info': '信息',
        'View Details': '查看详情',
        'Resolve': '解决',
        'Details': '详情',
        'Execution Status': '执行状态',
        'Execution Time': '执行耗时',
        'SQL Statement': 'SQL 语句',
        'SQL Type': 'SQL 类型',
        'Result': '结果',
        'Success': '成功',
        'Error': '错误',
        'Keyword': '关键词',
        'All': '全部',
        'Signal': '信号',
        'Status': '状态',
        'First seen': '首次发现',
        'Last seen': '最近发现',
        'Observed summary': '观测摘要',
        'Persisted evidence': '已持久化证据',
        'Typed actions': '类型化操作',
        'Run diagnosis': '运行诊断',
        'Resolve incident': '解决异常',
        'Open full detail': '打开完整详情',
        'Tuning actions': '调优操作',
        'Open Cluster': '打开实例',
        'Evidence Console': '证据控制台',
        'Live PostgreSQL': 'PostgreSQL 在线',
        'Database unavailable': '数据库不可用',
        'API unavailable': 'API 不可用'
    };
    const legacyTextEntries = Object.entries(legacyTextMap).sort((left, right) => right[0].length - left[0].length);
    const replaceLegacyText = (value) => legacyTextEntries.reduce((text, [source, target]) => text.split(source).join(target), String(value || ''));
    const legacyTitle = {
        'host-list.html': 'KeenInsight - 主机列表',
        'realtime-monitor.html': 'KeenInsight - 实时监控',
        'template.html': 'KeenInsight - 容器服务',
        'alert-list.html': 'KeenInsight - 异常列表',
        'alert-detail.html': 'KeenInsight - 异常详情',
        'alert-rules.html': 'KeenInsight - 异常规则',
        'code-source.html': 'KeenInsight - 源码分析',
        'manual.html': 'KeenInsight - 使用手册',
    };
    const translateLegacyInterface = () => {
        document.documentElement.lang = 'zh-CN';
        if (legacyTitle[page]) document.title = legacyTitle[page];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        const nodes = [];
        let node;
        while ((node = walker.nextNode())) nodes.push(node);
        nodes.forEach((textNode) => {
            const parent = textNode.parentElement;
            if (!parent || ['SCRIPT', 'STYLE', 'CODE', 'PRE', 'TEXTAREA', 'SVG'].includes(parent.tagName)) return;
            textNode.nodeValue = replaceLegacyText(textNode.nodeValue);
        });
        document.querySelectorAll('[placeholder], [title], [aria-label]').forEach((element) => {
            ['placeholder', 'title', 'aria-label'].forEach((attribute) => {
                if (element.hasAttribute(attribute)) element.setAttribute(attribute, replaceLegacyText(element.getAttribute(attribute)));
            });
        });
    };

    const latestDashboard = (items) => {
        const values = (items || []).filter((item) => item && !item.collection_error);
        return values.length ? values[values.length - 1] : null;
    };

    const databaseRates = (snapshot) => (((snapshot || {}).rates || {}).counters_per_second || {});

    const snapshotHost = (snapshot) => ((snapshot || {}).host || {});

    const serverName = (meta) => {
        const server = (meta || {}).server || meta || {};
        return server.database_name || '当前 PostgreSQL 实例';
    };

    const backendType = (meta) => {
        const server = (meta || {}).server || meta || {};
        return `PostgreSQL ${String(server.server_version || '').split(' ')[0] || ''}`.trim() || 'PostgreSQL';
    };

    const serverAddress = (meta) => {
        const server = (meta || {}).server || meta || {};
        return server.server_address || '本地套接字';
    };

    const incidentLabel = (kind) => ({
        connection_saturation: '连接数饱和',
        lock_wait: '锁等待',
        slow_query: '慢查询',
        temp_spill: '临时空间溢出',
        cache_hit_ratio: '缓存命中率'
    }[String(kind)] || String(kind || '数据库信号'));

    const incidentSummary = (incident) => {
        const evidence = incident && incident.evidence || {};
        const kind = String(incident && incident.kind || '');
        if (kind === 'lock_wait') return `当前有 ${(evidence.locks || []).length || evidence.waiting_sessions || 0} 个会话正在等待锁`;
        if (kind === 'slow_query') return `语句 ${evidence.query_id || '—'} 平均耗时 ${fixed(evidence.mean_exec_time_ms)} 毫秒，共调用 ${fixed(evidence.calls, 0)} 次`;
        if (kind === 'temp_spill') return `语句 ${evidence.query_id || '—'} 写入 ${fixed(evidence.temp_blks_written, 0)} 个临时块`;
        if (kind === 'connection_saturation' && evidence.numbackends !== undefined) return `当前连接数 ${fixed(evidence.numbackends, 0)}，已达到连接上限阈值`;
        if (kind === 'cache_hit_ratio' && evidence.ratio !== undefined) return `缓存命中率 ${fixed(Number(evidence.ratio) * 100, 1)}%，低于配置阈值`;
        return String(incident && incident.summary || '当前观测到数据库异常');
    };

    const loadCommon = async (metricLimit = 24) => {
        const results = await Promise.all([
            api('/meta'),
            api(`/dashboard/metrics?limit=${metricLimit}`)
        ]);
        state.meta = results[0];
        state.dashboard = results[1].items || [];
        return results;
    };

    const injectRuntimeStyle = () => {
        if (document.getElementById('ki-legacy-runtime-style')) return;
        const style = document.createElement('style');
        style.id = 'ki-legacy-runtime-style';
        style.textContent = `
            .ki-live-status { display:flex; align-items:center; gap:6px; color:#4e5969; font-size:12px; white-space:nowrap; }
            .ki-live-status-dot { width:7px; height:7px; border-radius:50%; background:#0fbf60; box-shadow:0 0 0 3px rgba(15,191,96,.12); }
            .ki-live-status-dot.offline { background:#ee4d38; box-shadow:0 0 0 3px rgba(238,77,56,.12); }
            .ki-live-toast { position:fixed; right:24px; bottom:24px; z-index:12000; padding:11px 16px; border-radius:6px; background:#1d2129; color:#fff; box-shadow:0 8px 24px rgba(29,33,41,.22); font-size:13px; opacity:0; transform:translateY(8px); pointer-events:none; transition:all .2s ease; }
            .ki-live-toast.show { opacity:1; transform:translateY(0); }
            .ki-live-toast.error { background:#c62828; }
            .ki-live-loading, .ki-live-error { padding:24px; text-align:center; color:#86909c; }
            .ki-live-error { color:#c62828; background:#fff5f5; border-radius:4px; }
            .ki-source-tag { display:inline-block; margin:2px 4px 2px 0; padding:2px 7px; color:#3370ff; background:#e8f3ff; border-radius:3px; font-size:11px; }
            .ki-live-value { font-weight:600; color:#1d2129; }
            .ki-live-muted { color:#86909c; font-size:12px; }
            .ki-live-pre { max-height:300px; overflow:auto; white-space:pre-wrap; word-break:break-word; background:#f7f8fa; padding:12px; border-radius:4px; font:12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; color:#282f38; }
            .ki-template-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:28px 0 22px; text-align:left; }
            .ki-template-card { background:#f7f8fa; border:1px solid #e5e6eb; border-radius:6px; padding:14px; }
            .ki-template-card-label { color:#86909c; font-size:12px; margin-bottom:4px; }
            .ki-template-card-value { color:#1d2129; font-size:20px; font-weight:600; }
            .ki-template-actions { display:flex; flex-wrap:wrap; justify-content:center; gap:10px; margin-top:16px; }
            .ki-template-actions a { display:inline-block; padding:8px 14px; border:1px solid #3370ff; border-radius:4px; color:#3370ff; text-decoration:none; font-size:13px; }
            .ki-template-actions a.primary { background:#3370ff; color:#fff; }
            .ki-live-note { margin:12px 0; padding:9px 12px; color:#4e5969; background:#f7f8fa; border-left:3px solid #3370ff; font-size:12px; }
            .ki-anomaly-entry { display:inline-flex; align-items:center; gap:6px; margin-left:12px; padding:5px 10px; border:1px solid #3370ff; border-radius:4px; color:#3370ff; background:#f7f9ff; text-decoration:none; font-size:12px; font-weight:600; white-space:nowrap; }
            .ki-anomaly-entry:hover { color:#fff; background:#3370ff; }
            .ki-anomaly-entry-dot { width:6px; height:6px; border-radius:50%; background:#0fbf60; }
            .ki-chart-empty { fill:#86909c; font:13px Arial,sans-serif; }
            .ki-detail-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
            .ki-detail-card { background:#f7f8fa; border-radius:6px; padding:12px; }
            .ki-detail-label { color:#86909c; font-size:11px; margin-bottom:4px; }
            .ki-detail-value { color:#282f38; font-size:13px; word-break:break-word; }
            .ki-live-workspace { margin:0 0 20px; padding:20px; background:linear-gradient(118deg,#f7faff 0%,#eef5ff 72%,#f7fbff 100%); border:1px solid #d7e3ff; border-radius:8px; }
            .ki-live-workspace-head { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; margin-bottom:17px; }
            .ki-live-workspace h2 { margin:0 0 5px; color:#1d2129; font-size:20px; }
            .ki-live-workspace h3 { margin:0; color:#1d2129; font-size:14px; }
            .ki-live-workspace p { margin:0; color:#86909c; font-size:12px; line-height:1.65; }
            .ki-live-workspace-actions { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
            .ki-live-workspace-actions a { display:inline-block; padding:7px 12px; border:1px solid #3370ff; border-radius:4px; color:#3370ff; background:#fff; text-decoration:none; font-size:12px; font-weight:600; }
            .ki-live-workspace-actions a.primary { color:#fff; background:#3370ff; }
            .ki-live-online { display:inline-flex; align-items:center; gap:6px; margin-bottom:7px; color:#0b9950; font-size:11px; font-weight:650; }
            .ki-live-online::before { content:''; width:7px; height:7px; border-radius:50%; background:#0fbf60; box-shadow:0 0 0 3px rgba(15,191,96,.12); }
            .ki-live-metrics { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; margin-bottom:16px; }
            .ki-live-metric { min-width:0; padding:12px; border:1px solid #dce6f8; border-radius:6px; background:rgba(255,255,255,.82); }
            .ki-live-metric-label { color:#86909c; font-size:11px; }
            .ki-live-metric-value { margin-top:5px; color:#1d2129; font-size:19px; font-weight:650; overflow-wrap:anywhere; }
            .ki-live-metric-foot { margin-top:3px; color:#86909c; font-size:10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
            .ki-live-columns { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
            .ki-live-panel { min-width:0; padding:13px; border:1px solid #e5e6eb; border-radius:6px; background:#fff; }
            .ki-live-panel-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:9px; }
            .ki-live-count { color:#3370ff; background:#e8f3ff; border-radius:10px; padding:2px 7px; font-size:10px; font-weight:650; }
            .ki-live-list { display:grid; gap:8px; }
            .ki-live-list-item { min-width:0; padding-bottom:8px; border-bottom:1px solid #f2f3f5; }
            .ki-live-list-item:last-child { padding-bottom:0; border-bottom:0; }
            .ki-live-list-main { display:flex; justify-content:space-between; align-items:center; gap:8px; color:#1d2129; font-size:11px; font-weight:600; }
            .ki-live-list-text { margin-top:3px; color:#4e5969; font-size:11px; line-height:1.45; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
            .ki-live-list-meta { margin-top:3px; color:#86909c; font-size:10px; overflow-wrap:anywhere; }
            .ki-live-empty { color:#86909c; font-size:11px; line-height:1.6; }
            .ki-live-pending { visibility:hidden; }
            .ki-live-ready { visibility:visible; }
            @media (max-width:1100px) { .ki-live-metrics { grid-template-columns:repeat(3,minmax(0,1fr)); } .ki-live-columns { grid-template-columns:1fr; } }
            @media (max-width:700px) { .ki-live-workspace-head { flex-direction:column; } .ki-live-workspace-actions { justify-content:flex-start; } .ki-live-metrics { grid-template-columns:repeat(2,minmax(0,1fr)); } }
            @media (max-width:900px) { .ki-template-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .ki-detail-grid { grid-template-columns:1fr; } }
        `;
        document.head.appendChild(style);
    };

    const enhanceNavigation = () => {
        const right = document.querySelector('.navbar-right');
        if (right && !right.querySelector('.ki-anomaly-entry')) {
            const entry = document.createElement('a');
            entry.className = 'ki-anomaly-entry';
            entry.id = 'ki-anomaly-entry';
            entry.href = '/console#workloads';
            entry.title = '运行真实 PostgreSQL 压力负载并查看自动恢复';
            entry.innerHTML = '<span class="ki-anomaly-entry-dot"></span><span>异常实验室</span>';
            right.insertBefore(entry, right.firstChild);
        }
        if (right && !right.querySelector('.ki-live-status')) {
            const status = document.createElement('div');
            status.className = 'ki-live-status';
            status.innerHTML = '<span class="ki-live-status-dot offline"></span><span>正在连接</span>';
            right.insertBefore(status, right.firstChild);
            state.statusElement = status;
        }
        document.querySelectorAll('.nav-action').forEach((item) => {
            const label = item.textContent.trim().toLowerCase();
            if (label.includes('console')) {
                item.onclick = () => { window.location.href = '/console'; };
            } else if (label.includes('system settings') || label.includes('系统设置')) {
                item.onclick = () => { window.location.href = '/console#settings'; };
            } else if (label.includes('help')) {
                item.onclick = () => { window.location.href = 'manual.html'; };
            } else if (label.includes('user')) {
                item.onclick = () => notify(`${serverName(state.meta)} · ${serverAddress(state.meta)}`);
            }
        });
        document.querySelectorAll('.nav-link').forEach((item) => {
            const label = item.textContent.trim().toLowerCase();
            if (label.includes('overview') || label.includes('总览')) item.onclick = () => { window.location.href = 'host-list.html'; };
            if (label.includes('performance') || label.includes('性能')) item.onclick = () => { window.location.href = 'realtime-monitor.html'; };
            if (label.includes('security') || label.includes('audit') || label.includes('安全')) item.onclick = () => { window.location.href = '/console#audit'; };
            if (label.includes('capacity') || label.includes('容量')) item.onclick = () => { window.location.href = '/console#settings'; };
        });
        document.querySelectorAll('.menu-item').forEach((item) => {
            const label = item.textContent.trim().toLowerCase();
            if (label.includes('system config') || label.includes('系统配置')) item.onclick = () => { window.location.href = '/console#settings'; };
            if (label.includes('user management') || label.includes('用户管理')) item.onclick = () => notify('当前演示使用单一受控用户；用户管理不参与 PostgreSQL 调优闭环。');
        });
        api('/health').then((health) => {
            if (!state.statusElement) return;
            const dot = state.statusElement.querySelector('.ki-live-status-dot');
            const label = state.statusElement.querySelector('span:last-child');
            const ok = Boolean(health.ok && health.database);
            dot.classList.toggle('offline', !ok);
            label.textContent = ok ? 'PostgreSQL 在线' : '数据库不可用';
        }).catch(() => {
            if (!state.statusElement) return;
            state.statusElement.querySelector('span:last-child').textContent = 'API 不可用';
        });
    };

    const clearFixtureTargets = () => {
        // The supplied pages contain illustrative rows and chart samples.  We
        // keep their render functions for visual compatibility, but remove
        // fixture output before DOMContentLoaded so a network delay can never
        // be mistaken for live evidence.
        const bodyIds = {
            'alert-list.html': 'alertTableBody',
            'alert-rules.html': 'ruleTableBody',
            'code-source.html': 'functionTableBody',
        };
        const bodyId = bodyIds[page];
        if (bodyId) {
            const body = document.getElementById(bodyId);
            if (body) { body.innerHTML = ''; body.classList.add('ki-live-pending'); }
        }
        if (page === 'host-list.html') {
            const body = document.querySelector('.data-table tbody');
            if (body) { body.innerHTML = ''; body.classList.add('ki-live-pending'); }
        }
        if (page === 'manual.html') {
            const target = document.getElementById('snippetsContainer'); if (target) target.innerHTML = '';
        }
        if (page === 'tuning-knowledge.html') {
            const target = document.getElementById('tuningContainer'); if (target) target.innerHTML = '';
        }
        if (page === 'alert-detail.html' || page === 'sql-detail.html') {
            const target = document.getElementById('detailContent'); if (target) target.innerHTML = '';
        }
        if (page === 'alert-detail.html' || page === 'sql-detail.html' || page === 'alert-list.html') {
            ['kbModalOverlay .kb-modal-content', '#modalBody', '#sqlModalBody'].forEach((selector) => {
                const target = document.querySelector(selector);
                if (target) target.innerHTML = '';
            });
        }
        if (page === 'realtime-monitor.html') {
            ['systemLegend', 'databaseLegend'].forEach((id) => { const target = document.getElementById(id); if (target) target.innerHTML = ''; });
            ['systemChart', 'databaseChart'].forEach((id) => {
                const canvas = document.getElementById(id);
                if (canvas && canvas.getContext) {
                    canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
                    canvas.classList.add('ki-live-pending');
                }
            });
        }
        translateLegacyInterface();
    };

    const hostFromStorage = () => {
        try { return JSON.parse(window.localStorage.getItem('selectedHost') || 'null'); } catch (error) { return null; }
    };

    const setSelectedHost = (host) => {
        try { window.localStorage.setItem('selectedHost', JSON.stringify(host)); } catch (error) { /* private mode */ }
    };

    const navigateMonitor = (name, address, type) => {
        setSelectedHost({ name, ip: address, type });
        window.location.href = 'realtime-monitor.html';
    };

    const renderUsage = (value, colorClass) => {
        const number = percent(value);
        const width = number === null ? 0 : number;
        return `<div class="usage-bar"><div class="progress-container"><div class="progress-bar ${colorClass || 'progress-low'}" style="width:${width}%;"></div></div><span class="usage-value">${number === null ? '—' : `${number.toFixed(1)}%`}</span></div>`;
    };

    const renderTemplate = async () => {
        const target = document.querySelector('.content-placeholder');
        showLoading(target, '正在连接被监控的 PostgreSQL 实例…');
        try {
            await loadCommon(1);
            const latest = latestDashboard(state.dashboard) || {};
            const host = snapshotHost(latest);
            const database = latest.database || {};
            const rates = databaseRates(latest);
            const transactionRate = numberOrNull(rates.xact_commit) === null && numberOrNull(rates.xact_rollback) === null
                ? null : (numberOrNull(rates.xact_commit) || 0) + (numberOrNull(rates.xact_rollback) || 0);
            target.innerHTML = `
                <h2>欢迎使用 KeenInsight 实例服务</h2>
                <p>实时证据来源：<strong>${escapeHtml(serverName(state.meta))}</strong> · ${escapeHtml(backendType(state.meta))} · ${sourceTag('PostgreSQL API')}</p>
                <div class="ki-template-grid">
                    <div class="ki-template-card"><div class="ki-template-card-label">数据库会话</div><div class="ki-template-card-value">${valueOrDash(database.numbackends)}</div></div>
                    <div class="ki-template-card"><div class="ki-template-card-label">事务 / 秒</div><div class="ki-template-card-value">${fixed(transactionRate, 1)}</div></div>
                    <div class="ki-template-card"><div class="ki-template-card-label">CPU 使用率</div><div class="ki-template-card-value">${fixed(host.cpu_usage_pct, 1)}${host.cpu_usage_pct == null ? '' : '%'}</div></div>
                    <div class="ki-template-card"><div class="ki-template-card-label">采集器周期数</div><div class="ki-template-card-value">${valueOrDash((state.meta.collector || {}).cycles)}</div></div>
                </div>
                <div class="ki-live-note">以上数值来自持久化快照和当前 PostgreSQL 连接。下方入口提供异常、源码分析和实测调优闭环。</div>
                <div class="ki-template-actions">
                    <a class="primary" href="host-list.html">打开实例</a>
                    <a href="realtime-monitor.html">实时监控</a>
                    <a href="alert-list.html">异常列表</a>
                    <a href="../console.html">证据控制台</a>
                </div>
            `;
        } catch (error) {
            showError(target, error);
        }
    };

    const renderLiveHostOverview = async () => {
        if (page !== 'host-list.html') return;
        const header = document.querySelector('.page-header');
        if (!header) return;
        let target = document.getElementById('ki-live-workspace');
        if (!target) {
            target = document.createElement('section');
            target.id = 'ki-live-workspace';
            header.insertAdjacentElement('afterend', target);
        }
        target.className = 'ki-live-workspace';
        showLoading(target, '正在读取当前 PostgreSQL 实例的实时证据…');
        try {
            const results = await Promise.all([
                api('/dashboard'),
                optionalApi('/activity?limit=6', { items: [] }),
                optionalApi('/locks', { items: [] }),
            ]);
            const dashboard = results[0] || {};
            const snapshot = (dashboard.snapshot || {}).payload || {};
            const compact = latestDashboard(state.dashboard) || {};
            const host = snapshot.host || compact.host || {};
            const database = snapshot.database || compact.database || {};
            const rates = (snapshot.rates || {}).counters_per_second || databaseRates(compact);
            const server = (state.meta || {}).server || {};
            const activities = results[1].items || [];
            const lockRows = results[2].items || [];
            const incidents = dashboard.active_incidents || [];
            const txCommit = numberOrNull(rates.xact_commit);
            const txRollback = numberOrNull(rates.xact_rollback);
            const txRate = txCommit === null && txRollback === null ? null : (txCommit || 0) + (txRollback || 0);
            const percentage = (value) => { const number = numberOrNull(value); return number === null ? '—' : `${number.toFixed(1)}%`; };
            const metric = (label, value, foot) => `<div class="ki-live-metric"><div class="ki-live-metric-label">${escapeHtml(label)}</div><div class="ki-live-metric-value">${escapeHtml(value)}</div><div class="ki-live-metric-foot">${escapeHtml(foot)}</div></div>`;
            const severity = (value) => ({ critical: '严重', warning: '警告', info: '信息' }[String(value)] || String(value || '信息'));
            const activityHtml = activities.length ? activities.slice(0, 5).map((row) => `<div class="ki-live-list-item"><div class="ki-live-list-main"><span>PID ${escapeHtml(row.pid)}</span><span class="ki-live-count">${escapeHtml({ active: '活跃', idle: '空闲', 'idle in transaction': '事务空闲' }[row.state] || row.state || '未知')}</span></div><div class="ki-live-list-text">${escapeHtml(String(row.query || '（无查询文本）').replace(/\s+/g, ' ').trim())}</div><div class="ki-live-list-meta">${escapeHtml(row.application_name || '未命名应用')} · ${escapeHtml(row.wait_event || '未等待')}${row.query_start ? ` · ${escapeHtml(formatDate(row.query_start))}` : ''}</div></div>`).join('') : '<div class="ki-live-empty">当前实例没有可展示的实时会话。控制台连接已从列表中排除。</div>';
            const locksHtml = lockRows.length ? lockRows.slice(0, 5).map((row) => `<div class="ki-live-list-item"><div class="ki-live-list-main"><span>等待 PID ${escapeHtml(row.waiting_pid)}</span><span class="ki-live-count">锁等待</span></div><div class="ki-live-list-text">${escapeHtml(row.relation || row.locktype || '未命名对象')} · ${escapeHtml(row.waiting_mode || '未知模式')}</div><div class="ki-live-list-meta">阻塞 PID ${escapeHtml(row.blocking_pid || '—')} · ${escapeHtml(row.wait_event || '未提供等待事件')}</div></div>`).join('') : '<div class="ki-live-empty">当前没有未获授权的锁请求。启动“锁竞争”场景后，这里会显示真实阻塞关系。</div>';
            const incidentsHtml = incidents.length ? incidents.slice(0, 4).map((incident) => `<div class="ki-live-list-item"><div class="ki-live-list-main"><span>${escapeHtml(severity(incident.severity))} · ${escapeHtml(incidentLabel(incident.kind))}</span><span class="ki-live-count">活跃</span></div><div class="ki-live-list-text">${escapeHtml(incidentSummary(incident))}</div><div class="ki-live-list-meta"><a href="alert-detail.html?id=${encodeURIComponent(incident.id)}">查看异常 ${escapeHtml(incident.id)}</a> · ${escapeHtml(formatDate(incident.last_seen || incident.first_seen))}</div></div>`).join('') : '<div class="ki-live-empty">当前没有活跃异常。规则仍在后台持续评估。</div>';
            target.innerHTML = `<div class="ki-live-workspace-head"><div><div class="ki-live-online">PostgreSQL 实例在线</div><h2>当前实例实时工作区</h2><p>指标、会话和锁均来自这台机器上当前连接的 PostgreSQL；页面不再展示跨数据库的示例主机。</p></div><div class="ki-live-workspace-actions"><a class="primary" href="realtime-monitor.html">打开实时监控</a><a href="alert-list.html">异常列表</a><a href="/console#workloads">异常实验室</a></div></div><div class="ki-live-metrics">${metric('活跃会话', valueOrDash(database.numbackends), 'pg_stat_database.numbackends')}${metric('CPU 使用率', percentage(host.cpu_usage_pct), '当前采集周期')}${metric('内存使用率', percentage(host.memory_used_pct), formatBytes(host.memory_available_bytes) + ' 可用')}${metric('磁盘使用率', percentage(host.disk_used_pct), formatBytes(host.disk_free_bytes) + ' 可用')}${metric('事务 / 秒', valueOrDash(fixed(txRate, 1)), '提交 + 回滚')}${metric('临时字节 / 秒', formatBytes(rates.temp_bytes), 'pg_stat_database')}</div><div class="ki-live-columns"><div class="ki-live-panel"><div class="ki-live-panel-head"><h3>当前异常</h3><span class="ki-live-count">${escapeHtml(incidents.length)} 条</span></div><div class="ki-live-list">${incidentsHtml}</div></div><div class="ki-live-panel"><div class="ki-live-panel-head"><h3>锁等待</h3><span class="ki-live-count">${escapeHtml(lockRows.length)} 条</span></div><div class="ki-live-list">${locksHtml}</div></div></div><div class="ki-live-note">数据源：${escapeHtml(server.database_name || serverName(state.meta))} · ${escapeHtml(backendType(state.meta))} · 最近快照 ${escapeHtml(formatDate((dashboard.snapshot || {}).collected_at || compact.collected_at))}。实时会话和锁接口读取失败时会明确显示空状态。</div>`;
            target.classList.add('ki-live-ready');
        } catch (error) {
            showError(target, error);
        }
    };

    const hostState = { row: null, host: null };

    const renderHostRow = (host) => {
        const tbody = document.querySelector('.data-table tbody');
        if (!tbody) return;
        hostState.host = host;
        tbody.innerHTML = `
            <tr data-ki-host-row>
                <td onclick="event.stopPropagation()"><div class="checkbox-wrapper"><div class="custom-checkbox"></div></div></td>
                <td><div class="host-name"><span class="host-name-main">${escapeHtml(host.name)}</span><span class="host-name-sub">${escapeHtml(host.id)}</span></div></td>
                <td><span class="ip-address">${escapeHtml(host.address)}</span></td>
                <td><span class="status-tag status-running">运行中</span></td>
                <td><span class="db-tag">${escapeHtml(host.type)}</span></td>
                <td>${renderUsage(host.cpu, host.cpu == null ? 'progress-low' : host.cpu > 70 ? 'progress-high' : 'progress-low')}</td>
                <td>${renderUsage(host.memory, host.memory == null ? 'progress-low' : host.memory > 70 ? 'progress-high' : 'progress-low')}</td>
                <td>${renderUsage(host.disk, host.disk == null ? 'progress-low' : host.disk > 70 ? 'progress-high' : 'progress-low')}</td>
                <td>当前监控实例</td>
                <td>${escapeHtml(formatDate(host.created))}</td>
                <td><span class="action-link" data-ki-monitor>监控</span></td>
            </tr>
        `;
        tbody.classList.remove('ki-live-pending');
        tbody.classList.add('ki-live-ready');
        hostState.row = tbody.querySelector('[data-ki-host-row]');
        hostState.row.addEventListener('click', () => navigateMonitor(host.name, host.address, host.type));
        hostState.row.querySelector('[data-ki-monitor]').addEventListener('click', (event) => {
            event.stopPropagation(); navigateMonitor(host.name, host.address, host.type);
        });
    };

    const filterHost = () => {
        if (!hostState.row || !hostState.host) return;
        const inputs = Array.from(document.querySelectorAll('.search-grid .search-input'));
        const selects = Array.from(document.querySelectorAll('.search-grid .search-select'));
        const name = (inputs[0] && inputs[0].value || '').toLowerCase();
        const address = (inputs[1] && inputs[1].value || '').toLowerCase();
        const status = selects[0] && selects[0].value;
        const type = selects[1] && selects[1].value;
        const show = (!name || `${hostState.host.name} ${hostState.host.id}`.toLowerCase().includes(name))
            && (!address || hostState.host.address.toLowerCase().includes(address))
            && (!status || status === 'running')
            && (!type || (type === 'postgresql' && /postgresql/i.test(hostState.host.type)));
        hostState.row.style.display = show ? '' : 'none';
        const tbody = hostState.row.parentElement;
        let empty = tbody.querySelector('[data-ki-host-empty]');
        if (!show && !empty) {
            empty = document.createElement('tr'); empty.dataset.kiHostEmpty = 'true';
            empty.innerHTML = '<td colspan="11" class="ki-live-muted" style="text-align:center;padding:28px;">没有符合筛选条件的当前实例</td>'; tbody.appendChild(empty);
        } else if (show && empty) empty.remove();
    };

    const bindHostControls = () => {
        const headerButtons = Array.from(document.querySelectorAll('.page-actions .btn'));
        if (headerButtons[0]) headerButtons[0].onclick = () => loadHostPage();
        if (headerButtons[1]) headerButtons[1].onclick = () => notify('当前实例由 DATABASE_URL 配置；添加外部主机不在本演示范围内。');
        const actionButtons = Array.from(document.querySelectorAll('.search-actions .btn'));
        if (actionButtons[0]) actionButtons[0].onclick = () => { document.querySelectorAll('.search-grid input').forEach((input) => { input.value = ''; }); document.querySelectorAll('.search-grid select').forEach((select) => { select.value = ''; }); filterHost(); };
        if (actionButtons[1]) actionButtons[1].onclick = filterHost;
        document.querySelectorAll('.search-grid input,.search-grid select').forEach((element) => element.addEventListener('change', filterHost));
        document.querySelectorAll('.search-grid input').forEach((element) => element.addEventListener('input', filterHost));
        document.querySelectorAll('.batch-actions .btn').forEach((button) => button.onclick = () => notify('只读主机视图不会对被监控数据库执行批量操作。'));
        document.querySelectorAll('.view-toggle .view-btn').forEach((button, index, buttons) => button.onclick = () => {
            buttons.forEach((item) => item.classList.remove('active')); button.classList.add('active');
            notify(index === 0 ? '已切换到表格视图' : '卡片视图可在证据控制台中查看。');
        });
    };

    const loadHostPage = async () => {
        const tbody = document.querySelector('.data-table tbody');
        showLoading(tbody, '正在读取当前实例信息…');
        try {
            await loadCommon(2);
            const latest = latestDashboard(state.dashboard) || {};
            const host = snapshotHost(latest);
            renderHostRow({
                name: serverName(state.meta),
                id: `${serverName(state.meta)}:${(state.meta.server || {}).server_port || 5432}`,
                address: serverAddress(state.meta),
                type: backendType(state.meta),
                cpu: host.cpu_usage_pct,
                memory: host.memory_used_pct,
                disk: host.disk_used_pct,
                created: (state.meta.server || {}).postmaster_start_time
            });
            await renderLiveHostOverview();
            const footer = document.querySelector('.pagination-info');
            if (footer) footer.innerHTML = '<span>当前实例 <strong>1</strong> 个，显示 <strong>1-1</strong></span>';
            const pagination = document.querySelector('.pagination');
            if (pagination) pagination.style.display = 'none';
            bindHostControls();
        } catch (error) { if (tbody) tbody.classList.remove('ki-live-pending'); showError(tbody, error); }
    };

    const chartSeries = (items) => {
        const series = [
            { name: 'CPU 使用率', color: '#3370ff', unit: '%', values: items.map((item) => snapshotHost(item).cpu_usage_pct) },
            { name: '内存使用率', color: '#0fbf60', unit: '%', values: items.map((item) => snapshotHost(item).memory_used_pct) },
            { name: '磁盘使用率', color: '#ff8b07', unit: '%', values: items.map((item) => snapshotHost(item).disk_used_pct) },
            { name: '1 分钟负载', color: '#722ed1', unit: '', values: items.map((item) => snapshotHost(item).load1) }
        ];
        const rates = [
            { name: '事务 / 秒', color: '#0fbf60', unit: '/s', values: items.map((item) => { const rates = databaseRates(item); const commit = numberOrNull(rates.xact_commit); const rollback = numberOrNull(rates.xact_rollback); return commit === null && rollback === null ? null : (commit || 0) + (rollback || 0); }) },
            { name: '返回行数 / 秒', color: '#3370ff', unit: '/s', values: items.map((item) => databaseRates(item).tup_returned) },
            { name: '数据库会话', color: '#ff8b07', unit: '', values: items.map((item) => (item.database || {}).numbackends) },
            { name: '缓存命中率', color: '#ee4d38', unit: '%', values: items.map((item) => {
                const db = item.database || {}; const hit = numberOrNull(db.blks_hit); const read = numberOrNull(db.blks_read);
                return hit === null || read === null || hit + read === 0 ? null : (hit / (hit + read)) * 100;
            }) }
        ];
        return { system: series, database: rates };
    };

    const drawLiveChart = (canvas, definitions, items, legendId) => {
        if (!canvas) return;
        const width = Math.max(420, canvas.clientWidth || 720);
        const height = Math.max(240, canvas.clientHeight || 300);
        const ratio = window.devicePixelRatio || 1;
        canvas.width = width * ratio; canvas.height = height * ratio;
        canvas.style.width = `${width}px`; canvas.style.height = `${height}px`;
        const context = canvas.getContext('2d'); context.setTransform(ratio, 0, 0, ratio, 0, 0);
        context.clearRect(0, 0, width, height);
        const padding = { left: 48, right: 16, top: 18, bottom: 32 };
        const chartWidth = width - padding.left - padding.right;
        const chartHeight = height - padding.top - padding.bottom;
        const all = definitions.flatMap((definition) => definition.values).filter((value) => numberOrNull(value) !== null).map(Number);
        const min = all.length ? Math.min(...all) : 0;
        const max = all.length ? Math.max(...all) : 1;
        const span = Math.max(1e-9, max - min);
        context.font = '11px Arial'; context.fillStyle = '#86909c'; context.strokeStyle = '#e5e6eb'; context.lineWidth = 1;
        for (let tick = 0; tick <= 4; tick += 1) {
            const y = padding.top + chartHeight * tick / 4;
            context.beginPath(); context.moveTo(padding.left, y); context.lineTo(width - padding.right, y); context.stroke();
            const label = (max - span * tick / 4).toFixed(max > 100 ? 0 : 1);
            context.fillText(label, 6, y + 4);
        }
        context.beginPath(); context.moveTo(padding.left, padding.top); context.lineTo(padding.left, height - padding.bottom); context.lineTo(width - padding.right, height - padding.bottom); context.stroke();
        if (!all.length) {
            context.fillStyle = '#86909c'; context.fillText('尚未采集到快照', padding.left + 12, padding.top + chartHeight / 2);
        }
        definitions.forEach((definition) => {
            context.strokeStyle = definition.color; context.lineWidth = 2; context.beginPath();
            let started = false;
            definition.values.forEach((value, index) => {
                const number = numberOrNull(value); if (number === null) { started = false; return; }
                const x = padding.left + (definition.values.length <= 1 ? chartWidth / 2 : chartWidth * index / (definition.values.length - 1));
                const y = padding.top + chartHeight * (max - number) / span;
                if (!started) context.moveTo(x, y); else context.lineTo(x, y); started = true;
            });
            context.stroke();
        });
        const legend = document.getElementById(legendId);
        if (legend) legend.innerHTML = `<div class="legend-title">图例 <span class="ki-live-muted">（${items.length} 个持久化样本）</span></div>` + definitions.map((definition) => `<div class="legend-item"><span class="legend-color" style="background:${definition.color}"></span><span>${escapeHtml(definition.name)}</span></div>`).join('');
        canvas.classList.remove('ki-live-pending');
        canvas.classList.add('ki-live-ready');
    };

    const renderRealtimeCharts = () => {
        const counts = { today: 48, '1h': 60, '6h': 120, '24h': 96, '7d': 672 };
        const count = counts[state.chart.range] || 96;
        const items = state.chart.items.slice(-count);
        const series = chartSeries(items);
        drawLiveChart(document.getElementById('systemChart'), series.system, items, 'systemLegend');
        drawLiveChart(document.getElementById('databaseChart'), series.database, items, 'databaseLegend');
    };

    const loadRealtimePage = async () => {
        try {
            await loadCommon(120);
            const selected = hostFromStorage();
            const host = selected || { name: serverName(state.meta), ip: serverAddress(state.meta), type: backendType(state.meta) };
            const name = document.getElementById('hostName'); const ip = document.getElementById('hostIP'); const type = document.getElementById('hostType');
            if (name) name.textContent = host.name || serverName(state.meta); if (ip) ip.textContent = host.ip || serverAddress(state.meta); if (type) type.textContent = host.type || backendType(state.meta);
            state.chart.items = state.dashboard;
            window.drawAllCharts = renderRealtimeCharts;
            document.querySelectorAll('.time-btn').forEach((button) => {
                button.onclick = () => { document.querySelectorAll('.time-btn').forEach((item) => item.classList.remove('active')); button.classList.add('active'); state.chart.range = button.dataset.range || '24h'; renderRealtimeCharts(); };
            });
            renderRealtimeCharts();
        } catch (error) {
            document.querySelectorAll('#systemChart,#databaseChart').forEach((canvas) => canvas.classList.remove('ki-live-pending'));
            notify(`实时监控不可用：${error.message}`, 'error');
        }
    };

    window.KeenInsightLegacy = { api, notify, translate: translateLegacyInterface, refresh: () => initPage() };

    const initPage = async () => {
        injectRuntimeStyle();
        enhanceNavigation();
        clearFixtureTargets();
        let result = null;
        if (page === 'template.html') result = await renderTemplate();
        if (page === 'host-list.html') result = await loadHostPage();
        if (page === 'realtime-monitor.html') result = await loadRealtimePage();
        translateLegacyInterface();
        return result;
    };

    clearFixtureTargets();
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initPage);
    else initPage();
})();
