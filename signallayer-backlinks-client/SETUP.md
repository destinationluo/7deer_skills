# SignalLayer Backlinks Client 安装配置

## 前置要求

- OpenClaw 或其他支持 Agent Skills 的运行环境
- Python 3.10+
- `requests`：`python -m pip install requests`
- 有效的 SignalLayer API Key（`sl_` 开头）

## 安装

全局 OpenClaw：

```bash
cp -r signallayer-backlinks-client ~/.openclaw/skills/
```

项目级：

```bash
cp -r signallayer-backlinks-client your-project/.agent/skills/
```

安装后确认 Agent 能发现 `signallayer-backlinks-client`，而不是查找不存在的 Hermes SDK。

## 推荐配置

macOS/Linux：

```bash
export SIGNALLAYER_API_KEY="sl_your_actual_key"
python scripts/signallayer_client.py --credits
```

Windows PowerShell：

```powershell
$env:SIGNALLAYER_API_KEY = "sl_your_actual_key"
python scripts/signallayer_client.py --credits
```

成功输出 `/credits` 响应才表示 Key 有效。

## 兼容配置

```bash
python scripts/signallayer_client.py --configure
```

该命令会先请求 `/credits` 验证，成功后写入 `memory/signallayer-api-user.md`。这是明文文件；目录已加入仓库 `.gitignore`，但仍应限制文件访问权限。

## 验证完整流程

```bash
# 1. 验证 Key 和积分
python scripts/signallayer_client.py --credits

# 2. 创建最小测试任务（会真实扣除 1 积分）
python scripts/signallayer_client.py \
  --target "https://example.com" \
  --brand "Example" \
  --quantity 1 \
  --speed natural

# 3. 使用返回的 UUID 查询
python scripts/signallayer_client.py --status "campaign_uuid"

# 4. 从服务端查询列表
python scripts/signallayer_client.py --list --limit 20
```

第 2 步会产生真实订单和积分消耗；仅做连通性检查时停在第 1 步。

## 常见问题

- 401：Key 缺失、格式错误或服务端验证失败。
- 400 `INVALID_REQUEST`：检查 camelCase 字段、速度枚举和数量。
- 402：积分不足。
- HTML/404：检查 Base URL，状态路径必须是 `/campaign-status/{id}`。
- `instant` 被拒绝：这是预期行为；改用 `natural`、`standard` 或 `drip`。

完整接口见 `references/api.md`。
