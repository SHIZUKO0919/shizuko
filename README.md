# shizuko

自动化小工具集合。

## 1. 每周 skill 周报（`weekly-skill-report.yml`）

每周一 12:00（北京时间）自动跑一次：找出**上一周**（上周一~上周日）在 GitHub 上新建、
星标最多的 skill 仓库，生成榜单并发给你。

- 脚本：`scripts/weekly_skill_report.py`（纯标准库，无依赖）
- 工作流：`.github/workflows/weekly-skill-report.yml`

### 投递方式

| 通道 | 需要配置 | 说明 |
|---|---|---|
| GitHub Issue | 无 | 默认开启。自动建 Issue 并指派给仓库 owner，GitHub 会邮件/App 通知你。同日重跑只更新同一条，不刷屏 |
| Webhook | `WEBHOOK_URL` secret | Discord / Slack / 飞书机器人，POST 一份 Markdown |
| Telegram | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | 直接推消息 |

没配置的通道会自动跳过，不会报错。

### 搜索范围

5 组查询去重后合并，按 `created:<上周一>..<上周日>` 过滤，按星标降序：

```
skill in:name,description
skills in:name,description
claude skill in:name,description
agent skill in:name,description
topic:skill
```

想加关键词就改脚本顶部的 `QUERIES`，想改榜单长度改 `TOP_N`。

### 简介翻译

榜单里的英文简介会在投递前自动翻成中文（Google 翻译免费接口）：

- 全中文的简介原样保留；"英文为主 + 夹一点中文"的也会翻，投递出去是干净中文。
- 结果缓存在 `scripts/.translate-cache.json`（已 gitignore），同一个简介不会重复请求。
- 免费接口会限流：脚本在 429/5xx 时退避重试，翻译失败就保留英文，不影响出榜。
- 不想翻译：加 `--no-translate`，或设环境变量 `TRANSLATE_DESC=0`。

### 本地预览（不发任何东西）

```bash
python scripts/weekly_skill_report.py --dry-run --out report.md
python scripts/weekly_skill_report.py --dry-run --date 2026-11-09   # 以某个周一为基准
python scripts/weekly_skill_report.py --dry-run --no-translate     # 保留英文简介
```

### 注意事项

- cron 用 UTC：`0 4 * * 1` = 每周一 12:00 CST。中国无夏令时，全年固定。
- GitHub 对 60 天无提交的仓库会自动暂停 schedule 工作流。工作流里的 `keepalive` job 每周把当天
  日期写进仓库根目录的 `.keep` 并提交一次，保持"有活动"状态，正常不会被停；
  万一还是停了，在 Actions 页面点一下 Enable 即可恢复，也可以手动 Run workflow。
- 搜索 API 有频率限制，脚本在 403/429 时会退避重试，单次运行最多 5 次查询。

## 2. 微信推送（`scripts/push_wechat.py`）

周一榜单同时推到微信（DSH 桌面端的 IM 机器人通道）：

```bash
python scripts/push_wechat.py weekly-report.md    # 也可用 - 从标准输入读
```

- 前提：DSH 桌面端开着，且「设置 → IM机器人 → 微信机器人卡片齿轮 → 投递设置」里已有一条投递目标。
- botId / targetId 已写在脚本默认值里，需要换目标时用环境变量 `DSH_IM_BOT_ID`、`DSH_IM_TARGET_ID` 覆盖。
- 走 DSH 本地接口 `POST /api/dsh-im/delivery/messages`，成功输出 `{"sent": true}`。
- 单条超过 1800 字会自动截断（微信侧长度有限）。
- 这条通道和上面的 GitHub 工作流互相独立：GitHub 那边发 Issue，这边发微信。

## 3. 发布 / 部署（`release.yml`）

推 `v*` 标签（或手动运行）→ 打源码包 → 创建 GitHub Release →（可选）rsync 部署到服务器。
部署相关 Secrets 见 `.github/workflows/release.yml` 顶部注释。
