<template>
  <div class="page-container">
    <!-- 加载状态遮罩 -->
    <div v-if="loading" class="loading-overlay">
      <div class="loading-spinner">
        <el-icon class="is-loading">
          <Loading />
        </el-icon>
        <span>正在提交配置，请稍候...</span>
      </div>
    </div>

    <div class="page-title">第一步：负载选择 & 调优配置</div>

    <!-- 负载选择区域 -->
    <div class="form-section">
      <div class="form-section-title">负载选择</div>
      <el-form 
        :model="loadForm" 
        label-width="200px" 
        inline
        ref="loadFormRef"
        :rules="loadRules"
      >
        <el-form-item label="负载类型" prop="loadType">
          <el-select 
            v-model="loadForm.loadType" 
            placeholder="请选择负载类型"
            clearable
            class="custom-select"
            style="width: 200px;"
          >
            <el-option label="TPC-C" value="tpcc"></el-option>
            <el-option label="TPC-H" value="tpch"></el-option>
            <el-option label="Sysbench" value="sysbench"></el-option>
            <el-option label="用户自定义负载" value="custom"></el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="负载时长" prop="duration">
          <el-input 
            v-model.number="loadForm.duration" 
            suffix="分钟" 
            class="custom-input"
            type="number"
            :min="1"
            placeholder="请输入时长"
          ></el-input>
        </el-form-item>
        <!-- 注释掉了线程数输入框 -->
        <!-- <el-form-item label="线程数" prop="threadNum">
          <el-input 
            v-model.number="loadForm.threadNum" 
            class="custom-input"
            type="number"
            :min="1"
            :max="1000"
            placeholder="请输入线程数"
          ></el-input>
        </el-form-item> -->
        <el-form-item label="时间范围" prop="timeRange">
          <el-date-picker
            v-model="loadForm.timeRange"
            type="datetimerange"
            range-separator="至"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            format="YYYY-MM-DD HH:mm:ss"
            value-format="YYYY-MM-DD HH:mm:ss"
            class="custom-date-picker"
          ></el-date-picker>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="checkLoadConfig" class="primary-btn">确认选择</el-button>
          <el-button @click="resetLoadForm" class="secondary-btn">重置</el-button>
          <!-- 使用 isDev 变量而不是 import.meta.env.DEV -->
          <!-- <el-button v-if="isDev" @click="testConnection" type="info" class="secondary-btn">
            测试连接
          </el-button> -->
        </el-form-item>
      </el-form>
    </div>

    <!-- 调优配置区域 -->
    <div class="form-section">
      <div class="form-section-title">调优方法 & 参数配置</div>
      <el-form 
        :model="tuneForm" 
        label-width="120px" 
        ref="tuneFormRef"
        :rules="tuneRules"
      >
        <el-row :gutter="20">
          <el-col :span="8">
            <!-- <el-form-item label="调优算法" prop="algorithm">
              <el-select 
                v-model="tuneForm.algorithm" 
                placeholder="请选择调优算法"
                clearable
                class="custom-select"
              >
                <el-option label="SMAC" value="SMAC"></el-option>
                <el-option label="SysInsight" value="sysinsight"></el-option>
                <el-option label="MBO" value="MBO"></el-option>
                <el-option label="贝叶斯优化" value="BO"></el-option>
              </el-select>
            </el-form-item> -->
            <el-form-item label="调优轮次" prop="rounds">
              <el-input-number 
                v-model.number="tuneForm.rounds" 
                :min="1" 
                :max="100" 
                class="custom-input-number"
                placeholder="请输入轮次"
              ></el-input-number>
            </el-form-item>
            <el-form-item label="监控指标" prop="metrics">
              <el-select 
                v-model="tuneForm.metrics" 
                multiple 
                placeholder="请选择监控指标"
                class="custom-select"
              >
                <el-option label="吞吐量(TPS)" value="tps"></el-option>
                <el-option label="延迟(Latency)" value="-lat"></el-option>
                <el-option label="CPU利用率" value="cpu"></el-option>
                <el-option label="内存使用率" value="virtualMem"></el-option>
                <el-option label="磁盘IO" value="IO"></el-option>
              </el-select>
            </el-form-item>
            <el-form-item label="调优名称" prop="tuneName">
              <el-input 
                v-model="tuneForm.tuneName" 
                placeholder="请输入调优名称"
                class="custom-input"
              ></el-input>
            </el-form-item>
          </el-col>
        </el-row>
        <!-- <el-form-item>
          <el-button type="primary" @click="submitTuneConfig" class="primary-btn" :loading="loading">
            提交配置
          </el-button>
          <el-button @click="resetTuneForm" class="secondary-btn">重置</el-button>
        </el-form-item> -->
      </el-form>
    </div>
    <div class="btn-group">
      <el-button 
        type="primary" 
        size="large" 
        @click="submitTuneConfig"
        class="primary-btn-lg"
        :loading="loading"
      >
        提交配置
      </el-button>
      <el-button @click="resetTuneForm" class="secondary-btn">重置</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import { tuneAPI, type LoadConfig, type TuneConfig, type SetConfigRequest, type TaskInfo } from '@/api/tune'

const router = useRouter()

// 状态管理
const loading = ref(false)

// 判断是否是开发环境
const isDev = import.meta.env.DEV

// 表单引用
const loadFormRef = ref<FormInstance>()
const tuneFormRef = ref<FormInstance>()

// 负载选择表单
const loadForm = reactive({
  loadType: '',
  duration: 30,
  threadNum: 32, 
  timeRange: [] as string[]
})

// 调优配置表单
const tuneForm = reactive({
  tuneName: '',
  // algorithm: 'SysInsight',
  rounds: 5,
  metrics: [] as string[],
  selectedParams: [] as string[],
  paramRanges: {} as Record<string, { minValue: string; maxValue: string; unit: string }>,
})


// 表单验证规则
const loadRules: FormRules = {
  loadType: [
    { required: true, message: '请选择负载类型', trigger: 'change' }
  ],
  duration: [
    { required: true, message: '请输入负载时长', trigger: 'blur' },
    { type: 'number', min: 1, message: '时长必须大于0', trigger: 'blur' }
  ],
  threadNum: [
    { required: false, message: '请输入线程数', trigger: 'blur' }, // 改为非必填
    { type: 'number', min: 1, max: 1000, message: '线程数必须在1-1000之间', trigger: 'blur' }
  ],
  timeRange: [
    { type: 'array', required: true, message: '请选择时间范围', trigger: 'change' }
  ]
}

const tuneRules: FormRules = {
  // algorithm: [
  //   { required: true, message: '请选择调优算法', trigger: 'change' }
  // ],
  rounds: [
    { required: true, message: '请输入调优轮次', trigger: 'blur' },
    { type: 'number', min: 1, max: 100, message: '轮次必须在1-100之间', trigger: 'blur' }
  ],
  metrics: [
    { type: 'array', required: true, message: '请选择至少一个监控指标', trigger: 'change' }
  ],
  tuneName: [
    { required: true, message: '请输入调优任务名称', trigger: 'blur' },
    { min: 3, max: 50, message: '任务名称长度在3-50个字符之间', trigger: 'blur' }
  ]
}


// 检查负载配置
const checkLoadConfig = async () => {
  if (!loadFormRef.value) return
  
  try {
    await loadFormRef.value.validate()
    ElMessage.success('负载选择成功')
  } catch (error) {
    ElMessage.warning('请检查负载配置')
  }
}

// 重置负载表单
const resetLoadForm = () => {
  loadFormRef.value?.resetFields()
  loadForm.duration = 30
  loadForm.threadNum = 10
  loadForm.timeRange = []
  ElMessage.success('负载表单已重置')
}

// 提交调优配置
const submitTuneConfig = async () => {
  if (!tuneFormRef.value || !loadFormRef.value) return
  
  loading.value = true
  
  try {
    // 验证两个表单
    const [tuneValid, loadValid] = await Promise.all([
      tuneFormRef.value.validate(),
      loadFormRef.value.validate()
    ]).catch(() => [false, false])

    if (!tuneValid || !loadValid) {
      ElMessage.warning('请检查表单填写是否正确')
      return
    }

    // 构建请求数据
    const requestData: SetConfigRequest = {
      load: {
        loadType: loadForm.loadType,
        duration: loadForm.duration,
        threadNum: loadForm.threadNum
        // timeRange 不传给后端
      },
      tune: {
        tuneName: tuneForm.tuneName,
        // algorithm: tuneForm.algorithm,
        rounds: tuneForm.rounds,
        metrics: tuneForm.metrics
      }
    }

    console.log('提交配置数据:', requestData)

    // 调用后端API
    const response = await tuneAPI.setConfig(requestData)
    // 压测一次展示数据到第二页
    

    if (response.code === 200) {
      ElMessage.success({
        message: '配置提交成功，正在评估系统...',
        duration: 2000
      })

      const taskId = response.task_id || tuneForm.tuneName

      // 保存任务信息（供后续页面 / 刷新使用）
      const taskInfo: TaskInfo = {
        id: taskId,
        name: tuneForm.tuneName,
        configFile: response.ini_file || `${tuneForm.tuneName}.ini`,
        timestamp: new Date().toISOString(),
        status: 'created'
      }

      localStorage.setItem('currentTask', JSON.stringify(taskInfo))

      // 🚀 关键：提交成功后直接跳转
      router.push({
        name: 'system-status',
        query: {
          taskId: taskId,
          taskName: tuneForm.tuneName
        }
      })
    } else {
      ElMessage.error(`配置提交失败: ${response.msg}`)
      if (response.details) {
        console.error('错误详情:', response.details)
      }
    }
    
  } catch (error: any) {
    console.error('提交配置失败:', error)
    
    // 处理不同类型的错误
    if (error.response) {
      // 后端返回了错误响应
      const errorData = error.response.data
      ElMessage.error(`服务器错误: ${errorData?.msg || error.message}`)
    } else if (error.request) {
      // 请求发送了但没有收到响应
      ElMessage.error('网络错误，请检查后端服务是否启动')
    } else {
      // 其他错误
      ElMessage.error(`请求失败: ${error.message}`)
    }
    
  } finally {
    loading.value = false
  }
}

// 重置调优表单
const resetTuneForm = () => {
  tuneFormRef.value?.resetFields()
  tuneForm.tuneName = ''
  // tuneForm.algorithm = ''
  tuneForm.rounds = 5
  tuneForm.metrics = []
  tuneForm.selectedParams = []
  tuneForm.paramRanges = {}
  // configCompleted.value = false
  ElMessage.success('调优表单已重置')
}

// // 跳转到系统状态页面
// const toSystemStatus = async () => {
//   if (!configCompleted.value) {
//     ElMessage.warning('请先完成配置提交')
//     return
//   }
  
//   navigating.value = true
  
//   try {
//     // 获取保存的任务信息
//     const savedTask = localStorage.getItem('currentTask')
//     if (!savedTask) {
//       ElMessage.warning('未找到任务信息，请重新提交配置')
//       return
//     }
    
//     const taskInfo: TaskInfo = JSON.parse(savedTask)
    
//     // 跳转到系统状态页面，传递任务信息
//     router.push({
//       name: 'system-status',
//       query: { 
//         taskId: taskInfo.id,
//         taskName: taskInfo.name 
//       }
//     })
    
//   } catch (error) {
//     console.error('跳转失败:', error)
//     ElMessage.error('跳转失败，请重试')
//   } finally {
//     navigating.value = false
//   }
// }

// // 测试后端连接（开发环境使用）
// const testConnection = async () => {
//   try {
//     const result = await tuneAPI.healthCheck()
//     ElMessage.success(`后端连接正常: ${result.status}`)
//   } catch (error) {
//     ElMessage.error('无法连接到后端服务')
//     console.error('连接测试失败:', error)
//   }
// }

// 组件挂载时恢复任务状态
// onMounted(() => {
//   const savedTask = localStorage.getItem('currentTask')
//   if (savedTask) {
//     try {
//       const taskInfo: TaskInfo = JSON.parse(savedTask)
//       console.log('恢复保存的任务:', taskInfo)
      
//       // 如果任务存在且状态正常，启用下一步按钮
//       if (taskInfo.id && taskInfo.name) {
//         configCompleted.value = true
//         tuneForm.tuneName = taskInfo.name
//       }
//     } catch (error) {
//       console.error('恢复任务失败:', error)
//     }
//   }
// })
</script>

<style scoped>
/* 样式保持不变，只添加加载样式 */
.loading-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(255, 255, 255, 0.8);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
}

.loading-spinner {
  text-align: center;
  padding: 30px;
  background: white;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.1);
}

.loading-spinner .el-icon {
  font-size: 40px;
  margin-bottom: 10px;
  color: #4a00e0;
}

.loading-spinner span {
  display: block;
  margin-top: 10px;
  color: #666;
}
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
  max-width: 1800px;
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

/* 表单区域样式 - 与home.html的section一致 */
.form-section {
  background: #fff;
  border-radius: 10px;
  padding: 20px 24px;
  margin-bottom: 24px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.06);
  border-left: 4px solid #4a00e0;
}

.form-section-title {
  font-size: 18px;
  font-weight: 500;
  color: #333;
  margin-bottom: 15px;
  padding-left: 10px;
  border-left: 4px solid #4a00e0;
}

/* 按钮样式 - 与home.html一致 */
.primary-btn, .primary-btn-lg {
  background: #4a00e0;
  color: #fff;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
}

.primary-btn:hover, .primary-btn-lg:hover {
  background: #3a00b3;
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
}

.primary-btn-lg {
  padding: 10px 24px;
  font-size: 16px;
}

.danger-btn {
  background: #d9534f;
  color: #fff;
  border: none;
  border-radius: 6px;
  cursor: pointer;
}

.danger-btn:hover {
  background: #c9302c;
}

/* 表单控件样式 */
:deep(.custom-select .el-input__inner) {
  border-radius: 6px;
  border: 1px solid #ddd;
  height: 36px;
  line-height: 36px;
}

:deep(.custom-input .el-input__inner) {
  border-radius: 6px;
  border: 1px solid #ddd;
  height: 36px;
  line-height: 36px;
}

:deep(.custom-input-number .el-input-number__decrease),
:deep(.custom-input-number .el-input-number__increase) {
  border-radius: 6px;
}

:deep(.custom-date-picker .el-range-input) {
  height: 36px;
}

/* 卡片样式 */
.custom-card {
  border-radius: 10px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.06);
  border: none;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 0;
}

.card-header-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}

.search-input {
  width: 200px;
}

.search-input :deep(.el-input__inner) {
  border-radius: 6px;
  border: 1px solid #ddd;
}

/* 参数列表样式 */
.param-list {
  max-height: 400px;
  overflow-y: auto;
  padding: 10px;
}

.param-checkbox {
  width: 100%;
  margin-bottom: 10px;
  padding: 8px;
  border-radius: 6px;
  border: 1px solid #eee;
  transition: all 0.3s;
}

.param-checkbox:hover {
  background: #f8f9fa;
  border-color: #4a00e0;
}

.param-item {
  padding: 8px;
  width: 100%;
}

.param-item-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}

.param-item-name {
  font-weight: 500;
  font-size: 14px;
  color: #333;
}

.category-tag {
  background: #e9ecef;
  color: #666;
  border: none;
  font-size: 10px;
}

/* 参数范围配置样式 */
.param-range-config {
  margin-top: 20px;
  padding: 15px;
  background: #f8fafc;
  border-radius: 8px;
  border: 1px solid #eee;
}

.range-config-title {
  font-weight: 500;
  margin-bottom: 10px;
  color: #333;
  font-size: 16px;
}

.custom-table {
  width: 100%;
  margin-top: 10px;
  border-collapse: collapse;
}

:deep(.custom-table th) {
  background: #f5f7fb;
  padding: 10px 12px;
  border-bottom: 1px solid #eee;
  font-size: 14px;
  text-align: left;
  font-weight: 600;
}

:deep(.custom-table td) {
  padding: 10px 12px;
  border-bottom: 1px solid #eee;
  font-size: 14px;
  text-align: left;
}

.param-name-cell {
  font-weight: 500;
  color: #333;
}

.param-desc-cell {
  font-size: 12px;
  color: #666;
  margin-top: 2px;
}

.range-inputs {
  display: flex;
  align-items: center;
  gap: 5px;
}

.range-input {
  width: 100px;
}

.range-input :deep(.el-input__inner) {
  border-radius: 6px;
  border: 1px solid #ddd;
  height: 30px;
  line-height: 30px;
}

.range-separator {
  margin: 0 5px;
  color: #666;
}

.batch-actions {
  margin-top: 15px;
  display: flex;
  gap: 10px;
}

/* 分页样式 */
.param-pagination {
  margin-top: 20px;
  text-align: right;
}

:deep(.param-pagination .el-pagination) {
  justify-content: flex-end;
}

/* 对话框样式 */
.custom-dialog :deep(.el-dialog) {
  border-radius: 10px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.06);
}

.custom-dialog :deep(.el-dialog__header) {
  border-bottom: 1px solid #eee;
  padding: 15px 20px;
}

.custom-dialog :deep(.el-dialog__title) {
  font-size: 18px;
  color: #333;
}

.custom-textarea :deep(.el-textarea__inner) {
  border-radius: 6px;
  border: 1px solid #ddd;
  font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
  font-size: 12px;
}

/* 按钮组 */
.btn-group {
  margin-top: 30px;
  text-align: center;
}

/* 响应式调整 */
@media (max-width: 768px) {
  .page-container {
    padding: 15px;
    margin: 10px;
  }
  
  .card-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
  }
  
  .card-header-actions {
    width: 100%;
    flex-wrap: wrap;
  }
  
  .range-inputs {
    flex-wrap: wrap;
  }
  
  .form-section-title {
    font-size: 16px;
  }
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
}

/* 标签样式 - 与home.html一致 */
.tag {
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 12px;
  color: #fff;
  display: inline-block;
}

.danger { background: #d9534f; }
.warning { background: #f0ad4e; }
.success { background: #5cb85c; }

/* 表单验证样式 */
:deep(.el-form-item.is-error .param-range-config) {
  border-color: #d9534f;
}

:deep(.el-form-item.is-error .el-input__inner),
:deep(.el-form-item.is-error .el-select .el-input__inner) {
  border-color: #d9534f;
}
</style>