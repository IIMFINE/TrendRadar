#!/usr/bin/env python3
"""
render_commentary_html —— 把评论员视角分析渲染成可在 web 服务器查看的 HTML。

产物：
  output/commentary/commentary-<ts>.html    —— 这次评论的独立页面
  output/commentary/index.html              —— 历史列表索引（按时间倒序）
  output/commentary/latest.html             —— 指向最新一次的快捷入口

入参：comment_gemini.py 生成的 JSON（推荐）或 commentary.md（仅作为 body 兜底）。

为了不依赖第三方库（markdown/jinja），HTML 完全用字符串拼接 + 内嵌样式。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMENT_DIR = REPO_ROOT / "output" / "commentary"

# 平台徽章颜色（与首页风格呼应，但不强制一致）
PLATFORM_COLOR: dict[str, str] = {
    "澎湃新闻":     "#dc2626",
    "今日头条":     "#ef4444",
    "百度热搜":     "#2563eb",
    "华尔街见闻":   "#0ea5e9",
    "B站热搜":      "#fb7185",
    "财联社":       "#f97316",
    "凤凰网":       "#9333ea",
    "贴吧":         "#16a34a",
    "微博":         "#ec4899",
    "抖音":         "#06b6d4",
    "知乎":         "#3b82f6",
}

CATEGORY_EMOJI: dict[str, str] = {
    "中国": "🇨🇳", "台海": "🌉", "美国": "🇺🇸", "伊朗": "🇮🇷",
    "中东": "🕌", "俄罗斯": "🇷🇺", "乌克兰": "🇺🇦", "东亚": "🗾",
    "非洲": "🌍", "国际": "🌐", "经济": "💹", "社会": "👥",
    "电子": "💻", "科技": "🔬", "能源": "⚡", "AI": "🤖", "其他": "📌",
}

PAGE_CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
  margin: 0; padding: 0; background: #f5f6fa; color: #1f2937;
  line-height: 1.7;
}
.container { max-width: 880px; margin: 0 auto; padding: 28px 20px 60px; }
.topbar {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 24px;
}
.topbar a { color: #4f46e5; text-decoration: none; font-size: 14px; }
.topbar a:hover { text-decoration: underline; }
.header {
  background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%);
  color: white; padding: 28px 24px; border-radius: 14px;
  box-shadow: 0 4px 24px rgba(79, 70, 229, .18);
}
.header h1 { margin: 0 0 6px; font-size: 24px; font-weight: 700; }
.header .meta { font-size: 13px; opacity: .9; margin-top: 8px; }
.header .meta code {
  background: rgba(255,255,255,.18); padding: 2px 8px; border-radius: 4px;
  font-size: 12px;
}
.toc {
  margin: 22px 0 8px; padding: 14px 20px; background: white;
  border-radius: 10px; border: 1px solid #e5e7eb;
  display: flex; flex-wrap: wrap; gap: 8px;
}
.toc a {
  display: inline-block; padding: 4px 12px; border-radius: 999px;
  background: #f3f4f6; color: #374151; text-decoration: none;
  font-size: 13px; transition: background .15s;
}
.toc a:hover { background: #e0e7ff; color: #4f46e5; }
.toc .count { color: #9ca3af; margin-left: 4px; font-size: 11px; }
section.category {
  margin-top: 28px; background: white; border-radius: 14px;
  border: 1px solid #e5e7eb; overflow: hidden;
}
section.category h2 {
  margin: 0; padding: 14px 20px; background: #fafafa;
  border-bottom: 1px solid #e5e7eb;
  font-size: 18px; font-weight: 600; color: #111827;
}
section.category h2 .emoji { margin-right: 8px; }
.item { padding: 18px 20px; border-bottom: 1px solid #f3f4f6; }
.item:last-child { border-bottom: none; }
.item .title {
  font-weight: 600; color: #111827; font-size: 15px;
  margin-bottom: 8px;
}
.item .title a { color: inherit; text-decoration: none; }
.item .title a:hover { color: #4f46e5; text-decoration: underline; }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 11px; font-weight: 500; color: white; margin-right: 6px;
  vertical-align: 2px;
}
.rank { color: #6b7280; font-size: 12px; margin-left: 4px; }
.comment {
  color: #374151; font-size: 14.5px;
  white-space: pre-wrap; word-break: break-word;
}
.comment.error { color: #dc2626; font-style: italic; font-size: 13px; }
.sources {
  margin-top: 36px; padding: 18px 20px; background: white;
  border-radius: 12px; border: 1px solid #e5e7eb;
}
.sources h3 { margin: 0 0 10px; font-size: 15px; color: #374151; }
.sources ul { margin: 0; padding-left: 18px; font-size: 13px; color: #6b7280; }
.sources li { margin-bottom: 4px; }
.footer { text-align: center; color: #9ca3af; font-size: 12px; margin-top: 36px; }
@media (max-width: 640px) {
  .container { padding: 16px 12px 40px; }
  .header h1 { font-size: 20px; }
}
"""

INDEX_CSS = PAGE_CSS + """
.history-list { background: white; border-radius: 14px; border: 1px solid #e5e7eb; overflow: hidden; }
.history-item {
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 20px; border-bottom: 1px solid #f3f4f6;
  text-decoration: none; color: inherit;
}
.history-item:last-child { border-bottom: none; }
.history-item:hover { background: #f9fafb; }
.history-item .time { font-weight: 500; color: #111827; }
.history-item .stats { color: #6b7280; font-size: 13px; }
"""


def render_one(classified: dict, comments_by_idx: dict[int, dict], *, model_comment: str, search: bool, ts: str) -> str:
    """渲染单次评论页面的 HTML。"""
    groups = classified.get("groups", [])
    parts: list[str] = []
    # head
    parts.append(f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>评论员视角 {ts}</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<div class="container">
  <div class="topbar">
    <a href="../index.html">← 返回首页</a>
    <a href="index.html">评论历史 →</a>
  </div>
  <div class="header">
    <h1>📝 评论员视角分析</h1>
    <div class="meta">
      时间戳：<code>{html.escape(ts)}</code>
      分类模型：<code>{html.escape(classified.get('model', '?'))}</code>
      评论模型：<code>{html.escape(model_comment)}</code>
      联网搜索：{'开' if search else '关'}
    </div>
  </div>
""")

    # TOC（每分类一个锚点）
    parts.append('<div class="toc">')
    for g in groups:
        cat = g["category"]
        n = sum(1 for it in g["items"] if comments_by_idx.get(_key_of(it)))
        if n == 0:
            continue
        anchor = _slugify(cat)
        emoji = CATEGORY_EMOJI.get(cat, "📌")
        parts.append(
            f'<a href="#{anchor}">{emoji} {html.escape(cat)}<span class="count">({n})</span></a>'
        )
    parts.append('</div>')

    # body
    all_sources: list[str] = []
    seen_src: set[str] = set()

    for g in groups:
        cat = g["category"]
        anchor = _slugify(cat)
        emoji = CATEGORY_EMOJI.get(cat, "📌")
        items_with_comment: list[tuple[dict, dict]] = []
        for it in g["items"]:
            r = comments_by_idx.get(_key_of(it))
            if r is None:
                continue
            items_with_comment.append((it, r))
        if not items_with_comment:
            continue

        parts.append(f'<section class="category" id="{anchor}">')
        parts.append(f'<h2><span class="emoji">{emoji}</span>{html.escape(cat)}</h2>')

        for it, r in items_with_comment:
            color = PLATFORM_COLOR.get(it["platform"], "#6b7280")
            url = it.get("url") or "#"
            title = html.escape(it["title"])
            parts.append('<div class="item">')
            parts.append(
                f'<div class="title">'
                f'<span class="badge" style="background:{color}">{html.escape(it["platform"])}</span>'
                f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{title}</a>'
                f'<span class="rank">#{it.get("rank","?")}</span>'
                f'</div>'
            )
            if r.get("error"):
                parts.append(
                    f'<div class="comment error">评论生成失败：{html.escape(r["error"])}</div>'
                )
            else:
                body = html.escape(r["comment"]).replace("\n\n", "\n").strip()
                parts.append(f'<div class="comment">{body}</div>')
                for s in r.get("sources", []):
                    if s not in seen_src:
                        seen_src.add(s)
                        all_sources.append(s)
            parts.append('</div>')  # .item

        parts.append('</section>')

    if all_sources:
        parts.append('<div class="sources"><h3>🔗 联网检索来源</h3><ul>')
        for s in all_sources[:60]:
            # 来源行可能是 "• title" 或 "title\n  url"，留原始即可
            parts.append(f'<li>{html.escape(s)}</li>')
        parts.append('</ul></div>')

    parts.append('<div class="footer">由 TrendRadar 评论员视角脚本生成</div>')
    parts.append('</div></body></html>')
    return "".join(parts)


def render_index(records: list[dict]) -> str:
    """渲染历史归档索引页（按时间倒序）。"""
    parts: list[str] = []
    parts.append(f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>评论员视角 · 历史归档</title>
<style>{INDEX_CSS}</style>
</head>
<body>
<div class="container">
  <div class="topbar">
    <a href="../index.html">← 返回首页</a>
    <a href="latest.html">查看最新 →</a>
  </div>
  <div class="header">
    <h1>📝 评论员视角 · 历史归档</h1>
    <div class="meta">共 {len(records)} 份分析</div>
  </div>
  <div class="history-list" style="margin-top:22px">
""")
    for r in records:
        parts.append(
            f'<a class="history-item" href="{html.escape(r["filename"])}">'
            f'<span class="time">{html.escape(r["display_time"])}</span>'
            f'<span class="stats">{r["categories"]} 个分类 · {r["items"]} 条评论</span>'
            f'</a>'
        )
    parts.append('</div></div></body></html>')
    return "".join(parts)


def _key_of(item: dict) -> tuple:
    return (item.get("platform"), item.get("rank"), item.get("title"))


def _slugify(s: str) -> str:
    return "cat-" + re.sub(r"[^a-zA-Z0-9一-龥]", "-", s).strip("-")


def collect_history(out_dir: Path) -> list[dict]:
    """扫描 commentary-*.html，按时间倒序汇总。"""
    recs: list[dict] = []
    for p in sorted(out_dir.glob("commentary-*.html"), reverse=True):
        m = re.match(r"commentary-(\d{8})-(\d{6})\.html", p.name)
        if not m:
            continue
        date_str, time_str = m.group(1), m.group(2)
        try:
            dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
            display = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            display = p.stem
        # 顺便统计内容
        try:
            content = p.read_text("utf-8")
            cats = content.count('<section class="category"')
            items = content.count('<div class="item">')
        except Exception:
            cats = items = 0
        recs.append({
            "filename": p.name,
            "display_time": display,
            "categories": cats,
            "items": items,
        })
    return recs


def build_comments_map(classified: dict, results: list[dict]) -> dict[tuple, dict]:
    """从 comment_gemini 输出的 results 列表（带 comment/sources/error 字段）建索引。"""
    out: dict[tuple, dict] = {}
    for r in results:
        out[_key_of(r)] = r
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="render commentary to HTML")
    ap.add_argument("--classified", required=True, help="classify_gemini 输出的 json")
    ap.add_argument("--comments", required=True, help="comment_gemini 写出的 results json")
    ap.add_argument("--ts", required=True, help="时间戳，如 20260630-201530")
    ap.add_argument("--out-dir", default=str(COMMENT_DIR))
    ap.add_argument("--model-comment", default="gemini-3.1-pro-preview")
    ap.add_argument("--search", action="store_true", default=True)
    ap.add_argument("--no-search", dest="search", action="store_false")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    classified = json.loads(Path(args.classified).read_text("utf-8"))
    results = json.loads(Path(args.comments).read_text("utf-8"))
    comments_map = build_comments_map(classified, results)

    page_html = render_one(
        classified, comments_map,
        model_comment=args.model_comment, search=args.search, ts=args.ts,
    )
    out_path = out_dir / f"commentary-{args.ts}.html"
    out_path.write_text(page_html, "utf-8")
    print(f"[render] wrote {out_path}", file=sys.stderr)

    # latest.html 直接复制内容（容器内挂只读卷的情况下软链不一定能用）
    (out_dir / "latest.html").write_text(page_html, "utf-8")
    print(f"[render] wrote {out_dir / 'latest.html'}", file=sys.stderr)

    # index.html
    recs = collect_history(out_dir)
    (out_dir / "index.html").write_text(render_index(recs), "utf-8")
    print(f"[render] wrote {out_dir / 'index.html'} ({len(recs)} records)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
