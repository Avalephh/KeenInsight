<template>
  <div class="page-container">
    <!-- 页面标题 -->
    <!-- <header>
      <h1>数据库状态展示</h1>
      <p>实时监控数据库性能状态，及时诊断和分析异常。</p>
    </header> -->

    <main>
      <!-- 数据库监控图表 -->
      <div class="section">
        <div class="page-title">第二步：数据库环境监控</div>
        <div class="grafana-frames">
          <iframe
            src="http://10.77.110.147:3000/d-solo/MQWgroiiz/mysql-overview?var-interval=$__auto&orgId=1&timezone=browser&var-host=mysql-exporter:9104&refresh=1m&theme=light&panelId=panel-13&__feature.dashboardSceneSolo=true"
            class="grafana-frame"
            frameborder="0"
          ></iframe>
          <iframe
            src="http://10.77.110.147:3000/d-solo/MQWgroiiz/mysql-overview?var-interval=$__auto&orgId=1&timezone=browser&var-host=mysql-exporter:9104&refresh=1m&theme=light&panelId=panel-51&__feature.dashboardSceneSolo=true"
            class="grafana-frame"
            frameborder="0"
          ></iframe>
          <iframe
            src="http://10.77.110.147:3000/d-solo/MQWgroiiz/mysql-overview?var-interval=$__auto&orgId=1&timezone=browser&var-host=mysql-exporter:9104&refresh=1m&theme=light&panelId=panel-50&__feature.dashboardSceneSolo=true"
            class="grafana-frame"
            frameborder="0"
          ></iframe>
        </div>
      </div>

      <!-- 异常分析按钮 -->
      <div class="btn-group">
        <button @click="goBack" class="primary-btn-lg">返回上一步</button>
        <button @click="analyzeException" class="secondary-btn">分析异常</button>
      </div>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'

const router = useRouter();
const route = useRoute()

const taskId = computed(() => route.query.taskId as string || '')
const taskName = computed(() => route.query.taskName as string || '')

if (!taskId.value) {
  ElMessage.warning('未检测到任务信息，请重新提交配置')
}

const goBack = () => {
  // 这里可以实现返回上一步的逻辑
  // console.log("返回上一步");
  router.push({ name: 'load-select' })
};


// TODO 传递任务名称，作为启动调优的配置文件
const analyzeException = () => {
  if (!taskId.value) {
    ElMessage.warning('任务信息缺失，无法进行异常分析')
    return
  }

  router.push({
    name: 'exception-analysis',
    query: {
      taskId: taskId.value,
      taskName: taskName.value
    }
  })
}
</script>

<style scoped>
.page-container {
  background: #fff;
  border-radius: 10px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
  padding: 26px 34px;
  max-width: 1800px;
  margin: 20px auto;
  min-height: auto;
}

header {
  background: linear-gradient(135deg, #8e2de2, #4a00e0);
  color: #fff;
  padding: 22px 32px;
}

header h1 {
  margin: 0;
  font-size: 22px;
}

header p {
  margin-top: 6px;
  font-size: 14px;
  opacity: 0.9;
}

.main {
  padding: 26px 34px;
}

.page-title {
  font-size: 22px;
  font-weight: 600;
  color: #4a00e0;
  margin-bottom: 20px;
  padding-bottom: 10px;
  border-bottom: 2px solid #f4f6f9;
}

.section {
  background: #fff;
  border-radius: 10px;
  padding: 20px 24px;
  margin-bottom: 24px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
}

.section h2 {
  margin-top: 0;
  font-size: 18px;
  border-left: 4px solid #4a00e0;
  padding-left: 10px;
}

.grafana-frames {
  display: flex;
  gap: 20px;
}

.grafana-frame {
  width: 450px;
  height: 350px;
  border-radius: 6px;
}

.btn-group {
  display: flex;
  gap: 20px;
  justify-content: center;
  margin-top: 20px;
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
