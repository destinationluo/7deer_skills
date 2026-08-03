# SignalLayer Backlinks Client

通过 SignalLayer OpenClaw API 创建、查询和分页管理外链 Campaign。

## 真实 API

- Base URL：`https://signallayer.io/api/openclaw`
- 认证：`Authorization: Bearer sl_xxx`
- 查询积分：`GET /credits`
- 创建：`POST /create-campaign`（成功返回 HTTP 201）
- 查询状态：`GET /campaign-status/{campaign_id}`
- 查询列表：`GET /campaigns?limit=20&offset=0`

服务端创建接口的 `linkCount` 是必填字段，没有 200 条默认值。CLI 在用户省略 `--quantity` 时会主动提交 200，这是客户端默认值。

## 安装

```bash
cp -r signallayer-backlinks-client ~/.openclaw/skills/
```

也可以安装到项目级 `.agent/skills/signallayer-backlinks-client/`。

## 配置 API Key

推荐使用环境变量，避免 Key 出现在对话记录或项目文件中：

```bash
export SIGNALLAYER_API_KEY="sl_your_actual_key"
python scripts/signallayer_client.py --credits
```

Windows PowerShell：

```powershell
$env:SIGNALLAYER_API_KEY = "sl_your_actual_key"
python scripts/signallayer_client.py --credits
```

兼容模式会把 Key 明文写入本 Skill 的 `memory/` 目录。该目录已被 Git 忽略：

```bash
python scripts/signallayer_client.py --configure
```

`--configure` 会调用 `/credits` 做真实验证，验证失败不会保存 Key。

## 创建 Campaign

```bash
python scripts/signallayer_client.py \
  --target "https://example.com" \
  --brand "Example Brand" \
  --keywords "keyword1,keyword2" \
  --quantity 200 \
  --strategy safety \
  --speed drip \
  --drip-days 14
```

可选策略：`safety`、`neutral`、`aggressive`。

可选速度：`natural`、`standard`、`drip`。`drip` 必须是 10–30 天；API 不接受 `instant`。

当前计费是 `1 条外链 = 1 积分`，策略不改变积分系数。

## 查询

```bash
python scripts/signallayer_client.py --status "campaign_uuid"
python scripts/signallayer_client.py --list --limit 20 --offset 0
```

`--list` 查询服务端，不再把本地 Markdown 历史冒充成服务端列表。

## 调试顺序

1. `--credits`：验证 Base URL、TLS、Bearer Key 和积分响应。
2. 检查错误中的 HTTP 状态、`code`、`error` 和 `field`。
3. HTTP 400：检查 camelCase 字段、枚举和数量范围。
4. HTTP 401：检查 API Key 是否完整有效。
5. HTTP 402：积分不足。
6. HTTP 404：检查 Campaign UUID 与所属账号。
7. 返回 HTML：通常意味着 Base URL 或路径错误。

## 文件结构

```text
signallayer-backlinks-client/
├── SKILL.md
├── SETUP.md
├── OPERATION-GUIDE.md
├── references/api.md
└── scripts/signallayer_client.py
```

## License

MIT
