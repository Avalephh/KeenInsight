<template>
  <div class="page-container">
    <div class="page-title">第四步：调优参数推荐 & 应用</div>

    <!-- 轮次选择 -->
    <div class="form-section">
      <div class="section-title">轮次选择与对比</div>
      <el-form :model="paramForm" inline class="round-form">
        <el-form-item label="当前轮次">
          <el-select 
            v-model="paramForm.activeRound" 
            placeholder="请选择调优轮次"
            clearable
            @change="switchRound"
            class="round-select"
          >
            <el-option 
              v-for="item in paramRounds" 
              :key="item.round" 
              :label="`第${item.round}轮调优`" 
              :value="item.round"
            >
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
        <el-form-item label="对比轮次">
          <el-select 
            v-model="paramForm.compareRound" 
            multiple 
            placeholder="选择对比轮次"
            class="compare-select"
          >
            <el-option 
              v-for="item in paramRounds" 
              :key="item.round" 
              :label="`第${item.round}轮`" 
              :value="item.round"
            ></el-option>
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button @click="applyDefaultRound" class="secondary-btn">应用默认轮次</el-button>
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
                {{ currentRoundStats.latencyChange <= 0 ? '-' : '' }}{{ Math.abs(currentRoundStats.latencyChange) }}%
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
      <el-table :data="currentRoundParams" border class="custom-table param-table">
        <el-table-column prop="paramName" label="参数名" width="200">
          <template #default="{ row }">
            <div class="param-name-cell">
              <span class="param-name">{{ row.paramName }}</span>
              <div class="param-tag" :class="row.riskLevel">{{ row.riskLevel }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="originalValue" label="原始值" width="150">
          <template #default="{ row }">
            <span class="original-value">{{ row.originalValue }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="currentValue" label="推荐值" width="150">
          <template #default="{ row }">
            <span class="current-value highlight">{{ row.currentValue }}</span>
          </template>
        </el-table-column>
        <el-table-column label="变化方向" width="120" align="center">
          <template #default="{ row }">
            <div class="change-direction">
              <i :class="row.changeIcon" :style="{ color: row.changeColor }"></i>
              <el-tag :class="row.changeTypeClass" size="small">
                {{ row.changeType }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="预期影响" width="150">
          <template #default="{ row }">
            <el-tag :class="row.impactClass" size="small">{{ row.impact }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="desc" label="参数说明">
          <template #default="{ row }">
            <div class="param-desc">{{ row.desc }}</div>
            <div v-if="row.note" class="param-note">{{ row.note }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" align="center">
          <template #default="{ row }">
            <el-button 
              type="text" 
              size="small" 
              @click="viewParamDetail(row)"
              class="detail-btn"
            >
              详情
            </el-button>
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

    <!-- 效果对比图表 -->
    <div class="form-section" v-if="paramForm.compareRound.length > 0">
      <div class="section-title">
        轮次对比分析
        <span class="compare-count">{{ paramForm.compareRound.length }} 个轮次对比</span>
      </div>
      <div class="chart-container">
        <div ref="compareChartRef" class="chart-box"></div>
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
      <el-button 
        type="warning" 
        @click="resetParams" 
        class="warning-btn-lg"
        :disabled="!paramForm.activeRound"
      >
        <i class="el-icon-refresh"></i>
        重置为原始参数
      </el-button>
      <el-button 
        type="primary" 
        size="large" 
        @click="applyParams" 
        class="primary-btn-lg"
        :disabled="!paramForm.activeRound"
      >
        <i class="el-icon-check"></i>
        应用当前轮次参数
      </el-button>
      <el-button 
        type="success" 
        size="large" 
        @click="toLoadSelect" 
        class="success-btn-lg"
      >
        <i class="el-icon-finished"></i>
        完成调优，重新挖掘规则
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, onMounted, watch, nextTick, computed } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { ElMessage, ElMessageBox } from 'element-plus'

const router = useRouter()

// 调优轮次基础数据
const paramRounds = ref([
  { round: 1, tps: 10200, latency: 45.2, improvement: 12 },
  { round: 2, tps: 11500, latency: 38.7, improvement: 18 },
  { round: 3, tps: 12890, latency: 32.5, improvement: 24 },
  { round: 4, tps: 12750, latency: 33.1, improvement: 22 },
  { round: 5, tps: 13100, latency: 31.8, improvement: 26 }
])

// 轮次选择表单
const paramForm = reactive({
  activeRound: undefined as number | undefined,
  compareRound: [] as number[]
})

// 计算当前轮次统计数据
const currentRoundStats = computed(() => {
  if (!paramForm.activeRound) {
    return { tps: 0, latency: 0, tpsChange: 0, latencyChange: 0, improvement: 0 }
  }
  
  const round = paramRounds.value.find(r => r.round === paramForm.activeRound)
  const baseRound = paramRounds.value[0] // 第一轮作为基准
  
  if (!round) return { tps: 0, latency: 0, tpsChange: 0, latencyChange: 0, improvement: 0 }
  
  const tpsChange = Math.round(((round.tps - baseRound.tps) / baseRound.tps) * 100)
  const latencyChange = Math.round(((round.latency - baseRound.latency) / baseRound.latency) * 100)
  
  return {
    tps: round.tps.toLocaleString(),
    latency: round.latency,
    tpsChange,
    latencyChange,
    improvement: round.improvement
  }
})

// 当前轮次参数数据（增强版）
const currentRoundParams = ref([
  {
    paramName: 'innodb_buffer_pool_size',
    currentValue: '32G',
    originalValue: '16G',
    changeType: '提升',
    changeIcon: 'el-icon-top',
    changeColor: '#5cb85c',
    changeTypeClass: 'success',
    impact: 'TPS +15%',
    impactClass: 'success',
    desc: 'InnoDB缓冲池大小，提升后减少磁盘IO，提高缓存命中率',
    note: '需要确保服务器有足够内存',
    riskLevel: 'low',
    riskLabel: '低风险'
  },
  {
    paramName: 'innodb_purge_threads',
    currentValue: '8',
    originalValue: '4',
    changeType: '提升',
    changeIcon: 'el-icon-top',
    changeColor: '#5cb85c',
    changeTypeClass: 'success',
    impact: '锁等待 -20%',
    impactClass: 'success',
    desc: 'InnoDB purge线程数，提升后加快undo日志清理速度',
    note: 'CPU核心数足够时可适当增加',
    riskLevel: 'medium',
    riskLabel: '中风险'
  },
  {
    paramName: 'max_connections',
    currentValue: '8000',
    originalValue: '5000',
    changeType: '提升',
    changeIcon: 'el-icon-top',
    changeColor: '#5cb85c',
    changeTypeClass: 'success',
    impact: '并发能力 +30%',
    impactClass: 'success',
    desc: 'MySQL最大连接数，适配高并发场景',
    note: '需要监控内存使用情况',
    riskLevel: 'medium',
    riskLabel: '中风险'
  },
  {
    paramName: 'innodb_lock_wait_timeout',
    currentValue: '5',
    originalValue: '10',
    changeType: '降低',
    changeIcon: 'el-icon-bottom',
    changeColor: '#d9534f',
    changeTypeClass: 'danger',
    impact: '死锁率 -15%',
    impactClass: 'success',
    desc: '锁等待超时时间，缩短后减少死锁概率',
    note: '可能增加超时错误，需要应用层处理',
    riskLevel: 'high',
    riskLabel: '高风险'
  },
  {
    paramName: 'innodb_log_file_size',
    currentValue: '2G',
    originalValue: '1G',
    changeType: '提升',
    changeIcon: 'el-icon-top',
    changeColor: '#5cb85c',
    changeTypeClass: 'success',
    impact: '写性能 +10%',
    impactClass: 'success',
    desc: 'InnoDB日志文件大小，提升后减少日志切换频率',
    note: '需要重启数据库生效',
    riskLevel: 'high',
    riskLabel: '高风险'
  },
  {
    paramName: 'query_cache_size',
    currentValue: '64M',
    originalValue: '128M',
    changeType: '降低',
    changeIcon: 'el-icon-bottom',
    changeColor: '#d9534f',
    changeTypeClass: 'danger',
    impact: '内存占用 -50%',
    impactClass: 'success',
    desc: '查询缓存大小，降低后释放内存给其他组件',
    note: '在MySQL 8.0中已弃用',
    riskLevel: 'low',
    riskLabel: '低风险'
  }
])

// 计算参数统计
const increaseCount = computed(() => 
  currentRoundParams.value.filter(p => p.changeType === '提升').length
)

const decreaseCount = computed(() => 
  currentRoundParams.value.filter(p => p.changeType === '降低').length
)

const highRiskCount = computed(() => 
  currentRoundParams.value.filter(p => p.riskLevel === 'high').length
)

// 对比图表容器
const compareChartRef = ref<HTMLElement | null>(null)
let compareChart: echarts.ECharts | null = null

// 组件挂载后初始化
onMounted(() => {
  // 延迟设置默认值
  setTimeout(() => {
    paramForm.activeRound = 3
    initCompareChart()
    window.addEventListener('resize', resizeCompareChart)
  }, 100)
})

// 初始化对比图表
const initCompareChart = () => {
  if (!compareChartRef.value) return
  compareChart = echarts.init(compareChartRef.value)
  
  // 监听对比轮次变化，更新图表
  watch(() => paramForm.compareRound, (val) => {
    if (val.length === 0 || !compareChart) {
      // 如果没有选择对比轮次，显示空状态
      compareChart.clear()
      return
    }
    
    // 筛选对比轮次的数据源
    const compareData = paramRounds.value.filter(item => val.includes(item.round))
    const option = {
      title: { 
        text: '调优轮次性能对比',
        left: 'left',
        fontSize: 14,
        fontWeight: 'normal'
      },
      tooltip: { 
        trigger: 'axis',
        axisPointer: { type: 'shadow' }
      },
      legend: { 
        data: ['TPS', '延迟(ms)', '性能提升(%)'],
        top: 30,
        textStyle: { fontSize: 12 }
      },
      grid: { 
        left: '3%', 
        right: '4%', 
        bottom: '3%', 
        top: '20%',
        containLabel: true 
      },
      xAxis: { 
        type: 'category', 
        data: compareData.map(item => `第${item.round}轮`),
        axisLine: { lineStyle: { color: '#eee' } }
      },
      yAxis: [
        { 
          type: 'value', 
          name: 'TPS',
          min: 0,
          axisLine: { show: true },
          splitLine: { lineStyle: { type: 'dashed' } }
        },
        { 
          type: 'value', 
          name: '延迟(ms)', 
          min: 0, 
          position: 'right',
          axisLine: { show: true },
          splitLine: { show: false }
        }
      ],
      series: [
        { 
          name: 'TPS', 
          type: 'bar', 
          barWidth: '30%',
          itemStyle: { color: '#4a00e0' },
          data: compareData.map(item => item.tps) 
        },
        { 
          name: '延迟(ms)', 
          type: 'line', 
          yAxisIndex: 1, 
          smooth: true,
          itemStyle: { color: '#f0ad4e' },
          lineStyle: { width: 3 },
          data: compareData.map(item => item.latency) 
        },
        { 
          name: '性能提升(%)', 
          type: 'line', 
          yAxisIndex: 1, 
          smooth: true,
          itemStyle: { color: '#5cb85c' },
          lineStyle: { width: 2, type: 'dashed' },
          data: compareData.map(item => item.improvement) 
        }
      ]
    }
    
    compareChart.setOption(option)
  }, { immediate: true })
}

// 窗口resize适配图表
const resizeCompareChart = () => {
  compareChart?.resize()
}

// 应用默认轮次
const applyDefaultRound = () => {
  paramForm.activeRound = 3
  ElMessage.success('已应用默认调优轮次')
}

// 切换轮次（获取对应轮次参数）
const switchRound = () => {
  if (paramForm.activeRound === undefined) {
    ElMessage.warning('请选择调优轮次')
    return
  }
  
  // 模拟API调用
  ElMessage.success(`已切换到第${paramForm.activeRound}轮参数`)
  
  // 这里可以添加实际的API调用逻辑
  // fetch(`/api/param/round/${paramForm.activeRound}`, {
  //   method: 'GET'
  // }).then(res => res.json()).then(data => {
  //   if (data.code === 200) {
  //     currentRoundParams.value = data.data
  //     ElMessage.success(`已切换到第${paramForm.activeRound}轮参数`)
  //   } else {
  //     ElMessage.error(`获取第${paramForm.activeRound}轮参数失败：${data.msg}`)
  //   }
  // })
}

// 查看参数详情
const viewParamDetail = (param: any) => {
  ElMessageBox.alert(
    `<div>
      <h3>${param.paramName}</h3>
      <p><strong>原始值：</strong>${param.originalValue}</p>
      <p><strong>推荐值：</strong><span style="color: #4a00e0">${param.currentValue}</span></p>
      <p><strong>调整方向：</strong>${param.changeType}</p>
      <p><strong>预期影响：</strong>${param.impact}</p>
      <p><strong>风险等级：</strong><span class="${param.riskLevel === 'high' ? 'danger' : param.riskLevel === 'medium' ? 'warning' : 'success'}">${param.riskLabel}</span></p>
      <p><strong>详细说明：</strong>${param.desc}</p>
      <p><strong>注意事项：</strong>${param.note || '无'}</p>
    </div>`,
    '参数详情',
    {
      dangerouslyUseHTMLString: true,
      confirmButtonText: '确定',
      customClass: 'param-detail-dialog'
    }
  )
}

// 重置为原始参数
const resetParams = () => {
  if (paramForm.activeRound === undefined) {
    ElMessage.warning('请先选择调优轮次')
    return
  }
  
  ElMessageBox.confirm(
    '确定要重置为原始参数吗？此操作会覆盖当前调优结果！',
    '参数重置确认',
    {
      confirmButtonText: '确定重置',
      cancelButtonText: '取消',
      type: 'warning',
      confirmButtonClass: 'danger-btn'
    }
  ).then(() => {
    // 模拟API调用
    ElMessage.success('参数已重置为原始值')
    
    // 重置表格数据为原始值
    currentRoundParams.value.forEach(item => {
      item.currentValue = item.originalValue
      item.changeType = '还原'
      item.changeIcon = 'el-icon-refresh'
      item.changeColor = '#666'
      item.changeTypeClass = 'info'
    })
  })
}

// 应用当前轮次参数
const applyParams = () => {
  if (paramForm.activeRound === undefined) {
    ElMessage.warning('请先选择调优轮次')
    return
  }
  
  ElMessageBox.confirm(
    `<div>
      <p><strong>确认应用第${paramForm.activeRound}轮参数吗？</strong></p>
      <p>应用后系统将重启生效，预计性能提升 <span style="color: #5cb85c">+${currentRoundStats.value.improvement}%</span>。</p>
      <p>包含 ${increaseCount.value} 个提升参数，${decreaseCount.value} 个降低参数。</p>
      <p>其中有 ${highRiskCount.value} 个高风险参数，请确认。</p>
    </div>`,
    '参数应用确认',
    {
      dangerouslyUseHTMLString: true,
      confirmButtonText: '确认应用',
      cancelButtonText: '取消',
      type: 'primary',
      confirmButtonClass: 'primary-btn'
    }
  ).then(() => {
    // 模拟API调用
    ElMessage.success('参数应用成功，系统将重启生效')
    
    // 这里可以添加实际的API调用逻辑
    // fetch('/api/param/apply', {
    //   method: 'POST',
    //   headers: { 'Content-Type': 'application/json' },
    //   body: JSON.stringify({
    //     round: paramForm.activeRound,
    //     params: currentRoundParams.value
    //   })
    // }).then(res => res.json()).then(data => {
    //   if (data.code === 200) {
    //     ElMessage.success('参数应用成功，系统已重启生效')
    //   } else {
    //     ElMessage.error(`参数应用失败：${data.msg}`)
    //   }
    // })
  })
}

// 页面跳转方法
const toExceptionAnalysis = () => {
  router.push({ name: 'exception-analysis' })
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
</script>

<style scoped>
/* 整体样式 - 与home.html一致 */
body {
  margin: 0;
  font-family: "Segoe UI", Roboto, Arial, sans-serif;
  background: #f4f6f9;
  color: #333;
}

.page-container {
  background: #fff;
  border-radius: 10px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.06);
  padding: 26px 34px;
  max-width: 1200px;
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
  box-shadow: 0 4px 12px rgba(0,0,0,0.06);
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

/* 轮次选择表单 */
.round-form {
  display: flex;
  align-items: center;
  gap: 20px;
}

.round-select, .compare-select {
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
  box-shadow: 0 4px 8px rgba(0,0,0,0.1);
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

.primary-btn-lg, .success-btn-lg, .warning-btn-lg, .secondary-btn-lg {
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