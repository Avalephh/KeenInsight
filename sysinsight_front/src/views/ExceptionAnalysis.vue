<template>
  <el-loading v-if="loading" text="系统正在进行参数调优，请勿关闭或重复操作..." background="rgba(255, 255, 255, 0.8)" />
  <div class="page-container">
    <div class="page-title">第三步：异常函数 & 关联Knob分析</div>

    <!-- 异常函数表格 -->
    <div class="table-section">
      <div class="section-title">异常函数列表</div>
      <el-table :data="exceptionFuncs" border class="custom-table">
        <el-table-column prop="funcName" label="异常函数名" width="200">
          <template #default="{ row }">
            <div class="func-name-cell">
              <span class="func-name">{{ row.funcName }}</span>
              <div class="func-path">{{ row.path }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="type" label="异常类型" width="150">
          <template #default="{ row }">
            <el-tag :class="row.type === '性能瓶颈' ? 'warning' : 'danger'" class="exception-tag">
              {{ row.type }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="relatedKnobs" label="关联Knob参数" min-width="300">
          <template #default="{ row }">
            <div class="knob-tags">
              <el-tag v-for="knob in row.relatedKnobs" :key="knob" class="knob-tag" @click="viewKnobDetail(knob)">
                {{ knob }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="影响度" width="120" align="center">
          <template #default="{ row }">
            <div class="impact-cell">
              <el-progress :percentage="row.impact" :stroke-width="8" :color="row.impact > 50 ? '#d9534f' : '#f0ad4e'"
                :show-text="false" />
              <span class="impact-value">{{ row.impact }}%</span>
            </div>
          </template>
        </el-table-column>
        <!-- <el-table-column label="操作" width="120" align="center">
          <template #default="{ row }">
            <el-button 
              type="text" 
              @click="viewDetail(row)"
              class="detail-btn"
            >
              查看详情
            </el-button>
          </template>
        </el-table-column> -->
      </el-table>
    </div>

    <!-- 异常详情面板 -->
    <div class="form-section" v-if="showDetail">
      <div class="section-title">异常函数详情：{{ activeFunc?.funcName }}</div>
      <el-descriptions :column="2" border class="custom-descriptions">
        <el-descriptions-item label="函数路径">
          <el-tag size="small" class="path-tag">{{ activeFunc?.path }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="调用频率">
          <span class="highlight-value">{{ activeFunc?.callRate }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="耗时占比">
          <div class="time-ratio">
            <el-progress :percentage="parseFloat(activeFunc?.timeRatio || '0')" :stroke-width="12" color="#4a00e0"
              :format="(percentage) => `${percentage}%`" />
          </div>
        </el-descriptions-item>
        <el-descriptions-item label="建议调整方向">
          <span class="suggestion">{{ activeFunc?.suggestion }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="异常描述" :span="2">
          <div class="description-box">
            {{ activeFunc?.desc }}
          </div>
        </el-descriptions-item>
      </el-descriptions>

      <div class="action-buttons">
        <el-button type="primary" @click="analyzeFurther" class="primary-btn">
          深度分析
        </el-button>
        <el-button @click="showDetail = false" class="secondary-btn">
          收起详情
        </el-button>
      </div>
    </div>

    <!-- 关联参数建议 -->
    <div class="form-section" v-if="showDetail">
      <div class="section-title">参数调整建议</div>
      <el-table :data="knobSuggestions" border class="custom-table">
        <el-table-column prop="name" label="参数名称" width="200">
          <template #default="{ row }">
            <span class="knob-name">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="currentValue" label="当前值" width="120">
          <template #default="{ row }">
            <span class="current-value">{{ row.currentValue }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="suggestedValue" label="建议值" width="120">
          <template #default="{ row }">
            <span class="suggested-value">{{ row.suggestedValue }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="impact" label="预期提升" width="120">
          <template #default="{ row }">
            <el-tag class="success" size="small">{{ row.impact }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="reason" label="调整原因"></el-table-column>
      </el-table>
    </div>

    <div class="btn-group">
      <el-button @click="toSystemStatus" class="secondary-btn-lg">
        返回上一步
      </el-button>
      <el-button type="primary" size="large" :loading="tuneStatus === 'running'" :disabled="tuneStatus === 'running'"
        @click="toParamRecommend" class="primary-btn-lg">
        {{ tuneStatus === 'running' ? '参数调优中...' : '开启参数推荐' }}
      </el-button>

    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import axios from 'axios'
import { tuneAPI, type StartTuneRequest, } from '@/api/tune'

const router = useRouter();
const route = useRoute()

const taskId = computed(() => route.query.taskId as string || '')
const taskName = computed(() => route.query.taskName as string || '')

if (!taskId.value) {
  ElMessage.warning('未检测到任务信息，请重新提交配置')
}
const tuneStatus = ref<'idle' | 'running' | 'success' | 'failed'>('idle')
const loading = computed(() => tuneStatus.value === 'running')
let timer: number | null = null



// 异常函数数据
const exceptionFuncs = ref([
  {
    funcName: 'innodb_purge_thread',
    type: '性能瓶颈',
    impact: 65.8,
    relatedKnobs: ['innodb_purge_threads', 'innodb_purge_batch_size', 'innodb_max_purge_lag'],
    desc: 'purge线程处理速度过慢，导致undo日志堆积，影响事务提交效率。在高并发写场景下，undo日志清理不及时会导致系统性能下降。',
    path: '/storage/innodb/purge/purge_thread.cc',
    callRate: '120次/秒',
    timeRatio: '45',
    suggestion: '提升purge线程数，增大batch size，优化undo日志清理策略'
  },
  {
    funcName: 'mysql_lock_wait',
    type: '死锁风险',
    impact: 32.1,
    relatedKnobs: ['innodb_lock_wait_timeout', 'transaction_isolation', 'innodb_deadlock_detect'],
    desc: '锁等待超时频繁触发，存在死锁风险，导致事务回滚率升高。多个事务同时竞争相同资源时容易产生死锁。',
    path: '/sql/lock/lock_wait.cc',
    callRate: '85次/秒',
    timeRatio: '28',
    suggestion: '调整锁等待超时时间，优化事务隔离级别，启用死锁检测'
  },
  {
    funcName: 'buffer_pool_read',
    type: 'IO瓶颈',
    impact: 45.3,
    relatedKnobs: ['innodb_buffer_pool_size', 'innodb_buffer_pool_instances', 'innodb_read_io_threads'],
    desc: '缓冲池命中率低，频繁触发磁盘读取操作，导致查询延迟增加。数据页未能有效缓存到内存中。',
    path: '/storage/innodb/buf/buf0buf.cc',
    callRate: '350次/秒',
    timeRatio: '32',
    suggestion: '增加缓冲池大小，优化缓冲池实例数量，提升IO线程数'
  },
  {
    funcName: 'log_flush_to_disk',
    type: '同步延迟',
    impact: 28.7,
    relatedKnobs: ['innodb_flush_log_at_trx_commit', 'sync_binlog', 'innodb_flush_method'],
    desc: '日志刷盘操作频繁，在高并发事务场景下产生明显的同步延迟，影响事务提交性能。',
    path: '/storage/innodb/log/log0write.cc',
    callRate: '200次/秒',
    timeRatio: '18',
    suggestion: '调整日志刷盘策略，优化事务提交模式，使用更高效的刷盘方法'
  }
])

const showDetail = ref(false)
const activeFunc = ref<(typeof exceptionFuncs.value)[0] | null>(null)

// 计算关联的参数建议
const knobSuggestions = computed(() => {
  if (!activeFunc.value) return []

  const suggestions = []
  const func = activeFunc.value

  if (func.funcName === 'innodb_purge_thread') {
    suggestions.push(
      {
        name: 'innodb_purge_threads',
        currentValue: '4',
        suggestedValue: '8',
        impact: '15-20%',
        reason: '增加purge线程数可以加速undo日志清理，减少事务提交等待时间'
      },
      {
        name: 'innodb_purge_batch_size',
        currentValue: '300',
        suggestedValue: '1000',
        impact: '10-15%',
        reason: '增大batch size可以提升单次清理效率，减少清理操作次数'
      }
    )
  } else if (func.funcName === 'mysql_lock_wait') {
    suggestions.push(
      {
        name: 'innodb_lock_wait_timeout',
        currentValue: '50',
        suggestedValue: '30',
        impact: '5-10%',
        reason: '适当减少锁等待超时时间，可以更快释放被锁定的资源'
      },
      {
        name: 'transaction_isolation',
        currentValue: 'REPEATABLE-READ',
        suggestedValue: 'READ-COMMITTED',
        impact: '8-12%',
        reason: '降低事务隔离级别可以减少锁竞争，提高并发性能'
      }
    )
  }

  return suggestions
})

// 查看详情
const viewDetail = (func: (typeof exceptionFuncs.value)[0]) => {
  activeFunc.value = func
  showDetail.value = true
  // 滚动到详情区域
  setTimeout(() => {
    const detailSection = document.querySelector('.form-section')
    if (detailSection) {
      detailSection.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, 100)
}

// 查看Knob参数详情
const viewKnobDetail = (knobName: string) => {
  ElMessage.info(`查看参数 ${knobName} 的详细说明`)
  // 这里可以添加跳转到参数详情页面的逻辑
}

// 深度分析
const analyzeFurther = () => {
  if (!activeFunc.value) return
  ElMessage.success(`开始对 ${activeFunc.value.funcName} 进行深度分析...`)
  // 这里可以添加深度分析逻辑
}

// 跳转页面
const toSystemStatus = () => {
  router.push({ name: 'system-status' })
}
// TODO 传递任务名称，作为启动调优的配置文件
// const toParamRecommend = () => {
//   // 参数推荐接口
//   router.push({
//     name: 'knob-recommend',
//     query: {
//       taskId: taskId.value,
//       taskName: taskName.value
//     }
//   })
// }

const restoreTuneStatus = () => {
  const saved = localStorage.getItem(`tune-status-${taskId.value}`)
  if (saved === 'running') {
    tuneStatus.value = 'running'
    startPolling()
  }
}

onMounted(() => {
  restoreTuneStatus()
})

onBeforeUnmount(() => {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
})

const startPolling = () => {
  timer = window.setInterval(async () => {
    try {
      const statusRes = await tuneAPI.getTuneStatus(taskName.value)

      if (statusRes.status === 'success') {
        finishTune('success')
      }

      if (statusRes.status === 'failed') {
        finishTune('failed')
      }
    } catch (e) {
      console.error('轮询调优状态失败', e)
    }
  }, 3000)
}

const finishTune = (status: 'success' | 'failed') => {
  if (timer) clearInterval(timer)

  tuneStatus.value = status
  localStorage.removeItem(`tune-status-${taskId.value}`)

  if (status === 'success') {
    ElMessage.success('调优完成，进入参数推荐页面')

    router.push({
      name: 'knob-recommend',
      query: {
        taskId: taskId.value,
        taskName: taskName.value
      }
    })
  } else {
    ElMessage.error('调优失败，请检查后端日志')
  }
}


const toParamRecommend = async () => {
  if (tuneStatus.value === 'running') return

  tuneStatus.value = 'running'
  localStorage.setItem(`tune-status-${taskId.value}`, 'running')

  try {
    const response = await tuneAPI.startTune({
      taskId: taskId.value,
      taskName: taskName.value
    })

    if (response.code === 200) {
      ElMessage({
        message: '调优任务已启动，系统正在持续分析中...',
        duration: 3000,
        type: 'success'
      })

      startPolling()
    } else {
      tuneStatus.value = 'idle'
      localStorage.removeItem(`tune-status-${taskId.value}`)
      ElMessage.error(response.msg)
    }
  } catch (error) {
    tuneStatus.value = 'idle'
    localStorage.removeItem(`tune-status-${taskId.value}`)
    ElMessage.error('启动调优失败，请检查后端服务')
  }
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
</style>