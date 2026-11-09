#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一段文本（例如周报）通过 DSH 的 IM 主动投递接口发到微信。

用法：
    python scripts/push_wechat.py report.md      # 从文件读
    python scripts/push_wechat.py -              # 从标准输入读
    type report.md | python scripts/push_wechat.py

依赖的环境变量（都有默认值，正常无需设置）：
    DSH_WEB_URL        DSH Web 服务地址，默认 http://127.0.0.1:51707
    DSH_IM_BOT_ID      微信机器人 botId
    DSH_IM_TARGET_ID   投递目标 targetId（在「设置 → IM机器人 → 机器人齿轮 → 投递设置」里创建）
    DSH_IM_MAX_CHARS   单条消息最大字符数，默认 1800（微信侧过长会被截断）
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("DSH_WEB_URL", "http://127.0.0.1:51707").rstrip("/")
BOT_ID = os.environ.get("DSH_IM_BOT_ID", "wx_11dbd72b513b5b951ef23270").strip()
TARGET_ID = os.environ.get("DSH_IM_TARGET_ID", "tgt_03036841abc8e9ec").strip()
MAX_CHARS = int(os.environ.get("DSH_IM_MAX_CHARS", "1800"))

ENDPOINT = BASE_URL + "/api/dsh-im/delivery/messages"


def read_text(argv):
    if len(argv) > 1 and argv[1] != "-":
        with open(argv[1], "r", encoding="utf-8") as fh:
            return fh.read()
    return sys.stdin.read()


def main(argv):
    if not BOT_ID or not TARGET_ID:
        print("缺少 DSH_IM_BOT_ID 或 DSH_IM_TARGET_ID", file=sys.stderr)
        return 2

    text = read_text(argv).strip()
    if not text:
        print("没有要发送的文本", file=sys.stderr)
        return 2

    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 20].rstrip() + "\n…（内容过长，已截断）"

    payload = json.dumps(
        {"botId": BOT_ID, "targetId": TARGET_ID, "text": text},
        ensure_ascii=False,
    ).encode("utf-8")

    request = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={"content-type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        print(f"投递失败 HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except Exception as exc:  # 网络、JSON 解析等
        print(f"投递失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(body, ensure_ascii=False))
    return 0 if body.get("sent") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
