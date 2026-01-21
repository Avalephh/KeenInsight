<template>
  <div class="page-container">
    <el-alert v-if="waitingTuneFinish" title="多轮调优正在运行中" type="info" show-icon :closable="false"
      description="系统正在后台执行调优任务，请耐心等待完成后自动跳转。" />
    <div class="page-title">一步调优参数推荐</div>

    <!-- 性能概览 -->
    <div class="table-section" v-if="tuneResult">
      <div class="section-title">一步调优性能概览</div>
      <div class="performance-cards">
        <div class="performance-card highlight">
          <div class="card-header">
            <div class="card-icon">
              <i class="el-icon-s-data"></i>
            </div>
            <div class="card-title">基础性能评分</div>
          </div>
          <div class="card-value">{{ tuneResult.base_score }}</div>
          <div class="card-subtitle">调优前</div>
        </div>

        <div class="performance-card highlight">
          <div class="card-header">
            <div class="card-icon">
              <i class="el-icon-trend-charts"></i>
            </div>
            <div class="card-title">预测性能评分</div>
          </div>
          <div class="card-value">{{ tuneResult.predicted_score }}</div>
          <div class="card-subtitle">调优后</div>
        </div>

        <div class="performance-card highlight">
          <div class="card-header">
            <div class="card-icon">
              <i class="el-icon-top-right"></i>
            </div>
            <div class="card-title">性能提升</div>
          </div>
          <div class="card-value">+{{ tuneResult.delta_score.toFixed(1) }}</div>
          <div class="card-change">
            提升 {{ ((tuneResult.delta_score / tuneResult.base_score) * 100).toFixed(1) }}%
          </div>
        </div>

        <div class="performance-card highlight">
          <div class="card-header">
            <div class="card-icon">
              <i class="el-icon-setting"></i>
            </div>
            <div class="card-title">参数调整</div>
          </div>
          <div class="card-value">{{ changedParams.length }}</div>
          <div class="card-subtitle">个参数变化</div>
        </div>
      </div>
    </div>

    <!-- 参数调整表格 -->
    <div class="table-section">
      <div class="section-title">
        参数调整详情
        <span class="change-count">{{ changedParams.length }} 个参数发生变化</span>
      </div>

      <!-- 筛选器 -->
      <div class="action-buttons" v-if="changedParams.length > 0">
        <el-input v-model="filter.keyword" placeholder="搜索参数名" clearable size="small"
          style="width: 200px; margin-right: 10px;" @input="filterParams">
          <template #prefix>
            <i class="el-icon-search"></i>
          </template>
        </el-input>

        <el-select v-model="filter.changeType" placeholder="变化类型" clearable size="small"
          style="width: 120px; margin-right: 10px;" @change="filterParams">
          <el-option label="提升" value="increase"></el-option>
          <el-option label="降低" value="decrease"></el-option>
        </el-select>

        <el-select v-model="filter.riskLevel" placeholder="风险等级" clearable size="small"
          style="width: 120px; margin-right: 10px;" @change="filterParams">
          <el-option label="高风险" value="high"></el-option>
          <el-option label="中风险" value="medium"></el-option>
          <el-option label="低风险" value="low"></el-option>
        </el-select>

        <span class="filter-stats">
          显示 {{ filteredParams.length }} / {{ changedParams.length }} 个参数
        </span>
      </div>

      <!-- 参数表格 -->
      <el-table :data="filteredParams" border class="custom-table param-table" empty-text="暂无参数调整数据">
        <el-table-column prop="paramName" label="参数名" width="300">
          <template #default="{ row }">
            <div class="func-name-cell">
              <span class="func-name">{{ row.paramName }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="原始值" width="180">
          <template #default="{ row }">
            <span class="current-value">{{ formatValue(row.originalValue) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="推荐值" width="180">
          <template #default="{ row }">
            <span class="suggested-value">{{ formatValue(row.currentValue) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="变化方向" width="150" align="center">
          <template #default="{ row }">
            <div class="change-direction">
              <i :class="row.changeIcon" :style="{ color: row.changeColor }"></i>
              <el-tag :class="row.changeTypeClass" size="small">
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
        <el-table-column label="预期影响" width="250">
          <template #default="{ row }">
            <el-tag :class="row.impactClass" size="small">{{ row.impact }}</el-tag>
          </template>
        </el-table-column>
        <!-- <el-table-column prop="desc" label="参数说明">
          <template #default="{ row }">
            <div class="param-desc">{{ getParamDesc(row.paramName) }}</div>
            <div v-if="row.note" class="param-note">{{ row.note }}</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" align="center">
          <template #default="{ row }">
            <el-button type="text" size="small" @click="viewParamDetail(row)" class="detail-btn">
              详情
            </el-button>
          </template>
        </el-table-column> -->
      </el-table>

      <!-- 参数统计 -->
      <div class="param-stats" v-if="changedParams.length > 0">
        <div class="stats-item">
          <span class="stats-label">总参数数:</span>
          <span class="stats-value">{{ totalParamsCount }}</span>
        </div>
        <div class="stats-item">
          <span class="stats-label">调整参数:</span>
          <span class="stats-value">{{ changedParams.length }}</span>
        </div>
        <div class="stats-item">
          <span class="stats-label">性能提升:</span>
          <span class="stats-value positive">+{{ tuneResult?.delta_score.toFixed(1) || 0 }}分</span>
        </div>
        <div class="stats-item">
          <span class="stats-label">高风险:</span>
          <span class="stats-value warning">{{ highRiskCount }}个</span>
        </div>
      </div>
    </div>

    <!-- 操作按钮 -->
    <div class="btn-group">
      <el-button @click="toExceptionAnalysis" class="secondary-btn-lg">
        <i class="el-icon-back"></i>
        返回上一步
      </el-button>
      <!-- <el-button type="warning" @click="resetParams" class="warning-btn-lg" :disabled="!tuneResult">
        <i class="el-icon-refresh"></i>
        重置为原始参数
      </el-button> -->
      <el-button type="primary" size="large" @click="applyParams" class="primary-btn-lg" :loading="applyingParams"
        :disabled="!tuneResult">
        <i class="el-icon-check"></i>
        {{ applyingParams ? '应用中...' : '应用当前参数配置' }}
      </el-button>
      <el-button type="success" size="large" @click="startMultiRoundTune" class="success-btn-lg"
        :loading="startingMultiRound || waitingTuneFinish" :disabled="waitingTuneFinish">
        <i class="el-icon-refresh-right"></i>
        {{ waitingTuneFinish ? '调优进行中...' : '启动多轮调优（备库）' }}
      </el-button>

    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, onMounted, computed, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { tuneAPI } from '@/api/tune'

const router = useRouter()
const route = useRoute()

const taskName = computed(() => route.query.taskName as string || '')

// 调优结果数据
const tuneResult = ref<any>(null)
const loading = ref(false)

// 参数数据
const baseParams = ref<Record<string, any>>({})
const recommendedParams = ref<Record<string, any>>({})
const changedParams = ref<any[]>([])
const filteredParams = ref<any[]>([])
const totalParamsCount = ref(0)

// 筛选器
const filter = reactive({
  keyword: '',
  changeType: '',
  riskLevel: ''
})

// 状态
const applyingParams = ref(false)
const startingMultiRound = ref(false)

// 调优状态轮询
const pollingTimer = ref<number | null>(null)
const pollingInterval = 3000 // 3 秒轮询一次

// UI 状态
const waitingTuneFinish = ref(false)
const tuneStatus = ref<'pending' | 'running' | 'success' | 'failed'>('pending')


// 参数描述映射
const paramDescriptions: Record<string, string> = {
  'innodb_buffer_pool_size': 'InnoDB缓冲池大小，用于缓存数据和索引，对性能影响最大',
  'innodb_thread_concurrency': 'InnoDB线程并发数，控制同时运行的线程数量',
  'innodb_log_file_size': 'InnoDB日志文件大小，影响事务提交速度和恢复时间',
  'key_buffer_size': 'MyISAM索引缓存大小',
  'query_cache_size': '查询缓存大小（MySQL 8.0已弃用）',
  'tmp_table_size': '临时表大小限制',
  'max_heap_table_size': '内存表大小限制',
  'sort_buffer_size': '排序缓冲区大小',
  'join_buffer_size': '连接缓冲区大小',
  'read_buffer_size': '顺序读缓冲区大小',
  'read_rnd_buffer_size': '随机读缓冲区大小',
  'table_open_cache': '表缓存数量',
  'thread_cache_size': '线程缓存数量',
  'max_connections': '最大连接数',
  'innodb_flush_log_at_trx_commit': '事务提交时日志刷新策略',
  'innodb_log_buffer_size': 'InnoDB日志缓冲区大小',
  'innodb_lock_wait_timeout': '锁等待超时时间',
  'innodb_io_capacity': 'InnoDB IO能力',
  'innodb_io_capacity_max': 'InnoDB最大IO能力',
  'innodb_read_io_threads': 'InnoDB读IO线程数',
  'innodb_write_io_threads': 'InnoDB写IO线程数',
  'innodb_purge_threads': 'InnoDB清理线程数',
  'innodb_page_cleaners': 'InnoDB页面清理线程数',
  'innodb_adaptive_hash_index': '自适应哈希索引',
  'innodb_change_buffering': '变更缓冲类型',
  'innodb_stats_on_metadata': '元数据统计'
}

// 初始化加载数据
onMounted(async () => {
  await loadTuneResult()
})

onUnmounted(() => {
  stopPolling()
})

// 加载调优结果
const loadTuneResult = async () => {
  if (!taskName.value) {
    ElMessage.warning('未检测到任务信息')
    return
  }

  loading.value = true
  try {
    // 加载一步调优结果
    const res = await tuneAPI.getOneStepResult(taskName.value)

    if (res.code === 200 && res.data?.records) {
      // 注意：res.data.records 直接就是 OneStepResult 对象，不是数组
      tuneResult.value = res.data.records
      baseParams.value = tuneResult.value.base_config || {}
      recommendedParams.value = tuneResult.value.recommended_config || {}

      // 计算变化的参数
      analyzeChangedParams()
    } else {
      ElMessage.error(res.msg || '加载调优结果失败')
    }
  } catch (error: any) {
    console.error('加载调优结果失败:', error)
    // 更友好的错误提示
    if (error.response && error.response.status === 400) {
      ElMessage.warning('调优任务尚未完成，请等待任务完成后再查看结果')
    } else {
      ElMessage.error(error.message || '加载调优结果失败')
    }
  } finally {
    loading.value = false
  }
}

// 分析变化的参数
const analyzeChangedParams = () => {
  const changed: any[] = []

  // 计算总参数数
  totalParamsCount.value = Object.keys(baseParams.value).length

  // 找出所有变化的参数
  Object.keys(baseParams.value).forEach(paramName => {
    const originalValue = baseParams.value[paramName]
    const recommendedValue = recommendedParams.value[paramName]

    // 如果值不同，则参数发生了变化
    if (originalValue !== recommendedValue) {
      const changePercent = recommendedValue > originalValue
        ? ((recommendedValue - originalValue) / originalValue * 100).toFixed(1)
        : ((originalValue - recommendedValue) / originalValue * 100).toFixed(1)

      const isIncrease = recommendedValue > originalValue
      const changeType = isIncrease ? '提升' : '降低'

      // 判断风险等级（根据参数重要性和变化幅度）
      const riskLevel = assessRiskLevel(paramName, Math.abs(parseFloat(changePercent)))

      // 判断预期影响
      const impact = assessImpact(paramName, isIncrease)

      changed.push({
        paramName,
        originalValue,
        currentValue: recommendedValue,
        changePercent,
        changeType,
        changeIcon: isIncrease ? 'el-icon-top' : 'el-icon-bottom',
        changeColor: isIncrease ? '#5cb85c' : '#d9534f',
        changeTypeClass: isIncrease ? 'success' : 'danger',
        impact,
        impactClass: 'success',
        riskLevel,
        note: getParamNote(paramName, originalValue, recommendedValue)
      })
    }
  })

  // 按风险等级和变化幅度排序
  changed.sort((a, b) => {
    const riskOrder = { high: 3, medium: 2, low: 1 }
    if (riskOrder[b.riskLevel] !== riskOrder[a.riskLevel]) {
      return riskOrder[b.riskLevel] - riskOrder[a.riskLevel]
    }
    return Math.abs(parseFloat(b.changePercent)) - Math.abs(parseFloat(a.changePercent))
  })

  changedParams.value = changed
  filteredParams.value = changed
}

// 评估风险等级
const assessRiskLevel = (paramName: string, changePercent: number): 'high' | 'medium' | 'low' => {
  // 关键参数 + 大幅变化 = 高风险
  const criticalParams = [
    'innodb_buffer_pool_size',
    'innodb_log_file_size',
    'max_connections',
    'innodb_flush_log_at_trx_commit'
  ]

  const importantParams = [
    'innodb_thread_concurrency',
    'innodb_io_capacity',
    'innodb_read_io_threads',
    'innodb_write_io_threads',
    'key_buffer_size',
    'query_cache_size'
  ]

  if (criticalParams.includes(paramName) && changePercent > 50) {
    return 'high'
  }

  if (criticalParams.includes(paramName) ||
    (importantParams.includes(paramName) && changePercent > 100)) {
    return 'medium'
  }

  return 'low'
}

// 评估预期影响
const assessImpact = (paramName: string, isIncrease: boolean): string => {
  const impactMap: Record<string, string> = {
    'innodb_buffer_pool_size': isIncrease ? '缓存命中率提升' : '内存占用降低',
    'innodb_thread_concurrency': isIncrease ? '并发处理能力提升' : '线程竞争降低',
    'innodb_log_file_size': isIncrease ? '事务提交速度提升' : '恢复时间缩短',
    'max_connections': isIncrease ? '连接数上限提升' : '内存占用降低',
    'key_buffer_size': isIncrease ? '索引缓存提升' : '内存占用降低',
    'query_cache_size': isIncrease ? '查询缓存提升' : '内存占用降低',
    'sort_buffer_size': isIncrease ? '排序性能提升' : '内存占用降低',
    'join_buffer_size': isIncrease ? '连接性能提升' : '内存占用降低',
    'read_buffer_size': isIncrease ? '读取性能提升' : '内存占用降低',
    'table_open_cache': isIncrease ? '表缓存提升' : '内存占用降低',
    'thread_cache_size': isIncrease ? '连接复用提升' : '内存占用降低'
  }

  return impactMap[paramName] || (isIncrease ? '性能可能提升' : '资源占用降低')
}

// 获取参数备注
const getParamNote = (paramName: string, original: any, recommended: any): string => {
  const notes: Record<string, string> = {
    'innodb_buffer_pool_size': `从 ${formatBytes(original)} 调整为 ${formatBytes(recommended)}`,
    'innodb_log_file_size': `从 ${formatBytes(original)} 调整为 ${formatBytes(recommended)}`,
    'query_cache_size': '在MySQL 8.0中已弃用，建议关闭'
  }

  return notes[paramName] || ''
}

// 获取参数描述
const getParamDesc = (paramName: string): string => {
  return paramDescriptions[paramName] || '数据库配置参数'
}

// 格式化数值
const formatValue = (value: any): string => {
  if (typeof value === 'number') {
    // 如果是字节单位，格式化为更易读的形式
    if (value >= 1024 * 1024) {
      return formatBytes(value)
    }
    return value.toString()
  }
  return String(value)
}

// 格式化字节
const formatBytes = (bytes: number): string => {
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unitIndex = 0

  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex++
  }

  return `${value.toFixed(1)}${units[unitIndex]}`
}

// 筛选参数
const filterParams = () => {
  let result = changedParams.value

  if (filter.keyword) {
    const keyword = filter.keyword.toLowerCase()
    result = result.filter(param =>
      param.paramName.toLowerCase().includes(keyword)
    )
  }

  if (filter.changeType) {
    if (filter.changeType === 'increase') {
      result = result.filter(param => param.changeType === '提升')
    } else if (filter.changeType === 'decrease') {
      result = result.filter(param => param.changeType === '降低')
    }
  }

  if (filter.riskLevel) {
    result = result.filter(param => param.riskLevel === filter.riskLevel)
  }

  filteredParams.value = result
}

// 计算高风险参数数量
const highRiskCount = computed(() => {
  return changedParams.value.filter(param => param.riskLevel === 'high').length
})

// 查看参数详情
const viewParamDetail = (param: any) => {
  ElMessageBox.alert(
    `<div>
      <h3>${param.paramName}</h3>
      <p><strong>原始值：</strong>${formatValue(param.originalValue)}</p>
      <p><strong>推荐值：</strong><span style="color: #4a00e0">${formatValue(param.currentValue)}</span></p>
      <p><strong>变化方向：</strong>${param.changeType} ${param.changePercent}%</p>
      <p><strong>预期影响：</strong>${param.impact}</p>
      <p><strong>风险等级：</strong><span class="${param.riskLevel === 'high' ? 'danger' : param.riskLevel === 'medium' ? 'warning' : 'success'}">${param.riskLevel === 'high' ? '高风险' : param.riskLevel === 'medium' ? '中风险' : '低风险'}</span></p>
      <p><strong>详细说明：</strong>${getParamDesc(param.paramName)}</p>
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

// 重置参数
const resetParams = () => {
  if (!tuneResult.value) {
    ElMessage.warning('暂无调优结果')
    return
  }

  ElMessageBox.confirm(
    '确定要重置为原始参数吗？此操作会覆盖当前调优结果！',
    '参数重置确认',
    {
      confirmButtonText: '确定重置',
      cancelButtonText: '取消',
      type: 'warning'
    }
  ).then(() => {
    // 这里可以调用API重置参数
    ElMessage.success('参数重置请求已发送')
    // 重新加载数据
    loadTuneResult()
  })
}

const pollTuneStatus = async () => {
  try {
    const res = await tuneAPI.getMultiTuneStatus(taskName.value)

    if (res.code !== 200) {
      throw new Error('状态接口返回异常')
    }

    tuneStatus.value = res.status

    if (res.status === 'success') {
      stopPolling()
      waitingTuneFinish.value = false

      ElMessage.success('多轮调优完成，正在跳转结果页面...')

      router.push({
        name: 'multi-knob-recommend',
        query: {
          taskName: taskName.value
        }
      })
    }

    if (res.status === 'failed') {
      stopPolling()
      waitingTuneFinish.value = false
      ElMessage.error('多轮调优失败，请查看日志')
    }

    // running / pending → 什么都不做，继续等
  } catch (err) {
    console.error('轮询调优状态失败:', err)
    stopPolling()
    waitingTuneFinish.value = false
    ElMessage.error('获取调优状态失败')
  }
}

const stopPolling = () => {
  if (pollingTimer.value) {
    clearInterval(pollingTimer.value)
    pollingTimer.value = null
  }
}



// 应用参数
const applyParams = async () => {
  if (!tuneResult.value) {
    ElMessage.warning('暂无调优结果')
    return
  }

  ElMessageBox.confirm(
    `<div>
      <p><strong>确认应用调优参数吗？</strong></p>
      <p>预计性能提升：<span style="color: #5cb85c">+${tuneResult.value.delta_score.toFixed(1)}分</span>（${((tuneResult.value.delta_score / tuneResult.value.base_score) * 100).toFixed(1)}%）</p>
      <p>共调整 <strong>${changedParams.value.length}</strong> 个参数</p>
      <p>其中 <strong style="color: #f0ad4e">${highRiskCount.value}个高风险</strong> 参数需要特别注意</p>
    </div>`,
    '参数应用确认',
    {
      dangerouslyUseHTMLString: true,
      confirmButtonText: '确认应用',
      cancelButtonText: '取消',
      type: 'primary'
    }
  ).then(async () => {
    applyingParams.value = true
    try {
      // 这里调用应用参数的API
      // const res = await tuneAPI.applyTuneParams({
      //   taskName: taskName.value,
      //   params: recommendedParams.value
      // })

      // 模拟API调用
      await new Promise(resolve => setTimeout(resolve, 2000))

      ElMessage.success('参数应用成功！建议重启数据库使配置生效')
    } catch (error) {
      console.error('应用参数失败:', error)
      ElMessage.error('应用参数失败')
    } finally {
      applyingParams.value = false
    }
  })
}

// 启动多轮调优
const startMultiRoundTune = async () => {
  ElMessageBox.confirm(
    `<div>
      <p><strong>确定要启动多轮调优吗？</strong></p>
      <p>多轮调优将在备库上运行，不会影响生产环境。</p>
      <p>预计耗时较长，请耐心等待。</p>
    </div>`,
    '启动多轮调优',
    {
      dangerouslyUseHTMLString: true,
      confirmButtonText: '确认启动',
      cancelButtonText: '取消',
      type: 'success'
    }
  ).then(async () => {
    startingMultiRound.value = true
    waitingTuneFinish.value = true

    try {
      const response = await tuneAPI.startTune({
        taskName: taskName.value
      })

      if (response.code !== 200) {
        throw new Error(response.msg || '启动失败')
      }

      ElMessage({
        message: '调优任务已启动，系统正在运行中...',
        type: 'success',
        duration: 2000
      })

      // 启动轮询
      pollTuneStatus()
      pollingTimer.value = window.setInterval(
        pollTuneStatus,
        pollingInterval
      )

    } catch (error) {
      console.error('启动多轮调优失败:', error)
      waitingTuneFinish.value = false
      ElMessage.error('启动多轮调优失败')
    } finally {
      startingMultiRound.value = false
    }
  })
}


// 页面跳转
const toExceptionAnalysis = () => {
  router.push({
    name: 'exception-analysis',
    query: {
      taskName: taskName.value
    }
  })
}
</script>

<style scoped>
/* 保持原有的样式完全不变 */
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
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
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

/* 表格区域样式 */
.table-section,
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
}

/* 表格样式 */
.custom-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 14px;
}

:deep(.custom-table th) {
  background: #f5f7fb;
  padding: 12px 15px;
  border-bottom: 1px solid #eee;
  font-size: 14px;
  text-align: left;
  font-weight: 600;
  color: #333;
}

:deep(.custom-table td) {
  padding: 12px 15px;
  border-bottom: 1px solid #eee;
  font-size: 14px;
  text-align: left;
}

.func-name-cell {
  display: flex;
  flex-direction: column;
}

.func-name {
  font-weight: 500;
  color: #333;
  margin-bottom: 4px;
}

.func-path {
  font-size: 12px;
  color: #666;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
}

/* 标签样式 */
.exception-tag {
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 12px;
  color: #fff;
  display: inline-block;
  border: none;
  min-width: 70px;
  text-align: center;
}

.exception-tag.danger {
  background: #d9534f;
}

.exception-tag.warning {
  background: #f0ad4e;
}

.knob-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.knob-tag {
  background: #e9ecef;
  color: #666;
  border: none;
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.3s;
}

.knob-tag:hover {
  background: #4a00e0;
  color: #fff;
  transform: translateY(-1px);
}

.impact-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}

.impact-value {
  font-weight: 500;
  color: #333;
  min-width: 40px;
}

.detail-btn {
  color: #4a00e0;
  font-weight: 500;
  transition: all 0.3s;
}

.detail-btn:hover {
  color: #3a00b3;
  transform: translateX(2px);
}

/* 描述列表样式 */
.custom-descriptions {
  margin-top: 10px;
}

:deep(.custom-descriptions .el-descriptions__label) {
  background: #f8f9fa;
  font-weight: 500;
  color: #333;
}

:deep(.custom-descriptions .el-descriptions__content) {
  color: #666;
}

.path-tag {
  background: #e9ecef;
  color: #666;
  border: none;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 11px;
}

.highlight-value {
  color: #4a00e0;
  font-weight: 500;
}

.time-ratio {
  width: 80%;
}

.description-box {
  padding: 12px;
  background: #f8f9fa;
  border-radius: 6px;
  border-left: 3px solid #4a00e0;
  font-size: 14px;
  line-height: 1.5;
}

.suggestion {
  color: #d9534f;
  font-weight: 500;
}

.action-buttons {
  margin-top: 20px;
  display: flex;
  gap: 10px;
}

/* 按钮样式 */
.primary-btn,
.primary-btn-lg {
  background: #4a00e0;
  color: #fff;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.3s;
}

.primary-btn:hover,
.primary-btn-lg:hover {
  background: #3a00b3;
  transform: translateY(-1px);
  box-shadow: 0 4px 8px rgba(58, 0, 179, 0.2);
}

.secondary-btn,
.secondary-btn-lg {
  background: #f5f7fb;
  color: #666;
  border: 1px solid #ddd;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.3s;
}

.secondary-btn:hover,
.secondary-btn-lg:hover {
  background: #e9ecef;
  border-color: #ccc;
}

.primary-btn-lg {
  padding: 10px 24px;
  font-size: 16px;
}

.secondary-btn-lg {
  padding: 10px 24px;
  font-size: 16px;
}

/* 参数建议表格样式 */
.knob-name {
  font-weight: 500;
  color: #333;
}

.current-value {
  color: #666;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
}

.suggested-value {
  color: #4a00e0;
  font-weight: 500;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
}

.success {
  background: #5cb85c;
  color: #fff;
  border: none;
  padding: 2px 8px;
  border-radius: 10px;
}

/* 按钮组 */
.btn-group {
  margin-top: 30px;
  text-align: center;
  display: flex;
  justify-content: center;
  gap: 20px;
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

/* 进度条样式 */
:deep(.el-progress-bar__outer) {
  border-radius: 4px;
}

:deep(.el-progress-bar__inner) {
  border-radius: 4px;
}

/* 响应式调整 */
@media (max-width: 768px) {
  .page-container {
    padding: 15px;
    margin: 10px;
  }

  .knob-tags {
    flex-direction: column;
  }

  .btn-group {
    flex-direction: column;
    align-items: center;
  }

  .time-ratio {
    width: 100%;
  }

  .impact-cell {
    flex-direction: column;
    align-items: flex-start;
    gap: 5px;
  }
}

/* 新增的样式 */
.change-count {
  background: #4a00e0;
  color: #fff;
  padding: 2px 10px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: normal;
  margin-left: 10px;
}

.filter-stats {
  font-size: 12px;
  color: #666;
  margin-left: 10px;
  line-height: 32px;
}

.change-percent {
  font-size: 12px;
  font-weight: 500;
}

.positive {
  color: #5cb85c;
}

.negative {
  color: #d9534f;
}

.warning {
  color: #f0ad4e;
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
  gap: 8px;
}

.stats-label {
  color: #666;
  font-size: 13px;
}

.stats-value {
  font-weight: 500;
  font-size: 14px;
}

.success-btn-lg {
  background: #5cb85c;
  color: #fff;
}

.success-btn-lg:hover {
  background: #4cae4c;
  transform: translateY(-1px);
  box-shadow: 0 4px 8px rgba(92, 184, 92, 0.2);
}

.warning-btn-lg {
  background: #f0ad4e;
  color: #fff;
}

.warning-btn-lg:hover {
  background: #ec971f;
  transform: translateY(-1px);
  box-shadow: 0 4px 8px rgba(240, 173, 78, 0.2);
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

/* 性能卡片样式 */
.performance-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 20px;
  margin: 20px 0;
}

.performance-card {
  background: #fff;
  border-radius: 12px;
  padding: 20px;
  text-align: center;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
  border: 1px solid #e9ecef;
  transition: all 0.3s ease;
  position: relative;
  overflow: hidden;
}

.performance-card:hover {
  transform: translateY(-5px);
  box-shadow: 0 8px 20px rgba(0, 0, 0, 0.12);
}

.performance-card.highlight {
  border-top: 4px solid #4a00e0;
}

.performance-card.success {
  border-top: 4px solid #5cb85c;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 15px;
}

.card-icon {
  width: 40px;
  height: 40px;
  background: linear-gradient(135deg, #4a00e0, #8e2de2);
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-right: 10px;
}

.performance-card.highlight .card-icon {
  background: linear-gradient(135deg, #4a00e0, #8e2de2);
}

.performance-card.success .card-icon {
  background: linear-gradient(135deg, #5cb85c, #4cae4c);
}

.card-icon i {
  color: #fff;
  font-size: 20px;
}

.card-title {
  font-size: 14px;
  color: #666;
  font-weight: 500;
}

.card-value {
  font-size: 28px;
  font-weight: 600;
  color: #333;
  margin: 10px 0;
  line-height: 1.2;
}

.performance-card.highlight .card-value {
  color: #4a00e0;
}

.performance-card.success .card-value {
  color: #5cb85c;
}

.card-subtitle {
  font-size: 12px;
  color: #999;
  margin-top: 5px;
}

.card-change {
  font-size: 13px;
  color: #5cb85c;
  font-weight: 500;
  margin-top: 8px;
  background: rgba(92, 184, 92, 0.1);
  padding: 4px 8px;
  border-radius: 12px;
  display: inline-block;
}

/* 响应式调整 */
@media (max-width: 1200px) {
  .performance-cards {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 768px) {
  .performance-cards {
    grid-template-columns: 1fr;
  }
}

/* 调优理由样式 */
.tuning-reasoning {
  margin-top: 25px;
  background: linear-gradient(135deg, #f8f9fa, #e9ecef);
  border-radius: 12px;
  padding: 20px;
  border-left: 4px solid #4a00e0;
  position: relative;
  overflow: hidden;
}

.tuning-reasoning:before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 4px;
  background: linear-gradient(90deg, #4a00e0, #8e2de2);
}

.reasoning-header {
  display: flex;
  align-items: center;
  margin-bottom: 15px;
}

.reasoning-header i {
  color: #4a00e0;
  font-size: 20px;
  margin-right: 10px;
}

.reasoning-title {
  font-size: 16px;
  font-weight: 600;
  color: #4a00e0;
}

.reasoning-content {
  color: #555;
  line-height: 1.6;
  font-size: 14px;
  padding: 10px;
  background: rgba(255, 255, 255, 0.8);
  border-radius: 8px;
  border: 1px solid #e9ecef;
}
</style>