<template>
  <div class="page-container">
    <div class="page-title">第二步：系统状态实时监控</div>

    <!-- 核心性能指标卡片 -->
    <div class="status-card-group">
      <div class="status-card">
        <div class="status-card-title">当前TPS</div>
        <div class="status-card-value">{{ systemStatus.tps }}</div>
      </div>
      <div class="status-card">
        <div class="status-card-title">平均延迟(ms)</div>
        <div class="status-card-value">{{ systemStatus.latency }}</div>
      </div>
      <div class="status-card">
        <div class="status-card-title">CPU利用率(%)</div>
        <div class="status-card-value">{{ systemStatus.cpu }}</div>
      </div>
      <div class="status-card">
        <div class="status-card-title">内存使用率(%)</div>
        <div class="status-card-value">{{ systemStatus.memory }}</div>
      </div>
    </div>

    <!-- 性能趋势图表 -->
    <div class="form-section">
      <div class="form-section-title">性能趋势（TPS/延迟）</div>
      <div ref="performanceChartRef" class="chart-box"></div>
    </div>

    <!-- 资源监控 & 火焰图 -->
    <el-row :gutter="20">
      <el-col :span="12">
        <div class="form-section">
          <div class="form-section-title">CPU/内存监控</div>
          <div ref="resourceChartRef" class="chart-box"></div>
        </div>
      </el-col>
      <el-col :span="12">
        <div class="form-section">
          <div class="form-section-title">火焰图（函数调用栈）</div>
          <div class="chart-box flame-chart">
            火焰图占位（实际对接火焰图组件）
          </div>
        </div>
      </el-col>
    </el-row>

    <div class="btn-group">
      <el-button @click="toLoadSelect">返回上一步</el-button>
      <el-button type="primary" size="large" @click="toExceptionAnalysis">分析异常函数</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'

const router = useRouter()

// 系统状态数据
const systemStatus = reactive({
  tps: 12890,
  latency: 32.5,
  cpu: 78.2,
  memory: 85.7
})

// 图表容器ref
const performanceChartRef = ref<HTMLElement | null>(null)
const resourceChartRef = ref<HTMLElement | null>(null)
let performanceChart: echarts.ECharts | null = null
let resourceChart: echarts.ECharts | null = null

// 初始化图表
const initCharts = () => {
  // 性能趋势图表
  if (performanceChartRef.value) {
    performanceChart = echarts.init(performanceChartRef.value)
    performanceChart.setOption({
      title: { text: 'TPS/延迟趋势（近10分钟）', left: 'left', fontSize: 14 },
      tooltip: { trigger: 'axis' },
      legend: { data: ['TPS', '延迟(ms)'], top: 30 },
      grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
      xAxis: { 
        type: 'category', 
        data: ['1分', '2分', '3分', '4分', '5分', '6分', '7分', '8分', '9分', '10分'] 
      },
      yAxis: [
        { type: 'value', name: 'TPS', min: 0, max: 15000 },
        { type: 'value', name: '延迟(ms)', min: 0, max: 50, position: 'right' }
      ],
      series: [
        { name: 'TPS', type: 'line', data: [8500, 9200, 9800, 10500, 11200, 11800, 12200, 12500, 12700, 12890] },
        { name: '延迟(ms)', type: 'line', yAxisIndex: 1, data: [48, 46, 44, 42, 40, 38, 36, 34, 33, 32.5] }
      ]
    })
  }

  // 资源监控图表
  if (resourceChartRef.value) {
    resourceChart = echarts.init(resourceChartRef.value)
    resourceChart.setOption({
      title: { text: 'CPU/内存使用率（近10分钟）', left: 'left', fontSize: 14 },
      tooltip: { trigger: 'axis' },
      legend: { data: ['CPU(%)', '内存(%)'], top: 30 },
      grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
      xAxis: { 
        type: 'category', 
        data: ['1分', '2分', '3分', '4分', '5分', '6分', '7分', '8分', '9分', '10分'] 
      },
      yAxis: { type: 'value', min: 0, max: 100 },
      series: [
        { name: 'CPU(%)', type: 'line', data: [65, 68, 70, 72, 75, 76, 77, 78, 78.5, 78.2] },
        { name: '内存(%)', type: 'line', data: [78, 80, 82, 83, 84, 85, 85.5, 86, 85.8, 85.7] }
      ]
    })
  }
}

// 窗口resize时图表自适应
const resizeCharts = () => {
  performanceChart?.resize()
  resourceChart?.resize()
}

onMounted(() => {
  initCharts()
  window.addEventListener('resize', resizeCharts)
})

// 跳转页面
const toLoadSelect = () => {
  router.push({ name: 'load-select' })
}

const toExceptionAnalysis = () => {
  // // 调用后端接口示例
  // fetch('/api/analyze/exception', {
  //   method: 'POST',
  //   headers: { 'Content-Type': 'application/json' },
  //   body: JSON.stringify({ systemStatus })
  // }).then(res => res.json()).then(data => {
  //   if (data.code === 200) {
  //     ElMessage.success('异常分析完成')
  //     router.push({ name: 'exception-analysis' })
  //   } else {
  //     ElMessage.error('异常分析失败：' + data.msg)
  //   }
  // })
  router.push({ name: 'exception-analysis' })
}
</script>

<style scoped>
.page-container {
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.1);
  padding: 30px;
  max-width: 1200px;
  margin: 0 auto;
  min-height: 600px;
}
.page-title {
  font-size: 18px;
  font-weight: 600;
  color: #1f2937;
  margin-bottom: 20px;
  padding-bottom: 10px;
  border-bottom: 1px solid #e5e7eb;
}
.status-card-group {
  display: flex;
  gap: 20px;
  margin: 20px 0;
  flex-wrap: wrap;
}
.status-card {
  flex: 1;
  min-width: 200px;
  padding: 15px;
  background: #f9fafb;
  border-radius: 6px;
  border-left: 4px solid #4096ff;
}
.status-card-title {
  font-size: 14px;
  color: #6b7280;
  margin-bottom: 8px;
}
.status-card-value {
  font-size: 24px;
  font-weight: 600;
  color: #1f2937;
}
.form-section {
  margin-bottom: 25px;
  padding: 20px;
  background: #f9fafb;
  border-radius: 6px;
}
.form-section-title {
  font-size: 16px;
  font-weight: 500;
  color: #374151;
  margin-bottom: 15px;
}
.chart-box {
  width: 100%;
  height: 300px;
  border-radius: 6px;
}
.flame-chart {
  display: flex;
  align-items: center;
  justify-content: center;
  color: #6b7280;
  background: #f9fafb;
}
.btn-group {
  margin-top: 30px;
  text-align: right;
}
</style>