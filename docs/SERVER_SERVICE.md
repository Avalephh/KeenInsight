# 服务器持久化运行

服务器上的 API 使用 systemd 管理，避免 SSH/终端断开后网页服务随之退出。

## 当前部署

- 服务：`keeninsight.service`
- 项目目录：`/root/keeninsight-postgres`
- 监听：`0.0.0.0:8000`
- 模型配置：`/etc/keeninsight/keeninsight.env`，权限应为 `600`
- 外部访问：`http://123.56.83.97:8000/console/home#overview`
- 流程演示：`http://123.56.83.97:8000/console/home#workloads`

密钥只放在服务器环境文件中，不写入 Git、静态页面、RAG 语料或审计记录。

## 安装或更新服务

从项目目录执行：

```bash
sudo install -D -m 0644 deploy/keeninsight.service \
  /etc/systemd/system/keeninsight.service
sudo systemctl daemon-reload
sudo systemctl enable --now keeninsight.service
```

环境文件至少包含以下非敏感配置；token 从密钥管理器或受保护的终端输入，
不要提交到仓库：

```text
KI_HOST=0.0.0.0
KI_PORT=8000
KI_RAG_LLM_URL=https://api.gpt.ge
KI_RAG_LLM_MODEL=gpt-4o-mini
KI_RAG_LLM_TOKEN=<secret>
```

## 检查和重启

```bash
systemctl status keeninsight.service --no-pager
systemctl is-enabled keeninsight.service
curl -fsS http://127.0.0.1:8000/api/health
ss -ltnp | grep ':8000'
sudo systemctl restart keeninsight.service
journalctl -u keeninsight.service -n 80 --no-pager
```

健康响应中的 `database=true`、`collector.first_sample_ready=true` 表示数据库和
持续采集均已就绪。若模型网关暂时不可达，RAG 会保留带引用的本地抽取式结果，
不会阻塞总览页和数据库监控。

## 访问失败时的排查顺序

1. 在服务器执行 `systemctl is-active keeninsight.service`。
2. 在服务器执行 `curl -fsS http://127.0.0.1:8000/api/health`。
3. 执行 `ss -ltnp | grep ':8000'`，确认监听地址为 `0.0.0.0:8000`。
4. 若本机可访问而公网不可访问，检查云安全组和主机防火墙是否放行 TCP 8000。
5. 浏览器使用完整地址；`#overview` 和 `#workloads` 是前端锚点，不会发送给服务器。
