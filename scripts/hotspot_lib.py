"""
hotspot_lib —— 拉取热点 + 调 LLM 的共享工具。

数据来源：
1) 优先从宿主机 ./output/news/YYYY-MM-DD.db 读 SQLite，拿最新一次抓取的全部条目
2) 兼容从 http://localhost:WEBSERVER_PORT 抓 HTML 报告作为兜底（暂未启用）

LLM 调用：
- 走环境变量 ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY
- /v1/messages 同时兼容 claude-* 和 gemini-* 模型
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "output"
NEWS_DIR = OUTPUT_DIR / "news"

DEFAULT_BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
DEFAULT_KEY = (
    os.environ.get("ANTHROPIC_API_KEY")
    or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    or ""
)

# 平台 id → 中文名
PLATFORM_NAMES = {
    "toutiao": "今日头条",
    "baidu": "百度热搜",
    "wallstreetcn-hot": "华尔街见闻",
    "thepaper": "澎湃新闻",
    "bilibili-hot-search": "B站热搜",
    "cls-hot": "财联社",
    "ifeng": "凤凰网",
    "tieba": "贴吧",
    "weibo": "微博",
    "douyin": "抖音",
    "zhihu": "知乎",
}


def latest_db(date: str | None = None) -> Path:
    """返回最新（或指定日期）的 SQLite 文件路径。"""
    if date:
        path = NEWS_DIR / f"{date}.db"
        if not path.exists():
            raise FileNotFoundError(path)
        return path
    candidates = sorted(NEWS_DIR.glob("*.db"))
    if not candidates:
        raise FileNotFoundError(f"no db files under {NEWS_DIR}")
    return candidates[-1]


def load_hotspots(db_path: Path, limit_per_platform: int = 15) -> list[dict]:
    """读最新一次抓取的热点，按平台分别取前 N 条。"""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute(
        """
        SELECT platform_id, title, rank, url, mobile_url, last_crawl_time
        FROM news_items
        WHERE last_crawl_time = (SELECT MAX(last_crawl_time) FROM news_items)
        ORDER BY platform_id, rank
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    con.close()

    bucket: dict[str, list[dict]] = {}
    for r in rows:
        bucket.setdefault(r["platform_id"], []).append(r)

    out: list[dict] = []
    for pid, items in bucket.items():
        for it in items[:limit_per_platform]:
            out.append(
                {
                    "platform_id": pid,
                    "platform": PLATFORM_NAMES.get(pid, pid),
                    "title": it["title"],
                    "rank": it["rank"],
                    "url": it["url"] or it["mobile_url"],
                    "crawl_time": it["last_crawl_time"],
                }
            )
    return out


def call_messages(
    model: str,
    user: str,
    *,
    system: str | None = None,
    max_tokens: int = 2048,
    base_url: str = DEFAULT_BASE,
    api_key: str = DEFAULT_KEY,
    timeout: int = 120,
) -> str:
    """调一次 /v1/messages，返回纯文本。同时兼容 claude-* / gemini-* 模型。"""
    if not api_key:
        raise RuntimeError("missing ANTHROPIC_API_KEY")

    payload: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user}],
    }
    if system:
        payload["system"] = system

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/v1/messages",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:400]}") from e

    parts: list[str] = []
    for c in data.get("content", []):
        if c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "\n".join(parts).strip()


def call_gemini_native(
    *args,
    **kwargs,
) -> tuple[str, list[tuple[str, str]]]:
    """已废弃：早期版本自己拼 generateContent 请求，现在改为直接 subprocess 调 `llms`，
    见 scripts/comment_gemini.py:call_llms_with_retry。保留这个 stub 仅为兼容旧 import。"""
    raise NotImplementedError(
        "call_gemini_native is removed; use llms CLI via subprocess (see comment_gemini.py)"
    )


def chunked(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def render_hotspot_line(idx: int, item: dict) -> str:
    return f"{idx}. [{item['platform']}#{item['rank']}] {item['title']}"


def fmt_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)
