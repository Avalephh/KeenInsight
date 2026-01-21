<template>
  <el-loading v-if="loading" text="系统正在进行参数调优，请勿关闭或重复操作..." background="rgba(255, 255, 255, 0.8)" />
  <div class="page-container">
    <div class="page-title">异常函数及关联参数分析</div>

    <!-- 异常函数表格 -->
    <div class="table-section">
      <div class="section-title">异常函数列表</div>
      <el-table :data="exceptionFuncs" border class="custom-table">
        <el-table-column prop="funcName" label="异常函数名" width="250" align="center">
          <template #default="{ row }">
            <div class="func-name-cell">
              <span class="func-name">{{ row.funcName }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="sampleRate" label="CPU占比" width="250" align="center">
          <template #default="{ row }">
            <div class="impact-cell">
              <el-progress :percentage="row.sampleRate" :stroke-width="8"
                :color="row.sampleRate > 70 ? '#d9534f' : '#f0ad4e'" :show-text="false" />
              <span class="CPU-percentage">
                {{ row.sampleRate.toFixed(2) }}%
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="change" label="异常类型" width="250" align="center">
          <template #default="{ row }">
            <el-tag :class="row.change === 1 ? 'danger' : 'warning'" class="exception-tag">
              {{ row.change === 1 ? '函数占用较高' : '函数占用较低' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="关联参数" min-width="300" align="center">
          <template #default="{ row }">
            <div class="knob-tags">
              <el-tag v-for="knob in row.relatedKnobs" :key="knob" class="knob-tag">
                {{ knob }}
              </el-tag>
              <span v-if="!row.relatedKnobs || row.relatedKnobs.length === 0" class="no-knobs">暂无关联参数</span>
            </div>
          </template>
        </el-table-column>
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
import { tuneAPI } from '@/api/tune'

const router = useRouter();
const route = useRoute()

const taskId = computed(() => route.query.taskId as string || '')
const taskName = computed(() => route.query.taskName as string || '')

const tuneStatus = ref<'idle' | 'running' | 'success' | 'failed'>('idle')
const loading = computed(() => tuneStatus.value === 'running')
let timer: number | null = null

// 异常函数数据
const exceptionFuncs = ref<any[]>([])

// 跳转页面
const toSystemStatus = () => {
  router.push({
    name: 'system-status',
    query: {
      taskId: taskId.value,
      taskName: taskName.value
    }
  })
}

const restoreTuneStatus = async () => {
  const saved = localStorage.getItem(`tune-status-${taskId.value}`)
  if (saved === 'running') {
    try {
      const statusRes = await tuneAPI.getTuneStatus(taskName.value)

      if (statusRes.status === 'running') {
        tuneStatus.value = 'running'
        startPolling()
      } else if (statusRes.status === 'success') {
        // 如果已经成功，清除本地状态
        localStorage.removeItem(`tune-status-${taskId.value}`)
        tuneStatus.value = 'success'
      } else if (statusRes.status === 'failed') {
        localStorage.removeItem(`tune-status-${taskId.value}`)
        tuneStatus.value = 'failed'
        ElMessage.error('上一次调优任务失败')
      }
    } catch (error) {
      console.error('恢复状态失败:', error)
      localStorage.removeItem(`tune-status-${taskId.value}`)
    }
  }
}

onMounted(async () => {
  // 恢复调优状态
  await restoreTuneStatus()

  // 加载异常函数
  try {
    // 注意：这里使用了GET请求，参数在URL中
    const res = await tuneAPI.getExceptionFunction(taskName.value, 10)

    if (res.code === 200) {
      // 数据转换：确保类型正确
      exceptionFuncs.value = res.data.map((item: any) => ({
        funcName: item.funcName,
        sampleRate: Number(item.sampleRate) || 0,
        change: Number(item.change) || 0,
        // 如果没有关联参数，使用空数组
        relatedKnobs: item.relatedKnobs || []
      }))
    } else {
      ElMessage.error(res.msg || '获取异常函数失败')
    }
  } catch (e: any) {
    console.error('获取异常函数列表失败:', e)
    ElMessage.error(e.message || '获取异常函数列表失败')
  }
})

onBeforeUnmount(() => {
  stopPolling()
})

const stopPolling = () => {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
}

const startPolling = () => {
  if (timer) {
    clearInterval(timer)
  }

  timer = window.setInterval(async () => {
    try {
      const statusRes = await tuneAPI.getTuneStatus(taskName.value)
      console.log('轮询状态:', statusRes)

      if (statusRes.status === 'success') {
        finishTune('success')
      } else if (statusRes.status === 'failed') {
        finishTune('failed')
      } else if (statusRes.status === 'idle') {
        // 如果状态变为 idle，说明任务不存在或已清理
        console.log('任务状态变为 idle，停止轮询')
        stopPolling()
        tuneStatus.value = 'idle'
        localStorage.removeItem(`tune-status-${taskId.value}`)
      }
      // 如果还在 running，继续轮询
    } catch (e) {
      console.error('轮询调优状态失败', e)
    }
  }, 3000)
}

const finishTune = (status: 'success' | 'failed') => {
  stopPolling()

  tuneStatus.value = status
  localStorage.removeItem(`tune-status-${taskId.value}`)

  if (status === 'success') {
    ElMessage.success('调优完成，进入参数推荐页面')
    // 延迟跳转，让用户看到成功消息
    setTimeout(() => {
      router.push({
        name: 'knob-recommend',
        query: {
          taskId: taskId.value,
          taskName: taskName.value
        }
      })
    }, 1000)
  } else {
    ElMessage.error('调优失败，请检查后端日志')
  }
}

const toParamRecommend = async () => {
  if (tuneStatus.value === 'running') return

  tuneStatus.value = 'running'
  localStorage.setItem(`tune-status-${taskId.value}`, 'running')

  try {
    const response = await tuneAPI.startOneStepTune({
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
  } catch (error: any) {
    tuneStatus.value = 'idle'
    localStorage.removeItem(`tune-status-${taskId.value}`)
    console.error('启动调优失败:', error)
    ElMessage.error(error.message || '启动调优失败，请检查后端服务')
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