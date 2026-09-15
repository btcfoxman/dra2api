# API 与管理端

外部调用使用 `Authorization: Bearer <DRA_API_KEY>` 或 `X-API-Key`；管理端使用登录后的 HttpOnly Cookie；账号同步使用 `DRA_SYNC_TOKEN`（兼容 API key）。

## 视频接口

| 请求 | 用途 |
| --- | --- |
| `GET /v1/models` | 模型、别名、规格及验证程度 |
| `POST /v1/videos` | 创建视频，默认异步 |
| `POST /v1/videos/generations` | 同上 |
| `POST /api/videos/generate` | 同上 |
| `GET /v1/videos/{id}` | 查询持久化状态与结果 |
| `GET /api/videos/{id}` | 同上 |
| `GET /v1/videos/{id}/content` | 完成后 307 跳转视频地址，否则 409 |
| `POST /v1/responses` | 视频用途的 Responses 风格输入，不是完整 OpenAI Responses 实现 |
| `GET /v1/responses/{id}` | `video_generation_call` 输出 |
| `POST /api/v3/contents/generations/tasks` | 兼容 ak2api 的 `{code:100,data:...}` 包装 |
| `GET /api/v3/contents/generations/tasks/{id}` | 同上 |

必填 `prompt`，或通过 `content` / `input` 提供文本。默认 Mini / 5 秒 / 480p / 16:9。时长必须是整数，比例必须在模型目录中。`max_credits` 为正数。`background` 与 `generate_audio` 必须是 JSON 布尔值。

```json
{
  "id": "gen_example", "object": "video.generation",
  "status": "succeeded", "progress": 100,
  "model": "doubao-seedance-2-0-mini-260615",
  "data": [{"url": "https://example.com/video.mp4"}]
}
```

状态：queued → preparing → submitted → running → succeeded / failed / expired。验证错误返回 HTTP 422，队列满为 429，无密钥为 401。异步失败在任务对象的 `error.code/message` 返回；管理端额外展示上游诊断信息。

这些兼容路径沿用 ak2api 的请求与响应设计，不表示与所有第三方 SDK 完全相容。不支持流式视频响应、批量 n>1、指定 seed/fps 或任意像素尺寸。

## 管理功能

- `/api/accounts`：列表、添加、修改、禁用、删除；删除前需禁用。列表中的 ID token、refresh token、密码及 Cookie 值脱敏。
- `/api/accounts/batch-import`：批量邮箱密码导入，重复邮箱合并，可选择启动登录和代理池。
- `/api/accounts/import-browser`：从已登录 CDP 页面导入 Firebase 会话。
- `/api/accounts/sync`：供外部同步程序写入登录状态。
- `/api/accounts/{id}/check`、`/balance`：验证身份、刷新余额。
- `/api/accounts/{id}/cdp/reconnect`、`/profile/reset`：登录恢复、托管 Profile 重建。
- `/api/accounts/{id}/browser/*`：截图、点击、滚动、完成会话导入及释放人工操作占用。
- `/api/settings`：队列、并发、轮询、超时、维护、代理、模型别名等运行配置持久化。
- `/api/tasks`：任务列表 / 测试调用 / 清理已结束记录；`/{id}` 查看完整审计；`/{id}/retry` 重试。
- `/api/model-costs`：实际已完成任务的费用样本。
- `/api/integration-docs`：根据当前模型与设置生成 Markdown 接入说明。

终态任务的显式重试：超时、限流、网络或登录异常会继续原项目；明确生成失败或成功后的再次重试会新建项目，可能产生新的费用。

## 持久化与权限

`data/dra2api.db` 保存账号、任务、余额预留、费用样本和运行设置。项目创建后立即保存项目 ID；prompt 与批准费用的操作在发送前保存标记。极端断电可能导致未发出的操作被当作已发出，此时人工核对项目，优先避免重复计费。

当前使用实例级共享 API key，适合受信任客户端；不提供多租户隔离和逐用户配额。持有 API key 的调用方可查询已知任务 ID；管理密钥可管理全部账号。素材 URL 下载由受信任调用方控制，不应把此实例作为匿名公网代理开放。
