'use strict';
// Original English.zip detail layout. Only live, escaped values are interpolated.
window.KeenInsightLayouts = {
  alert(v) { return `
                <!-- Top section - 1 and 2 form a whole -->
                <div class="top-section">
                    <!-- 1. Trigger Status -->
                    <div class="detail-section top-layout">
                        <div class="detail-title">
                            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                <circle cx="24" cy="24" r="16"/>
                                <path d="M16 22h16M16 26h12M16 30h16"/>
                            </svg>
                            Trigger Status
                        </div>
                        <div class="detail-card">
                            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                                ${v.rules}
                            </div>
                        </div>
                    </div>

                </div>

                <!-- Middle section - two-column layout -->
                <div class="middle-section">
                    <!-- Left: 3 and 4 form a whole, 3 on top 4 below -->
                    <div class="detail-section">
                        <div class="detail-title">
                            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                <path d="M24 8v32M8 24h32"/>
                                <rect x="14" y="14" width="20" height="20" rx="2" fill="none" stroke="currentColor" stroke-width="3"/>
                                <path d="M14 24h20M24 14v20"/>
                            </svg>
                            Differential Profiling Analysis
                        </div>
                        <div class="detail-card scrollable">
                            <div class="differential-profiling-container">
                                ${v.profiling}
                            </div>
                        </div>
                        <div class="detail-title" style="border-top: 1px solid #e5e6eb;">
                            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                <path d="M24 8v32M8 24h32"/>
                                <circle cx="24" cy="24" r="4"/>
                            </svg>
                            SHAP Model Analysis Results
                        </div>
                        <div class="detail-card scrollable shap-analysis">
                            ${v.shap}
                        </div>
                    </div>

                    <!-- Right: 5 and 6 form a whole, 5 on top 6 below -->
                    <div class="detail-section">
                        <div class="detail-title">
                            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                <circle cx="24" cy="24" r="16"/>
                                <path d="M18 20l6 4 6-4M18 28l6 4 6-4"/>
                            </svg>
                            Function Analysis
                        </div>
                        <div class="detail-card scrollable">
                            <div class="function-list">
                                ${v.functions}
                            </div>
                        </div>
                        <div class="detail-title detail-title-with-button" style="border-top: 1px solid #e5e6eb;">
                            <div style="display: flex; align-items: center; gap: 10px;">
                                <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                    <rect x="10" y="10" width="28" height="28" rx="2"/>
                                    <path d="M16 20h16M16 28h16"/>
                                </svg>
                                Root-Cause Explaination
                            </div>
                            <button class="kb-button" data-ki-knowledge>Related Knowledge Base</button>
                        </div>
                        <div class="detail-card scrollable explanation-analysis">
                            <div style="padding: 16px; color: #282f38; line-height: 1.8; font-size: 14px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;">${v.explanation}</div>
                        </div>
                    </div>
                </div>

                <!-- Bottom section - Tuning Results (full width, compact layout) -->
                <div class="bottom-section">
                    <div class="detail-section top-layout">
                        <div class="detail-title">
                            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                <path d="M12 24h24M24 12v24"/>
                                <rect x="6" y="6" width="36" height="36" rx="4"/>
                            </svg>
                            Tuning Results
                        </div>
                        <div class="detail-card">
                            <div class="tuning-result-compact">
                                <!-- Knobs to Tune -->
                                <div class="tuning-section green-background">
                                    <div class="tuning-section-title">Tuning Actions</div>
                                    <div class="knob-list" style="gap: 8px;">
                                        ${v.knobs}
                                    </div>
                                </div>

                                <!-- Before Tuning -->
                                <div class="tuning-section">
                                    <div class="tuning-section-title">Before Tuning</div>
                                    <div class="tuning-metrics">
                                        <div class="tuning-metric-row">
                                            <span class="tuning-metric-name">Query time</span>
                                            <span class="tuning-metric-value highlight">${v.beforeTuning_latency}</span>
                                        </div>
                                        <div class="tuning-metric-row">
                                            <span class="tuning-metric-name">Restored time</span>
                                            <span class="tuning-metric-value highlight">${v.beforeTuning_restored}</span>
                                        </div>
                                        <div class="tuning-metric-row">
                                            <span class="tuning-metric-name">State</span>
                                            <span class="tuning-metric-value highlight">${v.beforeTuning_state}</span>
                                        </div>
                                    </div>
                                </div>

                                <!-- After Tuning -->
                                <div class="tuning-section">
                                    <div class="tuning-section-title">After Tuning</div>
                                    <div class="tuning-metrics">
                                        <div class="tuning-metric-row">
                                            <span class="tuning-metric-name">Query time</span>
                                            <span class="tuning-metric-value success">${v.afterTuning_latency}</span>
                                        </div>
                                        <div class="tuning-metric-row">
                                            <span class="tuning-metric-name">Restored time</span>
                                            <span class="tuning-metric-value success">${v.afterTuning_restored}</span>
                                        </div>
                                        <div class="tuning-metric-row">
                                            <span class="tuning-metric-name">State</span>
                                            <span class="tuning-metric-value success">${v.afterTuning_state}</span>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
`; },
  sql(v) { return `
                <!-- 顶部区域 - 1和2组成一个整体 -->
                <div class="top-section">
                    <!-- 1. SQL基本信息 -->
                    <div class="detail-section top-layout">
                        <div class="detail-title">
                            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                <circle cx="24" cy="24" r="16"/>
                                <path d="M16 22h16M16 26h12M16 30h16"/>
                            </svg>
                            SQL Basic Information
                        </div>
                        <div class="detail-card" style="padding: 12px 20px;">
                            <div style="display: flex; gap: 20px; align-items: center;">
                                <div style="width: 50%; min-width: 0;">
                                    <div style="font-size: 11px; color: #86909c; margin-bottom: 4px;">SQL Statement</div>
                                    <div class="sql-statement">${v.sql}</div>
                                </div>
                                <div style="display: flex; gap: 24px; flex-shrink: 0;">
                                    <div class="info-item" style="align-items: flex-start; text-align: left;">
                                        <div class="info-label">Execution Time</div>
                                        <div class="info-value" style="font-size: 14px;">${v.time}</div>
                                    </div>
                                    <div class="info-item" style="align-items: flex-start; text-align: left;">
                                        <div class="info-label">SQL Type</div>
                                        <div class="info-value" style="font-size: 14px;">${v.type}</div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- 中间区域 - 两栏布局 -->
                <div class="middle-section">
                    <!-- 左边：异常根因、规则匹配、预测结果 -->
                    <div class="detail-section">
                        <div class="detail-title">
                            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                <circle cx="24" cy="24" r="16"/>
                                <path d="M16 22h16M16 26h12M16 30h16"/>
                            </svg>
                            Root Causes
                        </div>
                        <div class="detail-card" style="padding: 16px;">
                            <div style="display: flex; align-items: center; gap: 12px; padding: 12px; background: #fff7e6; border-radius: 6px; border: 1px solid #ffcc80;">
                                <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3" style="width: 32px; height: 32px; color: #ff9800; flex-shrink: 0;">
                                    <circle cx="24" cy="24" r="16"/>
                                    <path d="M20 16l4 20M28 16l-4 20"/>
                                    <circle cx="24" cy="36" r="2" fill="currentColor"/>
                                </svg>
                                <div>
                                    <div style="font-size: 14px; font-weight: 600; color: #e65100;">${v.causes}</div>
                                    <div style="font-size: 13px; color: #6d4c41;">${v.diagnosis}</div>
                                </div>
                            </div>
                        </div>
                        <div class="detail-title" style="border-top: 1px solid #e5e6eb;">
                            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                <rect x="8" y="12" width="32" height="24" rx="2"/>
                                <path d="M12 20h8M24 20h12M12 28h12M28 28h8"/>
                            </svg>
                            Matched Diagnosis Rules
                        </div>
                        <div class="detail-card scrollable">
                            ${v.rules}
                        </div>
                        <div class="detail-title" style="border-top: 1px solid #e5e6eb;">
                            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                <path d="M24 8v32M8 24h32"/>
                                <circle cx="24" cy="24" r="4"/>
                            </svg>
                            Multiclass scoring model
                        </div>
                        <div class="detail-card scrollable">
                            <div class="root-causes-list">
                                ${v.scores}
                            </div>
                        </div>
                    </div>

                    <!-- 右边：SQL生命周期和调优建议 -->
                    <div class="detail-section">
                        <div class="detail-title">
                            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                <path d="M8 32l12-12 8 8 12-14"/>
                                <path d="M8 24h4M8 16h4M8 8h4"/>
                            </svg>
                            SQL Lifecycle
                        </div>
                        <div class="detail-card" style="position: relative; padding: 16px;">
                            <div style="display: flex; justify-content: flex-end; margin-bottom: 8px;">
                                <button data-ki-zoom style="padding: 4px 8px; background: var(--primary-color); color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px;">
                                    Zoom In
                                </button>
                            </div>
                            <div id="flameGraphContainer" data-ki-zoom style="cursor: pointer; overflow-x: auto;">
                                ${v.flame}
                            </div>
                        </div>
                        <div class="detail-title detail-title-with-button" style="border-top: 1px solid #e5e6eb;">
                            <div style="display: flex; align-items: center; gap: 10px;">
                                <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                    <path d="M12 24h24M24 12v24"/>
                                    <rect x="6" y="6" width="36" height="36" rx="4"/>
                                </svg>
                                Root Cause Explaination
                            </div>
                            <button class="kb-button" data-ki-knowledge>Related Knowledge Base</button>
                        </div>
                        <div class="detail-card scrollable">
                            ${v.explanation}
                        </div>
                    </div>
                </div>

                <!-- 底部区域 - 调优结果（全宽） -->
                <div class="bottom-section">
                    <div class="detail-section top-layout">
                        <div class="detail-title">
                            <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3">
                                <path d="M12 24h24M24 12v24"/>
                                <rect x="6" y="6" width="36" height="36" rx="4"/>
                            </svg>
                            Tuning Results
                        </div>
                        <div class="detail-card" style="padding: 12px 20px;">
                            <div style="display: flex; gap: 24px; align-items: stretch;">
                                <div style="flex: 0 0 55%; min-width: 0;">
                                    <div style="font-size: 11px; color: #86909c; margin-bottom: 4px;">Optimized SQL</div>
                                    <div class="sql-statement" style="overflow-x: auto; white-space: pre-wrap !important;">${v.rewrite}</div>
                                </div>
                                <div style="flex: 1; display: flex; gap: 16px; align-items: center; justify-content: center;">
                                    <div style="text-align: center;">
                                        <div style="font-size: 16px; color: #86909c; margin-bottom: 6px; font-weight: 500;">Before Tuning</div>
                                        <div style="font-size: 24px; font-weight: 700; color: var(--danger-color);">${v.before}</div>
                                    </div>
                                    <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="3" style="width: 24px; height: 24px; color: var(--success-color); flex-shrink: 0;">
                                        <path d="M10 24h28M30 14l10 10-10 10"/>
                                    </svg>
                                    <div style="text-align: center;">
                                        <div style="font-size: 16px; color: #86909c; margin-bottom: 6px; font-weight: 500;">After Tuning</div>
                                        <div style="font-size: 24px; font-weight: 700; color: var(--success-color);">${v.after}</div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
`; },
};
