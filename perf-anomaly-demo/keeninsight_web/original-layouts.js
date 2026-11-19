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
                                    <div class="knob-list" style="gap: 8px;" role="region" aria-label="Tuning actions" tabindex="0">
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
};
