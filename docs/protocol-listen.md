# 监听记录方法

本项目的首次协议分析来自经授权的浏览器 CDP 网络记录。公开仓库保留结论、字段说明和合成测试数据，原始登录信息、用户提示词、素材及网页源代码留在本地。

## 建议日志结构

| 文件 | 用途 |
| --- | --- |
| events.jsonl | 请求、响应、响应体、重定向、导航、WebSocket 与 SSE |
| errors.jsonl | 监听器自身的失败，不等同于业务请求失败 |
| status.json | 监听心跳、连接目标与事件计数 |
| marks.jsonl | 人工标注登录、选择规格、提交和获取结果的阶段 |
| session.json | 监听时间、浏览器范围与脱敏规则 |

使用 `sessionId + requestId` 关联事件。同一个 requestId 可能被重定向复用，结合事件顺序及 redirectResponse 还原。页面、iframe、worker 可能使用不同 sessionId，应跟随新 target 附加监听。SSE 中的工具执行与费用审批是恢复完整链路的重要证据。

解析 JSONL 时逐行迭代文件，不以 Unicode `splitlines()` 切分整个文件，避免请求 JSON 中的 Unicode 行分隔符造成错误切割。

## 录制核验

1. 先确认 CDP 页面域名、浏览器 Profile 及端口；IPv4 与 IPv6 同端口可能由不同进程监听。
2. 在登录前开始记录，覆盖业务请求与 Firebase 身份服务。
3. 标注选择的 model、duration、resolution、aspect ratio、素材清单，以及提交和结果出现的时间。
4. 关联 project_id、approval_id、job_ref、供应商任务 ID，不用奖励任务列表替代视频任务查询。
5. 监听结束停止写入并固定目录，保留文件摘要和分析索引。
6. 导出报告前排除 token、refresh token、密码、Cookie、签名上传 URL、用户内容及无关网站流量。

新型号应至少核验一次实际任务及账单，再提升其验证级别。一次成功请求不能证明全部参数组合都支持。
