# Video task failures

Failed and expired task responses include an `error` object:

```json
{
  "code": "DRAMA_HTTP_ERROR",
  "message": "素材超限，请修改后再试~",
  "category": "MEDIA_LIMIT_EXCEEDED",
  "outcome": "rejected",
  "refunded": false
}
```

- `code` preserves the original diagnostic code. A generic HTTP error is not sufficient to decide whether another generation is safe.
- `category` identifies the normalized reason, such as media size, duration, format, queue capacity, or content moderation.
- `outcome=rejected` means the failure occurred before project creation. `outcome=failed` means an upstream generation has a confirmed failed status. These permit the caller's normal failure policy.
- `outcome=unknown` means a generation may still exist. Do not automatically create another task or switch to another generation channel. Inspect the existing task/project first.
- `refunded=true` requires an upstream refund receipt; it does not follow merely from failure.

The project-creation boundary is persisted before the write. Older media validation failures are classified as rejected only when no project, generation activity, or persisted create request exists. A friendly message alone never proves that a submitted task has ended.

Raw diagnostic messages remain available in authenticated administrative records.
