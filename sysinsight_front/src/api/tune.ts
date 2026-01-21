import axios from 'axios'

// 使用 axios 提供的类型
type AxiosResponse<T = any> = import('axios').AxiosResponse<T>
type AxiosError = import('axios').AxiosError

// 定义请求和响应的接口类型
export interface LoadConfig {
    loadType: string
    duration: number
    threadNum: number
    timeRange?: [string, string]
}

export interface TuneConfig {
    tuneName: string
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
    taskName: string
}

export interface StartTuneResponse {
    code: number
    msg: string
    status: string
}

// 异常函数数据类型
export interface ExceptionFunctionItem {
    funcName: string
    sampleRate: number
    diffFromMean: number
    change: number
}

export interface ExceptionFunctionResponse {
    code: number
    msg: string
    data: ExceptionFunctionItem[]
}

// 任务状态接口
export interface TuneStatusResponse {
    code: number
    msg: string
    status: string
}

// 一步调优结果接口
export interface OneStepResult {
    base_config: Record<string, any>
    recommended_config: Record<string, any>
    base_score: number
    predicted_score: number
    delta_score: number
    reasoning: string
}

export interface OneStepResultResponse {
    code: number
    msg: string
    data: {
        records: OneStepResult
    }
}

// 每一轮概要信息（用于轮次选择）
export interface TuneRoundSummary {
    round: number
    tps: number
    latency: number
    improvement: number
}

// 每个参数的展示结构（直接绑定你现有表格）
export interface TuneParamItem {
    paramName: string
    originalValue: number | string
    currentValue: number | string
    changeType: '提升' | '降低' | '不变'
    desc?: string
    riskLevel?: 'low' | 'medium' | 'high'
}

// 接口整体返回
export interface MultiStepResultResponse {
    code: number
    msg: string
    data: {
        rounds: TuneRoundSummary[]
        roundParams: Record<string, TuneParamItem[]>
    }
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

    async getTuneStatus(taskName: string): Promise<TuneStatusResponse> {
        try {
            const response = await apiClient.get<TuneStatusResponse>('/tune/oneStepStatus', {
                params: { taskName }
            })
            return response.data
        } catch (error) {
            console.error('获取调优状态失败:', error)
            throw error
        }
    },

    async getMultiTuneStatus(taskName: string): Promise<TuneStatusResponse> {
        try {
            const response = await apiClient.get<TuneStatusResponse>('/tune/multiTuneStatus', {
                params: { taskName }
            })
            return response.data
        } catch (error) {
            console.error('获取调优状态失败:', error)
            throw error
        }
    },

    async getExceptionFunction(taskName: string, topN: number = 10): Promise<ExceptionFunctionResponse> {
        try {
            const response = await apiClient.get<ExceptionFunctionResponse>(
                '/tune/exception-functions',
                {
                    params: {
                        taskName: taskName,
                        topN: topN
                    }
                }
            )
            return response.data
        } catch (error) {
            console.error('获取异常函数失败:', error)
            throw error
        }
    },

    async startOneStepTune(data: StartTuneRequest): Promise<StartTuneResponse> {
        try {
            const response = await apiClient.post<StartTuneResponse>(
                '/tune/oneStep',
                data
            )
            return response.data
        } catch (error) {
            console.error('启动一步调优失败:', error)
            throw error
        }
    },

    async getOneStepResult(taskName: string): Promise<OneStepResultResponse> {
        try {
            // 尝试使用GET请求（推荐）
            const response = await apiClient.get<OneStepResultResponse>(
                '/tune/oneStepResult',
                {
                    params: { task_name: taskName }
                }
            )
            return response.data
        } catch (error: any) {
            // 如果GET失败，尝试使用POST（向后兼容）
            if (error.response && error.response.status === 405) {
                try {
                    const response = await apiClient.post<OneStepResultResponse>(
                        '/tune/oneStepResult',
                        null,
                        {
                            params: { task_name: taskName }
                        }
                    )
                    return response.data
                } catch (postError) {
                    console.error('获取一步调优结果失败（POST）:', postError)
                    throw postError
                }
            }
            console.error('获取一步调优结果失败:', error)
            throw error
        }
    },

    async applyTuneParams(taskName: string, params: Record<string, any>): Promise<any> {
        try {
            const response = await apiClient.post('/tune/applyParams', {
                taskName,
                params
            })
            return response.data
        } catch (error) {
            console.error('应用参数失败:', error)
            throw error
        }
    },

    // async startMultiRoundTune(data: { taskName: string, rounds: number, useSlave: boolean }): Promise<any> {
    //     try {
    //         const response = await apiClient.post('/tune/startMultiRound', data)
    //         return response.data
    //     } catch (error) {
    //         console.error('启动多轮调优失败:', error)
    //         throw error
    //     }
    // },

    async getMultiStepResult(taskName: string): Promise<MultiStepResultResponse> {
        try {
            // 推荐：GET
            const response = await apiClient.get<MultiStepResultResponse>(
                '/tune/getMultiTuneResult',
                {
                    params: { taskName: taskName }
                }
            )
            return response.data
        } catch (error: any) {
            // 向后兼容：POST
            if (error.response && error.response.status === 405) {
                try {
                    const response = await apiClient.post<MultiStepResultResponse>(
                        '/tune/getMultiTuneResult',
                        null,
                        {
                            params: { taskName: taskName }
                        }
                    )
                    return response.data
                } catch (postError) {
                    console.error('获取多轮调优结果失败（POST）:', postError)
                    throw postError
                }
            }

            console.error('获取多轮调优结果失败:', error)
            throw error
        }
    }


}

export default apiClient