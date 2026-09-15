# 部署与维护

按 ak2api 的既有约定使用 `.github/workflows/deploy-pre.yml`；`pre` 推送或手动运行触发。仓库应为 public，环境凭据留在服务器。

## 当前部署约定

| 项目 | 值 |
| --- | --- |
| GitHub | `btcfoxman/dra2api`，public，默认分支 pre |
| GHCR | `ghcr.io/btcfoxman/dra2api:<commit SHA>` 与 latest |
| runner 标签 | self-hosted、linux、x64、dra2api-pre |
| runner 目录 | `/home/btcfoxman/actions-runners/repo-dra2api` |
| 目标主机 | `192.168.3.5` |
| DEPLOY_PATH | `/home/btcfoxman/docker/dra2api` |
| DEPLOY_SERVICE | dra2api |
| 容器 / 端口 | dra2api / 8798 |
| CDP 起始端口 | 20800 |
| 网络 | my-shared-net |
| 公网入口 | `https://dra2api.aiid.edu.kg` |
| cloudflared | `/etc/cloudflared/config.yml`，origin `http://127.0.0.1:8798` |

## 首次准备

1. 在目标主机创建部署目录，放入 `docker-compose.yml` 和 `.env`，创建 `data/`。凭据文件只允许管理员读取。
2. 配置 API / 管理 / 同步密钥、数据目录与代理池。容器访问宿主机代理使用 `host.docker.internal`；保留 `DRA_PROXY_HOST_OVERRIDE=host.docker.internal`。
3. 在新仓库注册独立 self-hosted runner，标签 `dra2api-pre`，安装为 systemd 服务。复用 ak2api 的 runner 程序版本即可，不能复制其 `.credentials`、`.runner` 等注册状态。
4. runner 用户需能使用 Docker，以及现有 cloudflared 工作流所需的受限 sudo 命令。
5. 确认现有 Cloudflare Tunnel 可用，登录证书 / tunnel 凭据保留原服务的管理方式。

仓库 `GITHUB_TOKEN` 用于 GHCR 推送 / 拉取，工作流授予 `contents:read` 与 `packages:write`。不把服务器 SSH 密码放入仓库；部署由服务器上的 runner 执行。仅可信的 pre 推送和手动运行触发部署，不执行外部 PR 的服务器任务。

## 工作流

1. Ubuntu runner 执行 pytest、ruff。
2. 构建 linux/amd64 镜像，推送 SHA 标签和 latest。
3. 专用 runner 在部署目录以临时 `IMAGE_REGISTRY/IMAGE_NAMESPACE/IMAGE_NAME/IMAGE_TAG` 环境变量拉取并重启服务。
4. 校验部署前后 `.env` / compose 校验和未变化，检查容器健康、Chromium 路径与代理可达性。
5. 为 dra2api 增加 / 更新 cloudflared ingress；修改前备份配置，验证配置后重启 tunnel 服务，创建 DNS 路由。
6. 检查公网 `/health`。

`IMAGE_*` 不写入应用 `.env`，服务器独立维护账号与运行设置。发布后修改 `.env` 需要重建容器；管理端运行设置保存在数据库，优先于对应启动默认值。

## 回滚与备份

保留前一成功 SHA，回滚时：

```bash
cd /home/btcfoxman/docker/dra2api
IMAGE_TAG=<previous-sha> docker compose pull dra2api
IMAGE_TAG=<previous-sha> docker compose up -d dra2api
curl -fsS http://127.0.0.1:8798/health
```

定期备份 `.env`、SQLite 数据库与托管 Chrome Profile。使用 SQLite backup API 或停机复制，避免单独复制正在写入的 WAL 数据库。上游提交记录保存在数据库，升级时必须保留 `data/`。

原始监听日志、数据库、token、Cookie、服务器凭据、CDP 会话文件均不进入公开仓库。`Dockerfile` 只复制应用与依赖。
