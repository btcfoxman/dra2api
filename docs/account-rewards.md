# 账号任务奖励

管理端账号列表的 **领取奖励** 按钮领取当前已完成任务的积分奖励，并重新查询上游余额。启用与禁用账号均可操作；领取成功不会自动启用禁用账号。

管理接口：`POST /api/accounts/{account_id}/rewards/claim`，需要管理端登录 Cookie。

响应包含 `claimed_count`、`claimed_credits`、`already_claimed_count`、`reconciled_count`、`failed_count`、`unknown_count`、`unattempted_count`、`balance_refreshed`、逐项 `results`、中文 `message` 和脱敏的 `account`。`status=partial` 表示有失败、未确认、未尝试的任务或余额刷新失败。最近一次结果持久化在 `account.balance_details.reward_claim_stats`，鼠标停留在账号余额下方结果提示可查看逐项详情。

## 已观察的 Drama.Land 协议

2026-09-17 通过用户在 Drama.Land 的手动操作确认：

1. `GET /api/v1/task/list` 返回 `data.tasks`。
2. `POST /api/v1/task/claim`，JSON 为 `{"task_id":"daily_generate"}`。
3. 成功回执的 `data` 包含 `success=true`、`task_id`、`reward_type=credits`、`reward_amount`。
4. `GET /api/v1/user/credits/summary` 返回真实 `data.credits` 和带有来源、到期时间的积分批次。网关通过现有 `account_state()` 同时校验账号身份并刷新这些字段。

领取条件必须同时满足：`status=completed`、`reward_eligible=true`、没有 `claimed_at`、奖励类型为 `credits`。待完成的邀请任务也可能有 `reward_eligible=true`；注册奖励自动发放且没有完成状态。不会自动执行邀请、生成视频等任务。任务 ID 与页面描述中的数量未必相同，不能按 ID 推断达成条件、金额或期限。

本次人工操作依次领取成就、每日生成及另外两项成就，成功回执为 1000、200、3000、6000 积分；余额最终为 10200。一次重复领取返回 HTTP 400 / `Reward already claimed`。

## 重复、异常与余额

- 同账号的领取互斥，并与自动签到、登录及维护互斥；前端轮询重绘仍保留按钮忙碌状态。
- 重复领取作为已领取跳过，不累计本次奖励金额。
- POST 超时、服务端错误或回执无法确认时，通过只读任务列表核实。不会自动重发领取；未能核实的显示待确认，并停止本轮后续领取。再次手动操作会重新查询领取条件。
- 核实已领取并不能证明积分由本次请求发放，故单独记录 `reconciled_count`，不计入 `claimed_credits`。
- 单项明确失败保留其他成功结果；余额刷新失败保留此前余额并明确提示，不能用“旧余额 + 奖励”代替实际余额。
- 请求使用账号原有 Firebase 会话与代理。原始 CDP 日志、浏览器资料及凭据不属于公开仓库内容。
