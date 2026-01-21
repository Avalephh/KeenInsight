<template>
  <div class="page-container">
    <div class="page-title">多轮参数调优结果</div>

    <!-- 轮次选择 -->
    <div class="form-section">
      <div class="section-title">轮次选择与对比</div>
      <el-form :model="paramForm" inline class="round-form">
        <el-form-item label="当前轮次">
          <el-select v-model="paramForm.activeRound" placeholder="请选择调优轮次" clearable @change="switchRound"
            class="round-select">
            <el-option v-for="item in paramRounds" :key="item.round" :label="`第${item.round}轮调优`" :value="item.round">
              <div class="round-option">
                <span class="round-label">第{{ item.round }}轮</span>
                <div class="round-stats">
                  <span class="stat-item">TPS: {{ item.tps }}</span>
                  <span class="stat-item">延迟: {{ item.latency }}ms</span>
                </div>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
      </el-form>

      <!-- 当前轮次指标概览 -->
      <div v-if="paramForm.activeRound" class="round-summary">
        <div class="summary-cards">
          <div class="summary-card">
            <div class="card-icon">
              <i class="el-icon-s-data"></i>
            </div>
            <div class="card-content">
              <div class="card-title">TPS</div>
              <div class="card-value highlight">{{ currentRoundStats.tps }}</div>
              <div class="card-change" :class="currentRoundStats.tpsChange >= 0 ? 'positive' : 'negative'">
                {{ currentRoundStats.tpsChange >= 0 ? '+' : '' }}{{ currentRoundStats.tpsChange }}%
              </div>
            </div>
          </div>
          <div class="summary-card">
            <div class="card-icon">
              <i class="el-icon-timer"></i>
            </div>
            <div class="card-content">
              <div class="card-title">延迟</div>
              <div class="card-value">{{ currentRoundStats.latency }}ms</div>
              <div class="card-change" :class="currentRoundStats.latencyChange <= 0 ? 'positive' : 'negative'">
                {{ currentRoundStats.latencyChange <= 0 ? '+' : '-' }}{{ Math.abs(currentRoundStats.latencyChange) }}%
                  </div>
              </div>
            </div>
            <div class="summary-card">
              <div class="card-icon">
                <i class="el-icon-setting"></i>
              </div>
              <div class="card-content">
                <div class="card-title">参数调整</div>
                <div class="card-value">{{ currentRoundParams.length }}</div>
                <div class="card-subtitle">个参数</div>
              </div>
            </div>
            <div class="summary-card">
              <div class="card-icon">
                <i class="el-icon-trend-charts"></i>
              </div>
              <div class="card-content">
                <div class="card-title">整体提升</div>
                <div class="card-value positive">+{{ currentRoundStats.improvement }}%</div>
                <div class="card-subtitle">性能提升</div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 当前轮次参数表格 -->
      <div class="form-section">
        <div class="section-title">
          当前轮次参数推荐
          <span v-if="paramForm.activeRound" class="round-indicator">第{{ paramForm.activeRound }}轮</span>
        </div>
        <el-table :data="currentRoundParams" border class="custom-table param-table"
          header-cell-class-name="table-header-bold">
          <el-table-column prop="paramName" label="参数名" width="300">
            <template #default="{ row }">
              <div class="param-name-cell">
                <span class="param-name">{{ row.paramName }}</span>
                <!-- <div class="param-tag" :class="row.riskLevel">{{ row.riskLevel }}</div> -->
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="originalValue" label="原始值" width="180">
            <template #default="{ row }">
              <span class="original-value">{{ row.originalValue }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="currentValue" label="推荐值" width="180">
            <template #default="{ row }">
              <span class="current-value highlight">{{ row.currentValue }}</span>
            </template>
          </el-table-column>
          <el-table-column label="变化方向" width="150" align="center">
            <template #default="{ row }">
              <div class="change-direction">
                <i :class="row.changeIcon" :style="{ color: row.changeColor }"></i>
                <el-tag :class="row.changeType === '提升'
                  ? 'tag-increase'
                  : row.changeType === '降低'
                    ? 'tag-decrease'
                    : 'tag-neutral'" size="small">
                  {{ row.changeType }}
                </el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="变化幅度" width="150">
            <template #default="{ row }">
              <span class="change-percent">{{ row.changePercent }}%</span>
            </template>
          </el-table-column>
        </el-table>

        <!-- 参数统计 -->
        <div v-if="paramForm.activeRound" class="param-stats">
          <div class="stats-item">
            <span class="stats-label">总计:</span>
            <span class="stats-value">{{ currentRoundParams.length }} 个参数</span>
          </div>
          <div class="stats-item">
            <span class="stats-label">提升:</span>
            <span class="stats-value positive">{{ increaseCount }} 个</span>
          </div>
          <div class="stats-item">
            <span class="stats-label">降低:</span>
            <span class="stats-value danger">{{ decreaseCount }} 个</span>
          </div>
          <div class="stats-item">
            <span class="stats-label">高风险:</span>
            <span class="stats-value warning">{{ highRiskCount }} 个</span>
          </div>
        </div>
      </div>
      <!-- 参数变更总结 -->
      <div class="form-section" v-if="paramForm.activeRound">
        <div class="section-title">参数变更总结</div>
        <div class="summary-content">
          <p>第{{ paramForm.activeRound }}轮调优共调整 <strong>{{ currentRoundParams.length }}</strong> 个参数，其中：</p>
          <ul class="change-list">
            <li><strong>{{ increaseCount }}</strong> 个参数被提升（主要涉及性能优化）</li>
            <li><strong>{{ decreaseCount }}</strong> 个参数被降低（主要涉及资源限制）</li>
            <li><strong>{{ highRiskCount }}</strong> 个参数属于高风险调整，需要特别注意</li>
          </ul>
          <p class="summary-note">
            <i class="el-icon-warning warning-icon"></i>
            预计整体性能提升 <strong class="positive">+{{ currentRoundStats.improvement }}%</strong>，
            建议在生产环境应用前进行充分测试。
          </p>
        </div>
      </div>

      <!-- 操作按钮 -->
      <div class="btn-group">
        <el-button @click="toExceptionAnalysis" class="secondary-btn-lg">
          <i class="el-icon-back"></i>
          返回上一步
        </el-button>
        <!-- <el-button type="warning" @click="resetParams" class="warning-btn-lg" :disabled="!paramForm.activeRound">
          <i class="el-icon-refresh"></i>
          重置为原始参数
        </el-button>
        <el-button type="primary" size="large" @click="applyParams" class="primary-btn-lg"
          :disabled="!paramForm.activeRound">
          <i class="el-icon-check"></i>
          应用当前轮次参数
        </el-button>
        <el-button type="success" size="large" @click="toLoadSelect" class="success-btn-lg">
          <i class="el-icon-finished"></i>
          完成调优，重新挖掘规则
        </el-button> -->
      </div>
    </div>
</template>

<script setup lang="ts">
import { reactive, ref, onMounted, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { tuneAPI } from '@/api/tune'
import type {
  TuneRoundSummary,
  TuneParamItem
} from '@/api/tune'

const router = useRouter()

/* ---------------- 基础状态 ---------------- */
const route = useRoute()
const taskName = computed(() => route.query.taskName as string || '')

const paramRounds = ref<TuneRoundSummary[]>([])
const currentRoundParams = ref<TuneParamItem[]>([])
const roundParamMap = ref<Record<string, TuneParamItem[]>>({})

const paramForm = reactive({
  activeRound: 1,
  compareRound: [] as number[]
})

/* ---------------- 关键修复：轮次统计计算 ---------------- */

/**
 * 当前轮次统计（模板大量使用）
 * 永远返回一个完整对象，避免 undefined
 */
const currentRoundStats = computed(() => {
  const round = paramRounds.value.find(
    r => r.round === paramForm.activeRound
  )

  if (!round) {
    return {
      tps: 0,
      latency: 0,
      improvement: 0,
      tpsChange: 0,
      latencyChange: 0
    }
  }

  // 第一轮作为 baseline
  const base = paramRounds.value[0]

  const tpsChange = base
    ? Number((((round.tps - base.tps) / base.tps) * 100).toFixed(1))
    : 0

  const latencyChange = base
    ? Number((((round.latency - base.latency) / base.latency) * 100).toFixed(1))
    : 0

  return {
    tps: round.tps,
    latency: round.latency,
    improvement: round.improvement ?? tpsChange,
    tpsChange,
    latencyChange
  }
})

/* ---------------- 参数统计（模板直接用） ---------------- */

const increaseCount = computed(() =>
  currentRoundParams.value.filter(p => p.changeType === '提升').length
)

const decreaseCount = computed(() =>
  currentRoundParams.value.filter(p => p.changeType === '降低').length
)

const highRiskCount = computed(() =>
  currentRoundParams.value.filter(p => p.riskLevel === 'high').length
)

/* ---------------- 数据加载 ---------------- */

const loadMultiStepResult = async () => {
  try {
    const res = await tuneAPI.getMultiStepResult(taskName.value)

    if (res.code !== 200) {
      ElMessage.error(res.msg || '获取多轮调优结果失败')
      return
    }

    paramRounds.value = res.data.rounds
    roundParamMap.value = res.data.roundParams

    if (paramRounds.value.length > 0) {
      paramForm.activeRound = paramRounds.value.length
      currentRoundParams.value =
        roundParamMap.value[String(paramForm.activeRound)] || []
    }
  } catch (e) {
    console.error(e)
    ElMessage.error('加载多轮调优结果失败')
  }
}

/* ---------------- 轮次切换 ---------------- */

const switchRound = () => {
  currentRoundParams.value =
    roundParamMap.value[String(paramForm.activeRound)] || []
}

/* ---------------- 页面跳转 ---------------- */

const toExceptionAnalysis = () => {
  router.push({ name: 'knob-recommend' })
}

const toLoadSelect = () => {
  ElMessageBox.confirm(
    '确定完成调优并重新挖掘规则吗？系统将保存当前调优结果。',
    '完成调优确认',
    {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'success'
    }
  ).then(() => {
    router.push({ name: 'load-select' })
  })
}

/* ---------------- 生命周期 ---------------- */

onMounted(() => {
  loadMultiStepResult()
})
</script>


<style scoped>
/* 整体样式 - 与home.html一致 */

body {
  margin: 0;
  font-family: "Segoe UI", Roboto, Arial, sans-serif;
  background: #f4f6f9;
  color: #333;
}

.table-header-bold {
  font-weight: 600;
  font-size: 14px;
  color: #333;
}

.page-container {
  background: #fff;
  border-radius: 10px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
  padding: 26px 34px;
  max-width: 1000px;
  margin: 20px auto;
  min-height: auto;
}

/* 页面标题样式 */
.page-title {
  font-size: 22px;
  font-weight: 600;
  color: #4a00e0;
  margin-bottom: 20px;
  padding-bottom: 10px;
  border-bottom: 2px solid #f4f6f9;
}

/* 表单区域样式 */
.form-section {
  background: #fff;
  border-radius: 10px;
  padding: 20px 24px;
  margin-bottom: 24px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
  border-left: 4px solid #4a00e0;
}

.section-title {
  font-size: 18px;
  font-weight: 500;
  color: #333;
  margin-bottom: 15px;
  padding-left: 10px;
  border-left: 4px solid #4a00e0;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.round-indicator {
  background: #4a00e0;
  color: #fff;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: normal;
}

.compare-count {
  background: #f0ad4e;
  color: #fff;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: normal;
}

/* 提升：绿色 */
:deep(.el-tag.tag-increase) {
  background-color: #e9f5e9;
  color: #5cb85c;
  border: none;
}

/* 降低：蓝色 */
:deep(.el-tag.tag-decrease) {
  background-color: #e6f0ff;
  color: #409eff;
  border: none;
}

/* 不变：灰色 */
:deep(.el-tag.tag-neutral) {
  background-color: #e9ecef;
  color: #666;
  border: none;
}

/* 轮次选择表单 */
.round-form {
  display: flex;
  align-items: center;
  gap: 20px;
}

.round-select,
.compare-select {
  width: 200px;
}

.round-select :deep(.el-input__inner),
.compare-select :deep(.el-input__inner) {
  border-radius: 6px;
  border: 1px solid #ddd;
  height: 36px;
  line-height: 36px;
}

.round-option {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  padding: 5px 0;
}

.round-label {
  font-weight: 500;
}

.round-stats {
  display: flex;
  flex-direction: column;
  font-size: 11px;
  color: #666;
}

.stat-item {
  line-height: 1.4;
}

/* 轮次指标概览 */
.round-summary {
  margin-top: 20px;
}

.summary-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 15px;
}

.summary-card {
  background: #f8f9fa;
  border-radius: 8px;
  padding: 15px;
  display: flex;
  align-items: center;
  border: 1px solid #eee;
  transition: all 0.3s;
}

.summary-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
}

.card-icon {
  width: 40px;
  height: 40px;
  background: #4a00e0;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-right: 15px;
}

.card-icon i {
  color: #fff;
  font-size: 20px;
}

.card-content {
  flex: 1;
}

.card-title {
  font-size: 12px;
  color: #666;
  margin-bottom: 4px;
}

.card-value {
  font-size: 18px;
  font-weight: 600;
  color: #333;
}

.highlight {
  color: #4a00e0;
}

.card-change {
  font-size: 11px;
  margin-top: 2px;
}

.positive {
  color: #5cb85c;
}

.negative {
  color: #d9534f;
}

.card-subtitle {
  font-size: 11px;
  color: #999;
  margin-top: 2px;
}

/* 参数表格 */
.param-table {
  margin-top: 10px;
}

.param-name-cell {
  display: flex;
  flex-direction: column;
}

.param-name {
  font-weight: 500;
  color: #333;
  margin-bottom: 4px;
}

.param-tag {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 10px;
  display: inline-block;
  width: fit-content;
}

.param-tag.low {
  background: #e9f5e9;
  color: #5cb85c;
}

.param-tag.medium {
  background: #fff3cd;
  color: #f0ad4e;
}

.param-tag.high {
  background: #f8d7da;
  color: #d9534f;
}

.original-value {
  color: #666;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
}

.current-value {
  font-weight: 500;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
}

.change-direction {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
}

.change-direction i {
  font-size: 16px;
}

:deep(.el-tag.success) {
  background: #e9f5e9;
  color: #5cb85c;
  border: none;
}

:deep(.el-tag.danger) {
  background: #f8d7da;
  color: #d9534f;
  border: none;
}

:deep(.el-tag.info) {
  background: #e9ecef;
  color: #666;
  border: none;
}

.param-desc {
  color: #666;
  font-size: 13px;
  line-height: 1.4;
}

.param-note {
  font-size: 11px;
  color: #999;
  margin-top: 4px;
  font-style: italic;
}

.detail-btn {
  color: #4a00e0;
  font-weight: 500;
}

/* 参数统计 */
.param-stats {
  display: flex;
  gap: 20px;
  margin-top: 20px;
  padding: 15px;
  background: #f8f9fa;
  border-radius: 8px;
  border: 1px solid #eee;
}

.stats-item {
  display: flex;
  align-items: center;
  gap: 5px;
}

.stats-label {
  color: #666;
  font-size: 13px;
}

.stats-value {
  font-weight: 500;
  font-size: 14px;
}

/* 图表容器 */
.chart-container {
  margin-top: 15px;
}

.chart-box {
  width: 100%;
  height: 350px;
  border-radius: 8px;
  border: 1px solid #eee;
}

/* 参数变更总结 */
.summary-content {
  background: #f8f9fa;
  padding: 20px;
  border-radius: 8px;
  border: 1px solid #eee;
}

.summary-content p {
  margin: 10px 0;
  line-height: 1.6;
}

.change-list {
  margin: 15px 0 15px 20px;
}

.change-list li {
  margin: 8px 0;
  color: #666;
}

.summary-note {
  padding: 12px;
  background: #fff3cd;
  border-radius: 6px;
  border-left: 4px solid #f0ad4e;
  margin-top: 20px !important;
}

.warning-icon {
  color: #f0ad4e;
  margin-right: 8px;
}

/* 按钮样式 */
.btn-group {
  margin-top: 30px;
  text-align: center;
  display: flex;
  justify-content: center;
  gap: 15px;
}

.primary-btn-lg,
.success-btn-lg,
.warning-btn-lg,
.secondary-btn-lg {
  padding: 12px 24px;
  font-size: 16px;
  border-radius: 6px;
  border: none;
  cursor: pointer;
  transition: all 0.3s;
  display: flex;
  align-items: center;
  gap: 8px;
}

.primary-btn-lg {
  background: #4a00e0;
  color: #fff;
}

.primary-btn-lg:hover {
  background: #3a00b3;
  transform: translateY(-2px);
  box-shadow: 0 4px 8px rgba(58, 0, 179, 0.2);
}

.success-btn-lg {
  background: #5cb85c;
  color: #fff;
}

.success-btn-lg:hover {
  background: #4cae4c;
  transform: translateY(-2px);
  box-shadow: 0 4px 8px rgba(92, 184, 92, 0.2);
}

.warning-btn-lg {
  background: #f0ad4e;
  color: #fff;
}

.warning-btn-lg:hover {
  background: #ec971f;
  transform: translateY(-2px);
  box-shadow: 0 4px 8px rgba(240, 173, 78, 0.2);
}

.secondary-btn-lg {
  background: #f5f7fb;
  color: #666;
  border: 1px solid #ddd;
}

.secondary-btn-lg:hover {
  background: #e9ecef;
  border-color: #ccc;
}

.secondary-btn {
  background: #f5f7fb;
  color: #666;
  border: 1px solid #ddd;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
}

.secondary-btn:hover {
  background: #e9ecef;
  border-color: #ccc;
}

/* 禁用状态按钮 */
:deep(.el-button.is-disabled) {
  background: #e9ecef;
  color: #999;
  border-color: #ddd;
  cursor: not-allowed;
}

:deep(.el-button.is-disabled:hover) {
  background: #e9ecef;
  color: #999;
  border-color: #ddd;
  transform: none;
  box-shadow: none;
}

/* 响应式调整 */
@media (max-width: 768px) {
  .page-container {
    padding: 15px;
    margin: 10px;
  }

  .summary-cards {
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
  }

  .btn-group {
    flex-direction: column;
    align-items: center;
  }

  .param-stats {
    flex-wrap: wrap;
  }

  .round-form {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>

<style>
/* 全局对话框样式 */
.param-detail-dialog .el-message-box__content {
  padding: 20px;
}

.param-detail-dialog h3 {
  color: #4a00e0;
  margin-bottom: 15px;
  padding-bottom: 10px;
  border-bottom: 1px solid #eee;
}

.param-detail-dialog p {
  margin: 10px 0;
  line-height: 1.6;
}

.danger-btn {
  background: #d9534f !important;
  border-color: #d9534f !important;
}

.danger-btn:hover {
  background: #c9302c !important;
  border-color: #c9302c !important;
}
</style>