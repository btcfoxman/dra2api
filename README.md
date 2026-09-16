# DRA2API

## 创建与查询视频

将 `BASE_URL` 替换为服务地址，使用 `DRA_API_KEY` 作为 Bearer 密钥。

### 创建视频

`POST /v1/videos`

```bash
curl "$BASE_URL/v1/videos" \
  -H "Authorization: Bearer $DRA_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "doubao-seedance-2-0-fast-260128",
    "prompt": "清晨的山间湖泊，镜头缓慢推进，水面泛起微光",
    "duration": 4,
    "resolution": "480p",
    "aspect_ratio": "16:9",
    "generate_audio": true,
    "background": true
  }'
```

| 参数 | 说明 |
| --- | --- |
| `model` | 模型 ID；示例使用 Seedance 2.0 Fast |
| `prompt` | 视频提示词 |
| `duration` | 视频秒数，须在所选模型支持范围内 |
| `resolution` | 分辨率档位，如 `480p`、`720p`，以所选模型支持范围为准 |
| `aspect_ratio` | 画面比例，如 `16:9`、`9:16` |
| `image_urls` | 可选，参考图片 URL 数组 |
| `video_urls` | 可选，参考视频 URL 数组 |
| `audio_urls` | 可选，参考音频 URL 数组 |
| `generate_audio` | 是否请求生成音频；上游不保证严格执行 |
| `max_credits` | 可选，允许审批的最高视频生成报价；不包含代理聊天等费用 |
| `background` | 默认 `true`，异步返回任务 ID |

创建响应示例：

```json
{
  "id": "gen_example",
  "object": "video.generation",
  "status": "queued",
  "progress": 0,
  "model": "doubao-seedance-2-0-fast-260128",
  "data": []
}
```

保存返回的 `id` 用于查询。`background:false` 会等待一段配置时间；尚未完成时仍返回任务 ID，可继续查询。

### 查询视频

`GET /v1/videos/{task_id}`

```bash
curl "$BASE_URL/v1/videos/TASK_ID" \
  -H "Authorization: Bearer $DRA_API_KEY"
```

成功响应示例：

```json
{
  "id": "gen_example",
  "object": "video.generation",
  "status": "succeeded",
  "progress": 100,
  "model": "doubao-seedance-2-0-fast-260128",
  "data": [
    { "url": "https://example.com/result.mp4" }
  ]
}
```

`queued`、`preparing`、`submitted`、`running` 表示任务尚未完成；`succeeded`、`failed`、`expired` 为终态。成功时读取 `data[].url` 获取视频。

失败时读取 `error.code` 和归一化后的 `error.message`。仅在上游确认退款后，提示才包含“积分已返还”。已确认退款的失败响应示例：

```json
{
  "id": "gen_example",
  "status": "failed",
  "data": [],
  "error": {
    "code": "GENERATION_FAILED",
    "message": "生成的视频内容违规，请修改描述后重试，积分已返还~"
  }
}
```

也可访问 `GET /v1/videos/{task_id}/content`，完成后会通过 HTTP 307 跳转到首个视频地址：

```bash
curl -L "$BASE_URL/v1/videos/TASK_ID/content" \
  -H "Authorization: Bearer $DRA_API_KEY" \
  -o result.mp4
```
