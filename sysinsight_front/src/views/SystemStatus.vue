<template>
  <el-loading v-if="loading" text="系统正在获取评估结果，请稍等..." background="rgba(255, 255, 255, 0.8)" />
  <div class="page-container">
    <div class="page-title">第二步：评估结果展示</div>

    <!-- 评估结果展示 -->
    <div class="result-section">
      <div class="section-title">评估结果</div>
      <div class="result-item">
        <span class="result-label">TPS：</span>
        <span class="result-value">{{ tps || '未获取到数据' }}</span>
      </div>
      <div class="result-item">
        <span class="result-label">QPS：</span>
        <span class="result-value">{{ qps || '未获取到数据' }}</span>
      </div>
      <div class="result-item">
        <span class="result-label">Latency：</span>
        <span class="result-value">{{ latency || '未获取到数据' }}</span>
      </div>
    </div>

    <!-- 异常分析按钮 -->
    <div class="btn-group">
      <button @click="goBack" class="primary-btn-lg">返回上一步</button>
      <button @click="analyzeException" class="secondary-btn">分析异常</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import axios from 'axios'

const router = useRouter()
const route = useRoute()

const taskId = computed(() => route.query.taskId as string || '')
const taskName = computed(() => route.query.taskName as string || '')

if (!taskId.value) {
  ElMessage.warning('未检测到任务信息，请重新提交配置')
}

const loading = ref(false)
const tps = ref<number | null>(null)
const qps = ref<number | null>(null)
const latency = ref<number | null>(null)

const fetchEvaluationResults = async () => {
  loading.value = true
  try {
    const response = await axios.get(`/api/tune/firstEvaluateResults`, {
      params: { taskName: taskName.value },
    })

    const resultData = response.data.data
    tps.value = resultData.tps || null
    qps.value = resultData.qps || null
    latency.value = resultData.latency || null

    loading.value = false
  } catch (error) {
    loading.value = false
    ElMessage.error('获取评估结果失败，请检查后端服务')
  }
}

const goBack = () => {
  router.push({ name: 'load-select' })
}

const analyzeException = async () => {
  if (!taskId.value) {
    ElMessage.warning('任务信息缺失，无法进行异常分析')
    return
  }

  // 获取最新的评估结果
  await fetchEvaluationResults()

  router.push({
    name: 'exception-analysis',
    query: {
      taskId: taskId.value,
      taskName: taskName.value,
    },
  })
}

onMounted(() => {
  // 页面加载时自动获取评估结果
  fetchEvaluationResults()
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

/* 结果展示区样式 */
.result-section {
  background: #fff;
  border-radius: 10px;
  padding: 20px 24px;
  margin-bottom: 24px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
}

.section-title {
  font-size: 18px;
  font-weight: 500;
  color: #333;
  margin-bottom: 15px;
  padding-left: 10px;
  border-left: 4px solid #4a00e0;
}

.result-item {
  font-size: 16px;
  margin-bottom: 10px;
}

.result-label {
  font-weight: 600;
  color: #333;
}

.result-value {
  color: #4a00e0;
  font-weight: 500;
}

.btn-group {
  margin-top: 20px;
  display: flex;
  gap: 20px;
  justify-content: center;
}

button {
  padding: 8px 16px;
  border-radius: 6px;
  border: none;
  cursor: pointer;
  font-size: 14px;
}

.primary-btn-lg {
  background: #4a00e0;
  color: white;
  font-size: 16px;
}

.secondary-btn {
  background: #f5f7fb;
  color: #666;
  border: 1px solid #ddd;
  font-size: 14px;
}

button:hover {
  opacity: 0.8;
}
</style>
