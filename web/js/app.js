/**
 * RUC Database Replay Tool - Application Logic
 */

// ========== State Management ==========
const state = {
    currentTaskId: null,
    currentStmtPage: 0,
    currentDivPage: 0,
    replayStartTime: null,
    progressInterval: null,
    charts: {
        type: null,
        operation: null
    }
};

// ========== Initialization ==========
document.addEventListener('DOMContentLoaded', () => {
    console.log('RUC Database Replay Tool - Frontend Loaded');
    initNavigation();
    initFileUpload();
    initServerStatus();

    // Check initial server status
    checkServerStatus();
    setInterval(checkServerStatus, 30000);
});

// ========== Navigation ==========
function initNavigation() {
    document.querySelectorAll('[data-page]').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const page = e.target.closest('[data-page]').dataset.page;
            showPage(page);
        });
    });
}

function showPage(page) {
    // Hide all pages
    document.querySelectorAll('.page-section').forEach(el => {
        el.classList.add('hidden');
        el.classList.remove('fade-in');
    });

    // Show target page
    const targetPage = document.getElementById(page + 'Page');
    if (targetPage) {
        targetPage.classList.remove('hidden');
        targetPage.classList.add('fade-in');
    }

    // Update navigation
    document.querySelectorAll('[data-page]').forEach(link => {
        link.classList.remove('active');
        if (link.dataset.page === page) {
            link.classList.add('active');
        }
    });

    // Page specific logic
    if (page === 'report' && state.currentTaskId) {
        loadReport();
    }
}

// ========== Toast Notifications ==========
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast-custom toast-${type}`;

    let iconClass = 'bi-info-circle';
    if (type === 'success') iconClass = 'bi-check-circle-fill';
    if (type === 'error') iconClass = 'bi-x-circle-fill';

    let color = 'var(--accent-blue)';
    if (type === 'success') color = 'var(--accent-green)';
    if (type === 'error') color = 'var(--accent-red)';

    toast.innerHTML = `
        <i class="bi ${iconClass}" style="color: ${color}; font-size: 1.2rem;"></i>
        <span>${message}</span>
    `;

    container.appendChild(toast);

    // Auto remove after 3 seconds
    setTimeout(() => {
        toast.style.animation = 'slideOutRight 0.4s cubic-bezier(0.16, 1, 0.3, 1) forwards';
        setTimeout(() => toast.remove(), 400);
    }, 3000);
}

// Inject slideOutRight animation style if not exists
if (!document.getElementById('anim-style')) {
    const style = document.createElement('style');
    style.id = 'anim-style';
    style.innerHTML = `
        @keyframes slideOutRight {
            from { transform: translateX(0); opacity: 1; }
            to { transform: translateX(100%); opacity: 0; }
        }
    `;
    document.head.appendChild(style);
}

// ========== File Upload ==========
function initFileUpload() {
    const uploadZone = document.getElementById('uploadZone');
    const fileInput = document.getElementById('logFile');

    uploadZone.addEventListener('click', () => fileInput.click());

    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        uploadZone.addEventListener(eventName, (e) => {
            e.preventDefault();
            e.stopPropagation();
        });
    });

    ['dragenter', 'dragover'].forEach(eventName => {
        uploadZone.addEventListener(eventName, () => uploadZone.classList.add('dragover'));
    });

    ['dragleave', 'drop'].forEach(eventName => {
        uploadZone.addEventListener(eventName, () => uploadZone.classList.remove('dragover'));
    });

    uploadZone.addEventListener('drop', (e) => {
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            fileInput.files = files;
            updateFileInfo(files[0]);
        }
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files[0]) {
            updateFileInfo(e.target.files[0]);
        }
    });
}

function updateFileInfo(file) {
    document.getElementById('fileName').textContent = file.name;
    document.getElementById('fileSize').textContent = formatFileSize(file.size);
    document.getElementById('fileInfo').classList.add('show');
}

function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// ========== Prepare Phase ==========
window.submitPrepare = async function () {
    const btn = document.getElementById('prepareBtn');
    const file = document.getElementById('logFile').files[0];

    if (!file) {
        showToast('请选择日志文件', 'error');
        return;
    }

    btn.disabled = true;
    btn.innerHTML = '<span class="loading-spinner me-2"></span> 解析中...';

    const formData = new FormData();
    formData.append('db_host', document.getElementById('dbHost').value);
    formData.append('db_port', document.getElementById('dbPort').value);
    formData.append('db_user', document.getElementById('dbUser').value);
    formData.append('db_pass', document.getElementById('dbPass').value);
    formData.append('db_name', document.getElementById('dbName').value);
    formData.append('log_file', file);

    try {
        const response = await fetch('/api/v1/replay/prepare', {
            method: 'POST',
            body: formData
        });

        const result = await response.json();

        if (result.code === 0) {
            state.currentTaskId = result.data.task_id;
            showPrepareResult(result.data);
            showToast('日志解析完成！', 'success');
        } else {
            showToast('解析失败: ' + result.msg, 'error');
        }
    } catch (error) {
        showToast('网络错误: ' + error.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-rocket-takeoff"></i> 开始解析';
    }
};

function showPrepareResult(data) {
    document.getElementById('prepareResult').classList.remove('hidden');
    document.getElementById('prepareResult').classList.add('fade-in');

    document.getElementById('taskIdShort').textContent = data.task_id.substring(0, 8);
    document.getElementById('stmtCount').textContent = formatNumber(data.total_statements);
    document.getElementById('txCount').textContent = formatNumber(data.total_transactions);
    document.getElementById('sessionCount').textContent = data.statistics?.session_count || '-';
    document.getElementById('taskStatus').textContent = data.status;

    // Draw charts
    if (data.statistics) {
        drawCharts(data.statistics);
    }
}

function drawCharts(stats) {
    // SQL Type Distribution
    const typeCtx = document.getElementById('typeChart').getContext('2d');
    if (state.charts.type) state.charts.type.destroy();

    const typeData = stats.by_type || {};
    state.charts.type = new Chart(typeCtx, {
        type: 'doughnut',
        data: {
            labels: Object.keys(typeData),
            datasets: [{
                data: Object.values(typeData),
                backgroundColor: ['#10b981', '#f59e0b', '#3b82f6', '#ef4444', '#8b5cf6'],
                borderWidth: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '70%',
            plugins: {
                legend: {
                    position: 'right',
                    labels: {
                        color: '#4b5563',
                        font: { family: "'Inter', sans-serif" },
                        padding: 20,
                        usePointStyle: true
                    }
                },
                title: {
                    display: true,
                    text: 'SQL 类型分布',
                    color: '#111827',
                    font: { size: 16, weight: 'bold', family: "'Outfit', sans-serif" },
                    padding: { bottom: 20 }
                }
            }
        }
    });

    // Operation Distribution
    const opCtx = document.getElementById('operationChart').getContext('2d');
    if (state.charts.operation) state.charts.operation.destroy();

    const opData = stats.by_operation || {};
    state.charts.operation = new Chart(opCtx, {
        type: 'bar',
        data: {
            labels: Object.keys(opData),
            datasets: [{
                label: 'Count',
                data: Object.values(opData),
                backgroundColor: '#6366f1',
                borderRadius: 4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                title: {
                    display: true,
                    text: '操作类型分布',
                    color: '#111827',
                    font: { size: 16, weight: 'bold', family: "'Outfit', sans-serif" },
                    padding: { bottom: 20 }
                }
            },
            scales: {
                x: {
                    ticks: { color: '#6b7280' },
                    grid: { display: false }
                },
                y: {
                    ticks: { color: '#6b7280' },
                    grid: { color: '#f3f4f6' },
                    beginAtZero: true
                }
            }
        }
    });
}

function formatNumber(num) {
    if (num >= 1000000) return (num / 1000000).toFixed(2) + 'M';
    if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
    return num.toString();
}

// ========== SQL Statements View ==========
window.showStatements = function () {
    const modal = new bootstrap.Modal(document.getElementById('statementsModal'));
    loadStatements(0);
    modal.show();
};

async function loadStatements(page) {
    if (page < 0) return;

    try {
        const response = await fetch(`/api/v1/replay/statements?task_id=${state.currentTaskId}&offset=${page * 50}&limit=50`);
        const result = await response.json();

        if (result.code === 0) {
            state.currentStmtPage = page;
            document.getElementById('stmtPageInfo').textContent = `第 ${page + 1} 页`;
            displayStatements(result.data.statements);
        }
    } catch (error) {
        console.error('Load statements error:', error);
    }
}

function displayStatements(statements) {
    const container = document.getElementById('statementsList');
    container.innerHTML = '';

    statements.forEach(stmt => {
        const div = document.createElement('div');
        div.className = 'mb-3 p-3 bg-white border rounded-3 shadow-sm';
        div.innerHTML = `
            <div class="d-flex justify-content-between align-items-center mb-2">
                <small class="text-secondary">
                    ID: ${stmt.id} | TX: ${stmt.tx_id} | ${stmt.timestamp}
                </small>
                <span class="badge-custom badge-${stmt.sql_type.toLowerCase()}">${stmt.sql_type}</span>
            </div>
            <div class="sql-code">${escapeHtml(stmt.sql)}</div>
            <div class="mt-2 d-flex gap-3 text-sm">
                <small class="text-secondary">
                    操作: <span class="fw-medium text-primary">${stmt.operation}</span>
                </small>
                <small class="text-secondary">
                    影响行数: <span class="fw-medium text-success">${stmt.rows_affected}</span>
                </small>
                <small class="text-secondary">
                    状态: <span class="fw-medium text-${stmt.state === '00000' ? 'success' : 'danger'}">${stmt.state}</span>
                </small>
            </div>
        `;
        container.appendChild(div);
    });
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ========== Replay Control ==========
window.goToReplay = function () {
    if (!state.currentTaskId) {
        showToast('请先解析日志文件', 'error');
        return;
    }
    document.getElementById('replayTaskId').textContent = state.currentTaskId.substring(0, 8);
    showPage('replay');
};

window.startReplay = async function () {
    if (!state.currentTaskId) {
        showToast('请先选择任务', 'error');
        return;
    }

    const speedFactor = document.getElementById('speedFactor').value;
    const maxWorkers = document.getElementById('maxWorkers').value;

    const btn = document.getElementById('startReplayBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="loading-spinner me-2"></span> 启动中...';

    try {
        const params = new URLSearchParams({
            task_id: state.currentTaskId,
            speed_factor: speedFactor,
            max_workers: maxWorkers
        });

        const response = await fetch(`/api/v1/replay/run?${params}`, {
            method: 'POST'
        });

        const result = await response.json();

        if (result.code === 0) {
            showToast('回放已启动！', 'success');
            document.getElementById('stopReplayBtn').disabled = false;
            document.getElementById('replayStatus').textContent = '运行中';
            document.getElementById('replayStatus').className = 'badge-custom badge-running ms-auto';
            state.replayStartTime = Date.now();
            startProgressPolling();
        } else {
            showToast('启动失败: ' + result.msg, 'error');
            btn.disabled = false;
            btn.innerHTML = '<i class="bi bi-play-fill"></i> 开始回放';
        }
    } catch (error) {
        showToast('网络错误: ' + error.message, 'error');
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-play-fill"></i> 开始回放';
    }
};

window.stopReplay = async function () {
    try {
        const response = await fetch(`/api/v1/replay/stop?task_id=${state.currentTaskId}`, {
            method: 'POST'
        });

        const result = await response.json();

        if (result.code === 0) {
            showToast('回放已停止', 'info');
            stopProgressPolling();
            resetReplayUI();
        } else {
            showToast('停止失败: ' + result.msg, 'error');
        }
    } catch (error) {
        showToast('网络错误: ' + error.message, 'error');
    }
};

function startProgressPolling() {
    state.progressInterval = setInterval(updateProgress, 1000);
    updateElapsedTime();
}

function stopProgressPolling() {
    if (state.progressInterval) {
        clearInterval(state.progressInterval);
        state.progressInterval = null;
    }
}

async function updateProgress() {
    try {
        const response = await fetch(`/api/v1/replay/progress?task_id=${state.currentTaskId}`);
        const result = await response.json();

        if (result.code === 0) {
            const data = result.data;

            animateValue('totalStmts', parseInt(document.getElementById('totalStmts').getAttribute('data-value') || 0), data.total_statements || 0, 500);
            animateValue('executedStmts', parseInt(document.getElementById('executedStmts').getAttribute('data-value') || 0), data.executed_statements || 0, 500);
            animateValue('successStmts', parseInt(document.getElementById('successStmts').getAttribute('data-value') || 0), data.success_count || 0, 500);
            animateValue('failedStmts', parseInt(document.getElementById('failedStmts').getAttribute('data-value') || 0), data.failure_count || 0, 500);

            document.getElementById('divergenceStmts').textContent = formatNumber(0); // Assuming 0 for now as it wasn't in the original response clearly

            const percentage = data.percentage || 0;
            document.getElementById('progressPercent').textContent = percentage.toFixed(1) + '%';
            document.getElementById('progressBar').style.width = percentage + '%';

            if (!data.running && data.executed_statements > 0) {
                // Replay completed
                stopProgressPolling();
                document.getElementById('replayStatus').textContent = '已完成';
                document.getElementById('replayStatus').className = 'badge-custom badge-success ms-auto';
                showToast('回放完成！', 'success');
                resetReplayUI();
            }
        }
    } catch (error) {
        console.error('Progress update error:', error);
    }
}

function animateValue(id, start, end, duration) {
    if (start === end) return;
    const obj = document.getElementById(id);
    obj.setAttribute('data-value', end);
    let startTimestamp = null;
    const step = (timestamp) => {
        if (!startTimestamp) startTimestamp = timestamp;
        const progress = Math.min((timestamp - startTimestamp) / duration, 1);
        obj.innerHTML = formatNumber(Math.floor(progress * (end - start) + start));
        if (progress < 1) {
            window.requestAnimationFrame(step);
        }
    };
    window.requestAnimationFrame(step);
}

function updateElapsedTime() {
    const update = () => {
        if (!state.progressInterval) return;
        const elapsed = Math.floor((Date.now() - state.replayStartTime) / 1000);
        const hours = Math.floor(elapsed / 3600);
        const minutes = Math.floor((elapsed % 3600) / 60);
        const seconds = elapsed % 60;
        document.getElementById('elapsedTime').textContent =
            `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
        requestAnimationFrame(update);
    };
    requestAnimationFrame(update);
}

function resetReplayUI() {
    const btn = document.getElementById('startReplayBtn');
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-play-fill"></i> 开始回放';
    document.getElementById('stopReplayBtn').disabled = true;
}

// ========== Report Phase ==========
window.loadReport = async function () {
    if (!state.currentTaskId) {
        showToast('请先选择任务', 'error');
        return;
    }

    try {
        const [reportRes, taskRes] = await Promise.all([
            fetch(`/api/v1/replay/report?task_id=${state.currentTaskId}`),
            fetch(`/api/v1/replay/task?task_id=${state.currentTaskId}`)
        ]);

        const reportResult = await reportRes.json();
        const taskResult = await taskRes.json();

        if (reportResult.code === 0 && taskResult.code === 0) {
            displayReport(reportResult.data, taskResult.data);
        } else {
            showToast('加载报告失败: ' + (reportResult.msg || taskResult.msg), 'error');
        }
    } catch (error) {
        showToast('网络错误: ' + error.message, 'error');
    }
};

function displayReport(report, taskData) {
    const stats = taskData.statistics || {};
    const origStmts = taskData.task.total_statements || 0;
    const origTx = taskData.task.total_tx || 0;
    const origSession = stats.session_count || 0;
    const origSingleTx = stats.single_stmt_tx || 0;
    const origMultiTx = stats.multi_stmt_tx || 0;

    const repStmts = report.executed_stmts || 0;
    const repTx = report.total_tx || 0;
    const repSession = report.session_count || 0;
    const repSingleTx = report.single_stmt_tx || 0;
    const repMultiTx = report.multi_stmt_tx || 0;

    const tbody = document.getElementById('reportStatsBody');
    tbody.innerHTML = `
        <tr>
            <td class="fw-medium">Statements</td>
            <td>${formatNumber(origStmts)}</td>
            <td>${formatNumber(repStmts)}</td>
        </tr>
        <tr>
            <td class="fw-medium">Sessions</td>
            <td>${origSession}</td>
            <td>${repSession}</td>
        </tr>
        <tr>
            <td class="fw-medium">Transactions</td>
            <td>${formatNumber(origTx)}</td>
            <td>${formatNumber(repTx)}</td>
        </tr>
        <tr>
            <td class="fw-medium">Single-stmt TX</td>
            <td>${formatNumber(origSingleTx)}</td>
            <td>${formatNumber(repSingleTx)}</td>
        </tr>
        <tr>
            <td class="fw-medium">Multi-stmt TX</td>
            <td>${formatNumber(origMultiTx)}</td>
            <td>${formatNumber(repMultiTx)}</td>
        </tr>
    `;

    const stmtRatio = origStmts > 0 ? (repStmts / origStmts).toFixed(4) : '-';
    const txRatio = origTx > 0 ? (repTx / origTx).toFixed(4) : '-';

    let similarity = 0;
    if (origTx > 0) {
        const matchCount = Math.min(origSingleTx, repSingleTx) + Math.min(origMultiTx, repMultiTx);
        similarity = (matchCount / origTx) * 100;
    }

    document.getElementById('stmtRatio').textContent = stmtRatio;
    document.getElementById('txRatio').textContent = txRatio;
    document.getElementById('txSim').textContent = similarity.toFixed(2) + '%';

    document.getElementById('reportDivergence').textContent = formatNumber(report.divergence_count || 0);
    document.getElementById('reportRowsDiff').textContent = formatNumber(report.rows_affected_diff || 0);
    document.getElementById('reportErrorDiff').textContent = formatNumber(report.error_state_diff || 0);
    document.getElementById('reportDivergenceRate').textContent = (report.divergence_rate || 0).toFixed(4) + '%';

    const divList = document.getElementById('divergenceList');
    if (report.divergences && report.divergences.length > 0) {
        divList.innerHTML = '';
        report.divergences.slice(0, 10).forEach(div => {
            const item = document.createElement('div');
            item.className = 'divergence-item p-3 mb-2 bg-white border rounded-3';
            item.innerHTML = renderDivergenceItem(div);
            divList.appendChild(item);
        });
    } else {
        divList.innerHTML = '<p class="text-secondary text-center">暂无差异记录</p>';
    }

    const errList = document.getElementById('errorList');
    if (report.errors && report.errors.length > 0) {
        errList.innerHTML = '';
        report.errors.slice(0, 10).forEach(err => {
            const item = document.createElement('div');
            item.className = 'error-item p-3 mb-2 bg-red-50 border border-red-100 rounded-3';
            item.innerHTML = `
                <div class="error-message text-danger font-monospace text-sm">${escapeHtml(err.error || 'Unknown error')}</div>
                ${err.sql ? `<div class="sql-code mt-2 p-2 bg-white border rounded text-xs text-secondary">${escapeHtml(err.sql.substring(0, 200))}...</div>` : ''}
            `;
            errList.appendChild(item);
        });
    } else {
        errList.innerHTML = '<p class="text-secondary text-center">暂无错误记录</p>';
    }
}

function renderDivergenceItem(div) {
    return `
        <div class="divergence-type d-flex justify-content-between align-items-center">
            <span class="badge-custom ${div.divergence_type === 'rows_affected' ? 'badge-write' : 'badge-failed'}">
                ${div.divergence_type === 'rows_affected' ? '影响行数差异' : '错误状态差异'}
            </span>
            <small class="text-secondary text-xs">
                TxID: ${div.tx_id} | VxID: ${div.vxid}
            </small>
        </div>
        <div class="divergence-detail mt-2 text-sm">
            ${div.divergence_type === 'rows_affected'
            ? `<div class="d-flex justify-content-between"><span>原始: ${div.original_rows_affected} 行</span> <span>回放: ${div.replay_rows_affected} 行</span></div>`
            : `<div class="d-flex justify-content-between"><span>原始: ${div.original_state}</span> <span>回放: ${div.replay_state}</span></div>`
        }
            ${div.replay_error ? `<div class="mt-1 text-danger small">${escapeHtml(div.replay_error)}</div>` : ''}
        </div>
        <div class="sql-code mt-2 p-2 bg-light border rounded text-xs text-secondary font-monospace" style="overflow:hidden; text-overflow:ellipsis;">${escapeHtml(div.sql.substring(0, 100))}${div.sql.length > 100 ? '...' : ''}</div>
    `;
}

// ========== Divergences List ==========
window.showDivergences = function () {
    const modal = new bootstrap.Modal(document.getElementById('divergencesModal'));
    loadDivergences(0);
    modal.show();
};

async function loadDivergences(page) {
    if (page < 0) return;

    try {
        const response = await fetch(`/api/v1/replay/divergences?task_id=${state.currentTaskId}&offset=${page * 50}&limit=50`);
        const result = await response.json();

        if (result.code === 0) {
            state.currentDivPage = page;
            document.getElementById('divPageInfo').textContent = `第 ${page + 1} 页`;
            displayDivergencesFull(result.data.divergences);
        }
    } catch (error) {
        console.error('Load divergences error:', error);
    }
}

function displayDivergencesFull(divergences) {
    const container = document.getElementById('divergencesListFull');
    container.innerHTML = '';

    if (!divergences || divergences.length === 0) {
        container.innerHTML = '<p class="text-center text-secondary my-4">暂无数据</p>';
        return;
    }

    divergences.forEach(div => {
        const divEl = document.createElement('div');
        divEl.className = 'divergence-item mb-3 p-3 bg-white border rounded-3';
        divEl.innerHTML = `
            <div class="d-flex justify-content-between align-items-center mb-2">
                <span class="badge-custom ${div.divergence_type === 'rows_affected' ? 'badge-write' : 'badge-failed'}">
                    ${div.divergence_type === 'rows_affected' ? '影响行数差异' : '错误状态差异'}
                </span>
                <small class="text-secondary">
                    ID: ${div.id} | Session: ${div.session_id} | VxID: ${div.vxid}
                </small>
            </div>
            <div class="sql-code mb-2 p-2 bg-light border rounded font-monospace">${escapeHtml(div.sql)}</div>
            <div class="row g-2 text-sm">
                <div class="col-6">
                    <div class="p-2 border rounded bg-light h-100">
                        <strong class="text-secondary mb-1 d-block">原始执行:</strong>
                        ${div.divergence_type === 'rows_affected'
                ? `Rows: ${div.original_rows_affected}`
                : `State: ${div.original_state}`}
                    </div>
                </div>
                <div class="col-6">
                    <div class="p-2 border rounded bg-light h-100">
                        <strong class="text-secondary mb-1 d-block">回放执行:</strong>
                        ${div.divergence_type === 'rows_affected'
                ? `Rows: ${div.replay_rows_affected}`
                : `State: ${div.replay_state}`}
                        ${div.replay_error ? `<br><span class="text-danger small">${escapeHtml(div.replay_error)}</span>` : ''}
                    </div>
                </div>
            </div>
        `;
        container.appendChild(divEl);
    });
}

// ========== Server Status ==========
function initServerStatus() {
    // Already defined checkServerStatus below
}

async function checkServerStatus() {
    const indicator = document.getElementById('serverStatus');
    const text = document.getElementById('serverStatusText');

    try {
        const response = await fetch('/health');
        if (response.ok) {
            indicator.classList.remove('bg-danger');
            indicator.classList.add('bg-success', 'active'); // Add active for pulse
            text.textContent = 'API 服务正常';
        } else {
            throw new Error('Server error');
        }
    } catch (error) {
        indicator.classList.remove('bg-success', 'active');
        indicator.classList.add('bg-danger');
        text.textContent = 'API 服务离线';
    }
}
