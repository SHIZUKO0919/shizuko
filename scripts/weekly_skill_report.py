#!/usr/bin/env python3
"""每周一 12:00（Asia/Shanghai）汇总"上一周"GitHub 上新出现、星标最多的 skill 仓库。

用法：
    python scripts/weekly_skill_report.py              # 生成周报并发送（Issue / Webhook）
    python scripts/weekly_skill_report.py --dry-run    # 只打印 Markdown，不发任何东西

环境变量（全部可选，缺省时自动跳过对应投递通道）：
    GITHUB_TOKEN            GitHub token；有 issues:write 权限时用来创建 Issue
    GITHUB_REPOSITORY       形如 owner/repo；创建 Issue 的目标仓库
    REPORT_ISSUE_ASSIGNEE   Issue 指派人，一般填仓库 owner，GitHub 会邮件通知
    WEBHOOK_URL             通用 webhook（Discord / Slack / 飞书等），POST JSON
    TELEGRAM_BOT_TOKEN      Telegram 机器人 token
    TELEGRAM_CHAT_ID        Telegram 会话 id

只依赖 Python 3.9+ 标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

API = "https://api.github.com"

# 中国自 1991 年起无夏令时，固定 UTC+8；Windows 版 Python 常缺 IANA 时区库，故退回固定偏移
try:
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:                                  # ZoneInfoNotFoundError 等
    TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

# 多组查询覆盖"skill"的不同叫法，结果按 owner/repo 去重
QUERIES: list[str] = [
    "skill in:name,description",
    "skills in:name,description",
    "claude skill in:name,description",
    "agent skill in:name,description",
    "topic:skill",
]

PER_QUERY = 50          # 每组查询取多少条
TOP_N = 10              # 榜单展示条数
MIN_STARS = 1           # 过滤掉 0 星的空壳仓库

# 简介翻译：默认把英文简介翻成中文再投递。
# 翻译接口不可用（离线 / 被墙 / 限流）时自动退回英文原文，不影响周报生成。
TRANSLATE_DESC = os.environ.get("TRANSLATE_DESC", "1") != "0"
TRANSLATE_LANG = os.environ.get("TRANSLATE_LANG", "zh-CN")
TRANSLATE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TRANSLATE_CACHE = os.path.join(SCRIPT_DIR, ".translate-cache.json")

HEADERS: dict[str, str] = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "weekly-skill-report",
}
if os.environ.get("GITHUB_TOKEN"):
    HEADERS["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #
def gh(path: str, method: str = "GET", payload: dict[str, Any] | None = None,
       params: dict[str, Any] | None = None) -> Any:
    """最简 GitHub REST 调用，带一次限流重试。"""
    url = path if path.startswith("http") else f"{API}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None

    last_error: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, method=method, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            last_error = RuntimeError(f"{method} {url} -> {exc.code}: {detail}")
            if exc.code in (403, 429) and attempt < 2:
                time.sleep(20 * (attempt + 1))   # 触发限流时退避
                continue
            raise last_error from None
        except urllib.error.URLError as exc:
            last_error = RuntimeError(f"{method} {url} -> {exc.reason}")
            if attempt < 2:
                time.sleep(5)
                continue
            raise last_error from None
    raise last_error  # pragma: no cover


def resolve_window(today: date) -> tuple[date, date]:
    """最近一个完整的"周一→周日"区间。

    周一运行时 = 上周一 ~ 昨天（周日）；其它日子运行时 = 上一个完整自然周。
    """
    last_monday = today - timedelta(days=today.weekday() + 7)
    return last_monday, last_monday + timedelta(days=6)


def search_repos(query: str, start: date, end: date) -> list[dict[str, Any]]:
    q = f"{query} created:{start.isoformat()}..{end.isoformat()} stars:>={MIN_STARS}"
    data = gh("/search/repositories",
              params={"q": q, "sort": "stars", "order": "desc",
                      "per_page": PER_QUERY})
    return data.get("items") or []


def collect(start: date, end: date) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """跑完所有查询，按仓库去重并校验创建日期落在窗口内。"""
    seen: dict[str, dict[str, Any]] = {}
    hits: dict[str, int] = {}

    for query in QUERIES:
        try:
            items = search_repos(query, start, end)
        except RuntimeError as exc:
            print(f"[warn] 查询失败，跳过：{query} ({exc})", file=sys.stderr)
            hits[query] = 0
            continue
        hits[query] = len(items)
        for repo in items:
            name = repo["full_name"]
            if repo.get("fork") or repo.get("archived"):
                continue
            created = (repo.get("created_at") or "")[:10]
            if not (start.isoformat() <= created <= end.isoformat()):
                continue
            seen.setdefault(name, repo)

    repos = sorted(seen.values(),
                   key=lambda r: (r.get("stargazers_count", 0),
                                  r.get("pushed_at") or ""),
                   reverse=True)
    return repos, hits


# --------------------------------------------------------------------------- #
# 简介翻译（英文 → 中文）
# --------------------------------------------------------------------------- #
def needs_translation(text: str) -> bool:
    """判断一段简介是否值得翻译。

    全中文的不动；只含少量英文单词的也不动；
    "英文为主 + 夹一点中文"（很多仓库喜欢中英双写）仍然翻译，
    这样投递出去的简介是干净中文。
    """
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff"
              or "\u3040" <= ch <= "\u30ff")
    latin = sum(1 for ch in text if ("a" <= ch.lower() <= "z"))
    if latin < 4:
        return False
    return cjk / max(1, cjk + latin) < 0.5


def load_cache() -> dict[str, str]:
    try:
        with open(TRANSLATE_CACHE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict[str, str]) -> None:
    try:
        with open(TRANSLATE_CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=0)
    except OSError as exc:
        print(f"[warn] 翻译缓存写入失败：{exc}", file=sys.stderr)


def _gt(text: str, sl: str = "auto") -> str | None:
    """调一次 Google 翻译，成功返回译文，失败返回 None（内部已重试）。"""
    params = urllib.parse.urlencode({
        "client": "gtx", "sl": sl, "tl": TRANSLATE_LANG, "dt": "t", "q": text,
    })
    url = f"{TRANSLATE_ENDPOINT}?{params}"

    last_error: Exception | None = None
    time.sleep(0.4)                                 # 连续请求之间留点间隔，避免限流
    for attempt in range(3):
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (weekly-skill-report)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            translated = "".join(
                seg[0] for seg in (data[0] or []) if seg and seg[0]
            ).strip()
            if translated:
                return translated
            last_error = RuntimeError("空翻译结果")
        except urllib.error.HTTPError as exc:
            last_error = exc
            # 免费接口会限流，退避后重试
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2.5 * (attempt + 1))
                continue
            break
        except Exception as exc:                    # 网络/解析失败
            last_error = exc
            if attempt < 2:
                time.sleep(1.5)
                continue
            break

    print(f"[warn] 翻译请求失败：{last_error}", file=sys.stderr)
    return None


def _join_parts(parts: list[str]) -> str:
    """中文句子之间不要空格，英文句子之间保留一个空格。"""
    out = ""
    for part in parts:
        if not out:
            out = part
            continue
        prev = out[-1]
        need_space = not ("\u3400" <= prev <= "\u9fff"
                          or "\u3040" <= prev <= "\u30ff"
                          or prev in "。！？；：、）」』，")
        out += (" " if need_space else "") + part
    return out


def _translate_segments(text: str) -> str | None:
    """混合语言简介：按句子切开，只翻英文句子，中文原样保留。"""
    parts = [p.strip() for p in re.split(r"(?<=[.!?;。！？；])\s*", text)]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    translated: list[str] = []
    changed = False
    for part in parts:
        if needs_translation(part):
            out = _gt(part)
            if out:
                translated.append(out)
                changed = changed or out.strip() != part
                continue
        translated.append(part)
    if not changed:
        return None
    return _join_parts(translated)


def translate_text(text: str, cache: dict[str, str]) -> str:
    """整段翻译，失败或翻不动时退回原文。"""
    text = text.strip()
    if not text or not needs_translation(text):
        return text
    if text in cache:
        return cache[text]

    translated = _gt(text)

    # 混着中文时 auto 检测会原样返回，强制按英文源再翻一次
    if not translated or translated.strip() == text:
        forced = _gt(text, sl="en")
        if forced and forced.strip() != text:
            translated = forced

    # 仍翻不动就按句子切片，只翻英文片段
    if not translated or translated.strip() == text:
        segmented = _translate_segments(text)
        if segmented:
            translated = segmented

    if not translated or translated.strip() == text:
        return text
    cache[text] = translated
    return translated


def translate_repos(repos: list[dict[str, Any]], limit: int) -> int:
    """把前 limit 个仓库的英文简介翻成中文，返回实际翻译条数。"""
    if not TRANSLATE_DESC or not repos:
        return 0

    cache = load_cache()
    before = len(cache)
    done = 0
    for repo in repos[:limit]:
        desc = (repo.get("description") or "").strip()
        if not desc:
            continue
        translated = translate_text(desc, cache)
        if translated != desc:
            repo["description"] = translated
            done += 1
    if len(cache) != before:
        save_cache(cache)
    return done


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def fmt_stars(n: int) -> str:
    return f"{n:,}"


def describe(repo: dict[str, Any]) -> str:
    desc = (repo.get("description") or "").strip().replace("\n", " ")
    if len(desc) > 140:
        desc = desc[:137] + "…"
    return desc or "_（无描述）_"


def render(repos: list[dict[str, Any]], start: date, end: date,
           hits: dict[str, int]) -> str:
    total = len(repos)
    lines: list[str] = [
        f"## 🎯 上周 GitHub 最热 skill",
        "",
        f"- 统计窗口：**{start.isoformat()} ~ {end.isoformat()}**（Asia/Shanghai，含首尾）",
        f"- 命中：{total} 个新建仓库（去重后，来源查询 {len(QUERIES)} 组）",
        "",
    ]

    if not repos:
        lines += ["上周没有找到符合条件的新 skill 仓库。", ""]
        return "\n".join(lines)

    top = repos[0]
    lines += [
        "### 🥇 星标最多",
        "",
        f"**[{top['full_name']}]({top['html_url']})** — ⭐ {fmt_stars(top['stargazers_count'])}",
        "",
        f"> {describe(top)}",
        "",
        " · ".join(filter(None, [
            f"语言：{top['language']}" if top.get("language") else "",
            f"创建：{(top.get('created_at') or '')[:10]}",
            f"最近提交：{(top.get('pushed_at') or '')[:10]}",
            f"Topics：{', '.join(top.get('topics', [])[:5])}" if top.get("topics") else "",
        ])),
        "",
    ]

    lines += [
        f"### 完整榜单（Top {min(TOP_N, total)}）",
        "",
        "| # | 仓库 | ⭐ | 简介 |",
        "|---:|---|---:|---|",
    ]
    for i, repo in enumerate(repos[:TOP_N], start=1):
        lines.append(
            f"| {i} | [{repo['full_name']}]({repo['html_url']}) "
            f"| {fmt_stars(repo['stargazers_count'])} | {describe(repo)} |"
        )
    lines.append("")

    if total > TOP_N:
        lines.append(f"（另有 {total - TOP_N} 个仓库未列出）")
        lines.append("")

    lines += [
        "<details><summary>各查询命中数</summary>",
        "",
    ]
    for query, count in hits.items():
        lines.append(f"- `{query}` → {count}")
    lines += ["", "</details>", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 投递
# --------------------------------------------------------------------------- #
def send_issue(title: str, body: str) -> None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        print("[skip] 未配置 GITHUB_TOKEN / GITHUB_REPOSITORY，跳过 Issue 投递",
              file=sys.stderr)
        return

    payload: dict[str, Any] = {"title": title, "body": body}
    assignee = os.environ.get("REPORT_ISSUE_ASSIGNEE")
    if assignee:
        payload["assignees"] = [assignee]

    try:
        # 同一天重复运行（重试 / 手动触发）时更新已有 Issue，避免刷屏
        existing = gh(f"/repos/{repo}/issues",
                      params={"state": "all", "per_page": 30,
                              "sort": "created", "direction": "desc"})
        for issue in existing if isinstance(existing, list) else []:
            if issue.get("title") == title and "pull_request" not in issue:
                gh(f"/repos/{repo}/issues/{issue['number']}",
                   method="PATCH", payload={"body": body})
                print(f"[ok] 已更新 Issue #{issue['number']}: {title}")
                return
        created = gh(f"/repos/{repo}/issues", method="POST", payload=payload)
        print(f"[ok] 已创建 Issue #{created['number']}: {title}")
    except RuntimeError as exc:
        print(f"[warn] Issue 投递失败：{exc}", file=sys.stderr)


def send_webhook(body: str) -> None:
    url = os.environ.get("WEBHOOK_URL")
    if not url:
        return
    chunks = [body[i:i + 1800] for i in range(0, len(body), 1800)] or [body]
    for chunk in chunks:
        payload = {"content": chunk, "text": chunk, "msgtype": "markdown",
                   "markdown": {"content": chunk}}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "weekly-skill-report"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
            print("[ok] 已发送 Webhook")
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"[warn] Webhook 失败：{exc}", file=sys.stderr)


def send_telegram(body: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in [body[i:i + 3800] for i in range(0, len(body), 3800)] or [body]:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": chunk,
            "parse_mode": "Markdown", "disable_web_page_preview": "true",
        }).encode("utf-8")
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, data=data, method="POST"),
                    timeout=20) as resp:
                resp.read()
            print("[ok] 已发送 Telegram")
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"[warn] Telegram 失败：{exc}", file=sys.stderr)


def write_step_summary(body: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:
        print(f"[warn] 写 step summary 失败：{exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
def main() -> int:
    # Windows 控制台默认 GBK，打印榜单里的 emoji 会直接抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="每周 GitHub 最热 skill 周报")
    parser.add_argument("--dry-run", action="store_true",
                        help="只输出 Markdown，不创建 Issue / 不发消息")
    parser.add_argument("--date", help="以指定日期（YYYY-MM-DD）为基准计算上周窗口")
    parser.add_argument("--out", help="把 Markdown 另存为指定文件（UTF-8）")
    parser.add_argument("--no-translate", action="store_true",
                        help="不翻译英文简介（默认会翻成中文）")
    args = parser.parse_args()

    if args.no_translate:
        global TRANSLATE_DESC
        TRANSLATE_DESC = False

    now = datetime.now(TZ)
    today = date.fromisoformat(args.date) if args.date else now.date()
    start, end = resolve_window(today)

    print(f"[info] 窗口 {start} ~ {end}", file=sys.stderr)
    repos, hits = collect(start, end)
    print(f"[info] 去重后 {len(repos)} 个仓库", file=sys.stderr)
    translated = translate_repos(repos, TOP_N)
    if translated:
        print(f"[info] 已翻译 {translated} 条英文简介", file=sys.stderr)

    body = render(repos, start, end, hits)
    title = f"🎯 上周 GitHub 最热 skill · {end.isoformat()}"

    write_step_summary(body)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
        print(f"[ok] 已写出 {args.out}", file=sys.stderr)

    if args.dry_run:
        print(body)
        return 0

    send_issue(title, body)
    send_webhook(body)
    send_telegram(body)
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
