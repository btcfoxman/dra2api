# DRA2API

将 Drama.Land 的视频生成流程接入统一 API。账号池、管理控制台、任务审计、代理、浏览器人工验证及部署方式沿用 [ak2api](https://github.com/btcfoxman/ak2api) 的结构。

链路为 Firebase 登录 → hosted 项目 → 生成指令 → 费用审批 → 异步视频任务 → 视频地址。适用于你有权使用的 Drama.Land 账号；这是独立的社区协议适配项目。

## 型号与规格

| API 别名 | 上游 service | 时长 | 分辨率 | 验证程度 |
| --- | --- | --- | --- | --- |
| `seedance-2.0-mini` | `seedance-2-0-mini` | 5–12 秒 | 480p、720p | 5 秒 / 480p / 16:9 已完成实际生成 |
| `seedance-2.0-fast` | `seedance-2-0-fast` | 4–15 秒 | 480p、720p | 文生视频 4 秒 / 480p / 9:16；9 图 + 3 视频 + 3 音频、4 秒 / 480p / 16:9 已实际生成 |
| `seedance-2.0` | `seedance-2-0` | 4–15 秒 | 480p、720p、1080p、4k* | 网站配置及 CLI 文档 |
| `seedance-2.5` | `seedance-2-5` | 5–30 秒 | 480p、720p | 9 图 + 3 视频 + 3 音频，5 秒 / 480p / 16:9 已实际生成 |

各型号接受 `16:9`、`9:16`、`1:1`、`4:3`、`3:4`、`21:9`。`GET /v1/models` 返回完整模型 ID、别名、参数范围与证据级别。

2.5 的 15 份素材实测：上传、项目参考、报价预览与生成命令逐项一致；生成费用 650 credits，结果含音轨。视频与音频参考合计上限为 30 秒。参见[实测记录](docs/verification-15-references.md)。

Fast 已使用同组 15 份素材生成 4 秒 / 480p / 16:9 视频，生成费用 260 credits。其参考总数由保守的 10 份修正为 15 份，其他型号限制不由此外推。参见 [Fast 实测记录](docs/verification-fast-15-references.md)。

*4k 仅见于 Seedance 2.0 CLI 文档，当前网页选择器未展示，尚未实测。其余未实测的组合也不能视为可用性保证。Fast 和 2.5 的 1080p 存在配置冲突，当前不开放。参见[协议与证据](docs/protocol.md)。

## 功能

- 文生视频及图片、视频、音频参考；URL 或 base64 data URL 输入。
- `/v1/videos`、`/v1/videos/generations`、视频版 `/v1/responses`、`/api/v3/contents/generations/tasks`。
- 账号导入、批量导入、Firebase 会话同步、refresh token 更新、余额检查和每日签到。
- 账号代理与并发、可调整的任务队列、余额预留、实际消耗统计。
- 持久化项目 ID、报价和任务结果；重启后查询原任务，模糊的网络写入失败不自动重发。
- 控制台测试调用、任务详情、调用方请求 / 上游请求 / 上游响应 / 调用方响应四份审计视图。
- Docker 中 Chromium + Xvfb，浏览器截图、人工验证、托管 Profile 重置。
- `pre` 分支 GitHub Actions 测试、GHCR 镜像构建、专用 runner 部署及 cloudflared HTTPS 入口。

## 快速启动

需要 Docker Compose。复制 `.env.example` 到 `.env`，设置独立的 `DRA_API_KEY`、`DRA_ADMIN_TOKEN`、`DRA_SYNC_TOKEN`，按运行环境配置代理。

```bash
docker network create my-shared-net
docker compose up -d
```

已有共享网络时可跳过创建步骤。GHCR 镜像已验证允许匿名拉取；也可从源码执行 `docker build -t ghcr.io/btcfoxman/dra2api:latest .` 后启动。

管理端：`http://localhost:8798`；健康检查：`/health`；接口定义：`/docs`。

本地开发：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --host 127.0.0.1 --port 8798
```

本地启动读取进程环境变量；如需读取 `.env`，使用 `uvicorn ... --env-file .env`，并调整数据路径为本机可写目录。没有设置 API 密钥时，首次启动将随机凭据保存为 `data/bootstrap-credentials.json`，后续启动复用。音视频参考时长检测需要 `ffprobe`；容器已安装。

## 导入账号

管理端可添加邮箱与密码，或者导入 Firebase `access_token`（ID token）及 `refresh_token`。有 refresh token 的会话无需每次启动浏览器；Cookie 不能代替 ID token。

已通过 Chrome CDP 登录时，点击“导入浏览器”，填写 **服务所在机器可访问的** 调试地址和账号代理。导入前会验证页面是 Drama.Land，并检查邮箱 / UID。IPv4 与 IPv6 同端口可能对应不同浏览器。外部浏览器的 Profile 由用户管理；容器无法访问另一台电脑的 localhost。

服务端同步示例，使用单独的同步密钥：

```bash
curl "$BASE_URL/api/accounts/sync" \
  -H "Authorization: Bearer $DRA_SYNC_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"my-account","email":"you@example.com","access_token":"FIREBASE_ID_TOKEN","refresh_token":"FIREBASE_REFRESH_TOKEN","proxy_url":"socks5://xray:20001","max_concurrency":1,"auto_login":true}'
```

## 创建与查询视频

```bash
curl "$BASE_URL/v1/videos" \
  -H "Authorization: Bearer $DRA_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"seedance-2.0-mini","prompt":"A calm mountain lake at sunrise","duration":5,"resolution":"480p","aspect_ratio":"16:9","max_credits":225,"background":true}'

curl "$BASE_URL/v1/videos/TASK_ID" -H "Authorization: Bearer $DRA_API_KEY"
```

成功响应的 `data[].url` 为视频地址。`GET /v1/videos/TASK_ID/content` 在完成后跳转到首个视频。默认异步；`background:false` 最多等待配置时间，未完成时仍返回可继续查询的任务 ID。

`max_credits` 是本次生成允许的最高上游报价。示例 225 是一次 Mini 5 秒 / 480p 观察值；费率会变化，以审批时返回的报价及实际账单为准。

该上限只约束视频生成报价。网站另有聊天、素材理解等代理调用计费，账户余额变化可能大于视频账单；即使视频报价被拒绝，也可能已产生少量代理费用。

参考素材使用 `image_urls`、`video_urls`、`audio_urls`，也支持 `content` / Responses 格式的多模态输入。一次请求只接受 `n=1`。使用 `resolution` + `aspect_ratio`；未确认的 `seed`、`fps`、自定义 `width` / `height` / `size` 会被拒绝。

`generate_audio` 与 `negative_prompt` 通过项目生成指令传递，报价没有逐项确认字段，不能保证供应商严格执行。网关能校验模型、时长、分辨率，并在报价提供比例时校验比例。

Fast 实测请求 `generate_audio:false` 仍返回非静音音轨，当前不能据此参数保证静音。该次文件为 496×864、约 4.10 秒；分辨率和比例是上游档位，输出存在编码对齐和时长取整。

## 日志与部署

- [协议链路、规格证据、风控点](docs/protocol.md)
- [API 与管理端功能说明](docs/api.md)
- [部署、runner、cloudflared 与回滚](docs/deployment.md)
- [监听记录方法](docs/protocol-listen.md)

SQLite、账号会话、Chrome Profile、原始流量、`.env` 和部署凭据均留在本地数据目录，不进入仓库或镜像。数据库与原始请求含账号会话或用户素材，应只向管理者开放并定期备份。

## 验证

```bash
python -m pytest -q
python -m ruff check app tests
```

测试包含参数矩阵、鉴权、凭据轮换、费用上限、任务所有权、网络写入保护、恢复查询、余额预留及公开响应格式。离线测试不代表所有上游组合都已生成验证。
