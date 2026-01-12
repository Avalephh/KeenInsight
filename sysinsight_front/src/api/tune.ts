import axios from 'axios'

// 使用 axios 提供的类型
type AxiosResponse<T = any> = import('axios').AxiosResponse<T>
type AxiosError = import('axios').AxiosError

// 或者更简单的方式：直接使用 any 类型（不推荐，但可以快速解决问题）
// type AxiosResponse<T = any> = any
// type AxiosError = any

// 定义请求和响应的接口类型
export interface LoadConfig {
    loadType: string
    duration: number
    threadNum: number
    timeRange?: [string, string]
}

export interface TuneConfig {
    tuneName: string
    algorithm: string
    rounds: number
    metrics: string[]
    selectedParams?: string[]
    paramRanges?: Record<string, {
        minValue: string
        maxValue: string
        unit: string
    }>
}

export interface SetConfigRequest {
    load?: LoadConfig
    tune?: TuneConfig
}

export interface SetConfigResponse {
    code: number
    msg: string
    task_id?: string
    ini_file?: string
    details?: string
    output?: string
}

export interface TaskInfo {
    id: string
    name: string
    configFile: string
    timestamp: string
    status?: string
}

export interface StartTuneRequest {
    taskId: string
    taskName: string
}

export interface StartTuneResponse {
    code: number
    msg: string
    task_id: string
    status: string
}

// 创建axios实例
const apiClient = axios.create({
    baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
    headers: {
        'Content-Type': 'application/json',
    },
    timeout: 30000,
})

// 请求拦截器
apiClient.interceptors.request.use(
    (config) => {
        console.log(`请求 ${config.method?.toUpperCase()} ${config.url}`)
        return config
    },
    (error: AxiosError) => {
        console.error('请求错误:', error)
        return Promise.reject(error)
    }
)

// 响应拦截器
apiClient.interceptors.response.use(
    (response: AxiosResponse) => {
        return response
    },
    (error: AxiosError) => {
        console.error('响应错误:', error)

        if (error.response) {
            console.error('错误状态码:', error.response.status)
            console.error('错误数据:', error.response.data)
        } else if (error.request) {
            console.error('无响应:', error.request)
        } else {
            console.error('请求配置错误:', error.message)
        }

        return Promise.reject(error)
    }
)

// API函数
export const tuneAPI = {
    async setConfig(data: SetConfigRequest): Promise<SetConfigResponse> {
        try {
            const response = await apiClient.post<SetConfigResponse>('/tune/setConfig', data)
            return response.data
        } catch (error) {
            console.error('设置配置失败:', error)
            throw error
        }
    },

    async startTune(data: StartTuneRequest): Promise<StartTuneResponse> {
        try {
            const response = await apiClient.post<StartTuneResponse>(
                '/tune/startTune',
                data
            )
            return response.data
        } catch (error) {
            console.error('启动调优失败:', error)
            throw error
        }
    },

    async getTuneStatus(taskName: string) {
        const response = await apiClient.get('/tune/status', {
            params: { taskName }
        })
        return response.data
    }
}

export default apiClient