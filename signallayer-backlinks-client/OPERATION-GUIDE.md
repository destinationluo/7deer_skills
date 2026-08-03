# SignalLayer 外链 Campaign 操作与调试手册

## 1. 组件边界

- OpenClaw：理解自然语言并执行 Skill。
- `signallayer-backlinks-client`：把请求转换成真实 API 参数；“Hermes”只是旧称，不是独立 SDK。
- SignalLayer：鉴权、扣积分、创建订单和返回状态。

## 2. 安全配置

优先使用环境变量：

```bash
export SIGNALLAYER_API_KEY="sl_your_actual_key"
python scripts/signallayer_client.py --credits
```

不要把完整 Key 粘贴到公开对话、日志或 Git。兼容命令 `--configure` 会真实调用 `/credits` 后才保存明文配置。

## 3. 自然语言操作

最简命令：

```text
给 https://example.com 发外链，品牌是 Example
```

客户端在用户不指定数量时采用 200，并将其显式发送为 `linkCount: 200`。服务端本身不会补 200。

完整命令：

```text
用 SignalLayer 创建 Campaign：
- 目标：https://example.com
- 品牌：Example
- 关键词：seo,marketing
- 数量：200
- 策略：safety
- 速度：drip
- 滴灌天数：14
```

支持：

- 策略：`safety`、`neutral`、`aggressive`。
- 速度：`natural`、`standard`、`drip`。
- `dripDays`：10–30。
- `instant`：不支持，不得发送。

## 4. API 操作

Base URL：`https://signallayer.io/api/openclaw`

### 验证 Key 和积分

```http
GET /credits
Authorization: Bearer sl_xxx
```

### 创建

```http
POST /create-campaign
Authorization: Bearer sl_xxx
Content-Type: application/json

{
  "targetUrl": "https://example.com",
  "brandName": "Example",
  "keywords": "seo,marketing",
  "linkCount": 200,
  "strategy": "safety",
  "speed": "natural",
  "dripDays": 14,
  "source": "openclaw-skill"
}
```

成功是 HTTP 201。读取 `campaign.id`，不要读取顶层 `campaign_id`。

### 查询状态

```http
GET /campaign-status/{campaign_uuid}
```

进度读取路径：

```text
campaign.progress.completed
campaign.progress.total
campaign.progress.pending
campaign.progress.percent
```

API 不返回 `updatedAt` 或预计完成时间，不要生成虚假的 ETA。

### 查询列表

```http
GET /campaigns?limit=20&offset=0
```

默认 20 条，最多 100 条。这里的数量是 Campaign 分页大小，不是每个 Campaign 的外链数量。

## 5. Python 客户端

```bash
python scripts/signallayer_client.py --credits

python scripts/signallayer_client.py \
  --target "https://example.com" \
  --brand "Example" \
  --quantity 200 \
  --strategy safety \
  --speed drip \
  --drip-days 14

python scripts/signallayer_client.py --status "campaign_uuid"
python scripts/signallayer_client.py --list --limit 20 --offset 0
```

脚本失败时返回非零退出码，可用于 CI/CD 判断。

## 6. 计费

当前规则：

```text
消耗积分 = linkCount
```

即 `1 条外链 = 1 积分`。`aggressive` 不使用 1.5 系数。消费时优先使用当日免费积分，再使用付费积分。

## 7. 排查矩阵

| 现象 | 可能原因 | 检查动作 |
|------|----------|----------|
| TLS 或域名错误 | 使用了旧 `api.signallayer.io/v1` | 改用 `https://signallayer.io/api/openclaw` |
| HTML 404 | 路径错误 | 状态路径必须是 `/campaign-status/{id}` |
| 400 `INVALID_REQUEST` | snake_case、非法枚举或范围 | 使用 `targetUrl/brandName/linkCount`，查看 `field` |
| 400 `INVALID_CAMPAIGN_ID` | ID 不是 UUID | 使用创建响应里的 `campaign.id` |
| 401 | Key 无效 | 运行 `--credits` 做真实验证 |
| 402 | 积分不足 | 查看 `available` 和 `required` |
| 404 `CAMPAIGN_NOT_FOUND` | 任务不存在或属于其他 Key | 检查 Key 和 UUID |
| 500 | 创建事务或查询失败 | 记录 `code`，稍后重试并联系支持 |

## 8. 批量任务

批量创建时逐条记录返回的 Campaign ID，并为网络失败设置有限重试。客户端可以主动间隔请求，但服务端没有公开固定限流阈值，因此不要把 `sleep(2)` 描述为服务端保证。

## 9. 能力边界

- 支持创建、积分查询、单任务状态和服务端列表。
- 暂不提供取消/暂停接口。
- 不承诺具体完成 SLA。
- Campaign 创建的订单、积分交易和全部明细在一个数据库事务中提交或回滚。

完整字段和响应示例见 `references/api.md`。
