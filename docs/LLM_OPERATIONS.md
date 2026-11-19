# 外部模型接入

KeenInsight 使用一个可选的 OpenAI 兼容 Chat Completions 客户端。当前推荐
使用低成本的 `gpt-4o-mini`；模型名仍可通过环境变量替换。服务接受裸主机地址、
`/v1` 基地址，或完整的 `/v1/chat/completions` 地址，并自动规范化请求路径。

## 本机配置

不要把 token 写入 Git、静态页面、RAG 语料或审计载荷。启动 API 进程时设置：

```bash
export KI_RAG_LLM_URL=https://your-openai-compatible-gateway.example
export KI_RAG_LLM_MODEL=gpt-4o-mini
export KI_RAG_LLM_TOKEN='从密钥管理器读取的 token'
exec /usr/bin/python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

也可以复制 `.env.example` 到本机未提交的 `.env`，但必须确保文件权限和忽略规则
正确。接口状态可从 `GET /api/llm/status` 查看；只返回 endpoint、模型名、调用计数、
延迟、token 是否存在和脱敏错误，不返回 token。

## 接入位置和安全边界

- RAG 问答：只发送检索到的来源段落，回答必须包含有效 `[R#]` 引用，否则使用抽取式回答。
- 异常/工作负载诊断：只生成展示用的 `[D#]` 证据解释，不改变根因、动作或实测结果。
- PostgreSQL 源码知识：只根据 Clang/LLVM 定位的源码行生成 `[S#]` 摘要；数值和参数方向仍须实验验证。
- 查询调优智能体：模型只能返回白名单 session hint 候选；每项仍需结果等价性检查和同库测量。

模型输出不会被拼接执行为 SQL，也不会绕过现有 typed action、回滚和审核流程。外部
请求失败、返回空内容、JSON 无效或缺少引用时，会保留确定性路径并在 `/api/llm/status`
中显示脱敏的失败计数。
