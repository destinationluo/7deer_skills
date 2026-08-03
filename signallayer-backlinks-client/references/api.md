# SignalLayer OpenClaw API 参考

## 基础信息

- Base URL：`https://signallayer.io/api/openclaw`
- 认证：`Authorization: Bearer sl_xxx`
- Content-Type：创建请求使用 `application/json`
- 所有响应都是 JSON，包含布尔字段 `success`

错误响应统一包含：

```json
{
  "success": false,
  "error": "Human-readable message",
  "code": "STABLE_ERROR_CODE",
  "field": "optionalFieldName"
}
```

## 查询积分

```http
GET /credits
```

成功响应（HTTP 200）：

```json
{
  "success": true,
  "email": "user@example.com",
  "credits": {
    "paid": 500,
    "dailyFreeRemaining": 10,
    "dailyFreeTotal": 10,
    "totalAvailable": 510
  }
}
```

## 创建 Campaign

```http
POST /create-campaign
```

### 请求字段

| 字段 | 类型 | 必填 | 默认值 / 约束 |
|------|------|------|---------------|
| `targetUrl` | string | 是 | HTTP/HTTPS URL；无协议时补 HTTPS |
| `targetUrls` | string[] | 否 | 多页面目标 URL |
| `brandName` | string | 是 | 1–200 字符 |
| `keywords` | string | 否 | 默认空，最多 5000 字符 |
| `linkCount` | integer | 是 | 1–10000；服务端没有 200 默认值 |
| `strategy` | string | 否 | `safety` / `neutral` / `aggressive`，默认 `safety` |
| `speed` | string | 否 | `natural` / `standard` / `drip`，默认 `natural` |
| `dripDays` | integer | 否 | 默认 14；`drip` 时必须 10–30 |
| `source` | string | 否 | 默认 `openclaw`，最多 100 字符 |

请求示例：

```json
{
  "targetUrl": "https://example.com",
  "brandName": "Example Brand",
  "keywords": "example,seo",
  "linkCount": 200,
  "strategy": "safety",
  "speed": "drip",
  "dripDays": 14,
  "source": "openclaw-client"
}
```

成功响应（HTTP 201）：

```json
{
  "success": true,
  "campaign": {
    "id": "8b8ff7e3-f29d-4b7d-b0cb-f3e00b731233",
    "targetUrl": "https://example.com/",
    "brandName": "Example Brand",
    "keywords": "example,seo",
    "linkCount": 200,
    "strategy": "safety",
    "speed": "drip",
    "dripDays": 14,
    "status": "processing",
    "createdAt": "2026-08-03T10:00:00Z"
  },
  "credits": {
    "freeUsed": 10,
    "paidUsed": 190,
    "paidBalance": 310,
    "dailyFreeRemaining": 0
  },
  "message": "Campaign created: 200 backlinks for https://example.com/. Agent will start processing shortly."
}
```

订单、积分交易和全部订单明细在一个数据库事务中创建；任一步失败都会回滚。

## 查询单个 Campaign

```http
GET /campaign-status/{campaign_id}
```

成功响应（HTTP 200）：

```json
{
  "success": true,
  "campaign": {
    "id": "8b8ff7e3-f29d-4b7d-b0cb-f3e00b731233",
    "targetUrl": "https://example.com/",
    "brandName": "Example Brand",
    "status": "processing",
    "progress": {
      "total": 200,
      "completed": 85,
      "pending": 115,
      "percent": 43
    },
    "strategy": "safety",
    "speed": "drip",
    "createdAt": "2026-08-03T10:00:00Z"
  },
  "liveLinks": []
}
```

不存在 `updatedAt` 或预计完成时间字段。

## 查询 Campaign 列表

```http
GET /campaigns?limit=20&offset=0
```

- `limit` 默认 20，范围 1–100。
- `offset` 默认 0，范围 0–100000。
- 分页数量与 Campaign 的 `linkCount` 无关。

```json
{
  "success": true,
  "campaigns": [],
  "pagination": {
    "limit": 20,
    "offset": 0,
    "total": 0,
    "hasMore": false
  }
}
```

## 状态与计费

订单状态可能是 `pending`、`processing`、`completed`、`cancelled`。

当前公开 API 始终按 `linkCount` 扣积分：`1 条外链 = 1 积分`。策略不会产生 1.5 倍系数。

## 常见错误

| HTTP | code | 含义 |
|------|------|------|
| 400 | `INVALID_REQUEST` | 请求字段、URL、枚举或范围错误 |
| 400 | `INVALID_CAMPAIGN_ID` | Campaign ID 不是 UUID |
| 400 | `INVALID_PAGINATION` | 列表分页参数错误 |
| 401 | `MISSING_API_KEY` | 缺少 Bearer Header |
| 401 | `INVALID_API_KEY` | Key 格式或凭证无效 |
| 402 | `INSUFFICIENT_CREDITS` | 可用积分不足 |
| 404 | `CAMPAIGN_NOT_FOUND` | 任务不存在或不属于当前用户 |
| 500 | `CAMPAIGN_CREATE_FAILED` | 创建事务失败 |
| 500 | `CAMPAIGN_STATUS_FAILED` | 状态查询失败 |
| 500 | `CAMPAIGN_LIST_FAILED` | 列表查询失败 |

## curl 调试

```bash
curl -i https://signallayer.io/api/openclaw/credits \
  -H "Authorization: Bearer $SIGNALLAYER_API_KEY"
```

```bash
curl -i -X POST https://signallayer.io/api/openclaw/create-campaign \
  -H "Authorization: Bearer $SIGNALLAYER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "targetUrl": "https://example.com",
    "brandName": "Example",
    "linkCount": 50,
    "strategy": "safety",
    "speed": "natural"
  }'
```
