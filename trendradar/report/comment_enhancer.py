# coding=utf-8
"""
评论增强器：对生成完的 HTML 中每条新闻调 Codex 评论员视角，
把评论注入回原 HTML，实现"localhost:8090 自动看到带评论版本"。

设计要点：
- 直接对 HTML 字符串做 DOM 操作（用 re + 字符串拼接，避免引入 BeautifulSoup 等重依赖）
- 失败重试 3 次 + 指数退避；任何单条失败不影响其它条
- 并发调用（默认 4 路），减少总耗时
- 通过环境变量 COMMENTARY_ENABLED=true 启用，未启用时是 no-op
- COMMENTARY_MAX_ITEMS 限制单次最多评论多少条，避免一轮跑太久
- 已经评论过的条目（结果落地在 output/commentary/cache.json）下次直接复用
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

# === 配置开关 ===

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


ENABLED = _env_bool("COMMENTARY_ENABLED", False)
# MAX_ITEMS: 单轮最多评论多少条。0 = 不限制（抓多少评多少，默认行为）。
# 想控制成本/耗时可设为正整数，比如 30。
MAX_ITEMS = _env_int("COMMENTARY_MAX_ITEMS", 0)
CONCURRENCY = _env_int("COMMENTARY_CONCURRENCY", 4)
RETRIES = _env_int("COMMENTARY_RETRIES", 3)
MODEL = os.environ.get("COMMENTARY_MODEL", "gpt-5.6-sol").strip()
THINKING = os.environ.get("COMMENTARY_THINKING", "HIGH").strip().lower()
# SEARCH: 是否开启模型内置联网搜索。默认开（抓热点强依赖时效性事实）。
SEARCH = _env_bool("COMMENTARY_SEARCH", True)

COMMENTARY_BASE = os.environ.get(
    "COMMENTARY_BASE_URL",
    "https://api.aicodemirror.ai/api/codex/backend-api/codex/v1",
).rstrip("/")
COMMENTARY_KEY = (
    os.environ.get("COMMENTARY_API_KEY")
    or os.environ.get("GEMINI_API_KEY")  # 兼容旧配置，避免升级时直接中断
    or os.environ.get("GOOGLE_API_KEY")
    or ""
)

CACHE_FILE = Path(os.environ.get("COMMENTARY_CACHE", "output/commentary/cache.json"))

# === 评论模板 ===

PROMPT_TEMPLATE = """⏰ 重要提示：当前真实日期是 {current_date}，请基于这个时间点来分析新闻的时效性和背景。

你是犀利的时事评论员，擅长锐评风格。这是一条热点新闻，请联网搜索背景资料，然后用锐评视角深挖背后的真实逻辑。

【新闻】[{source}] {title}
【URL】{url}

锐评风格要求：
1) **时间准确**：请务必基于当前真实日期（{current_date}）来判断新闻的时效性，不要根据训练数据中的过时信息做出错误判断。
2) **开门见山**：第一句话直击要害，点破表象背后的真实动机或利益博弈（不超过30字）。
3) **深层原因分析**：用 80~120 字深挖这条新闻为什么会在此时此刻出现——从政策环境、技术演进、市场周期、社会情绪等多维度剖析根源。避免用"资本布局"、"被资本操纵"等笼统概念替代具体分析，要落实到具体的利益相关方、政策动向、技术节点或市场拐点。
4) **历史镜像**：结合历史上的今天或近期类似事件，看是否存在周期性规律、重演模式或反常之处（30~50字）。如果搜索不到明确关联，可简略提及行业/领域的历史走向作为对照。
5) **趋势预判**：基于搜索到的最新信息和当前态势，预测后续最可能出现的 2-3 种走向——哪些群体会进一步行动？政策/市场会如何响应？对普通人有什么实际影响？（40~60字）
6) **有理有据**：优先引用搜索到的最新事实、数据或案例支撑观点；如果信息存疑，直接点名质疑。
7) **语言风格**：简洁有力，不绕弯子，不打官腔，敢于揭示矛盾和问题。避免中立表述，要有明确态度。
8) 只输出评论正文，一段连续中文，不要标题、不要 Markdown 符号、不要寒暄。总字数控制在 150~230 字。"""


# === Codex Responses API 调用 ===

def _extract_response_text(data: dict) -> str:
    """从 Responses API 响应提取模型正文，并兼容常见代理包装格式。"""
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in ("output_text", "text"):
                text = content.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    if chunks:
        return "".join(chunks).strip()

    # Some OpenAI-compatible gateways wrap a Responses result as chat.completions.
    choices = data.get("choices", []) or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {}) or {}
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
    return ""


def _call_codex(prompt: str, timeout: int = 120) -> str:
    """调用 Codex Responses API，并按需启用模型内置 web_search。"""
    if not COMMENTARY_KEY:
        raise RuntimeError("missing COMMENTARY_API_KEY")

    body: dict = {
        "model": MODEL,
        "input": prompt,
    }
    if THINKING in {"low", "medium", "high"}:
        body["reasoning"] = {"effort": THINKING}
    if SEARCH:
        body["tools"] = [{"type": "web_search"}]

    req = urllib.request.Request(
        url=f"{COMMENTARY_BASE}/responses",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {COMMENTARY_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"responses HTTP {e.code}: {detail}") from e
    text = _extract_response_text(data)
    if not text:
        raise RuntimeError("responses returned no output text")
    return text


def _call_with_retry(prompt: str) -> str:
    last_err = ""
    for attempt in range(1, RETRIES + 1):
        try:
            text = _call_codex(prompt)
            if not text:
                raise RuntimeError("empty response")
            return text
        except Exception as e:
            last_err = str(e)[:200]
            if attempt < RETRIES:
                delay = 2.0 * (2 ** (attempt - 1))
                print(f"  [comment retry {attempt}/{RETRIES - 1}] {last_err} → wait {delay}s",
                      file=sys.stderr)
                time.sleep(delay)
    raise RuntimeError(f"Codex failed after {RETRIES} attempts: {last_err}")


# === 缓存 ===

def _key_of(source: str, title: str) -> str:
    h = hashlib.md5(f"{MODEL}||{SEARCH}||{source}||{title}".encode("utf-8")).hexdigest()
    return h


def _load_cache() -> dict[str, str]:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), "utf-8"
    )


# === HTML 解析与注入 ===

# 匹配一个完整的 .news-item 块。news-item 内部有嵌套 div，但我们只关心找到
# news-content 结束位置，在它后面插入评论块。
# 策略：找到每个 news-content 的开闭 div，定位插入点。
NEWS_ITEM_PATTERN = re.compile(
    r'(<div class="news-item[^"]*">.*?</div>\s*</div>\s*</div>)',
    re.DOTALL,
)


def _extract_fields(news_item_html: str) -> Optional[dict]:
    """从一条 news-item HTML 中提取 source、title、url。"""
    src_m = re.search(
        r'<span class="source-name">([^<]+)</span>', news_item_html
    )
    link_m = re.search(
        r'<a href="([^"]+)"[^>]*class="news-link"[^>]*>([^<]+)</a>',
        news_item_html,
    )
    if not link_m:
        return None
    url = link_m.group(1)
    title = _unescape_html(link_m.group(2)).strip()
    source = _unescape_html(src_m.group(1)).strip() if src_m else ""
    return {"source": source, "title": title, "url": url}


def _unescape_html(s: str) -> str:
    return (
        s.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&#x27;", "'")
    )


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_comment_block(comment: str, *, error: bool = False) -> str:
    """构造右栏评论块 HTML（默认折叠成 2 行，点击展开）。"""
    safe = _escape_html(comment.strip())
    if error:
        return (
            f'<div class="news-comment news-comment-error">'
            f'<span class="comment-label">⚠️ 评论失败</span>'
            f'<div class="comment-body">{safe}</div>'
            f'</div>'
        )
    return (
        '<div class="news-comment" data-collapsed="true">'
        '<div class="comment-label">📝 评论员视角</div>'
        f'<div class="comment-body">{safe}</div>'
        '<button class="comment-toggle" type="button">展开 ▾</button>'
        '</div>'
    )


def _inject_into_news_item(news_item_html: str, comment_html: str) -> str:
    """把评论块塞进 .news-title 后面（仍在 .news-content 内，显示在标题下方）。"""
    # 找到 </div>(news-title 的关闭标签)，在它后面插入评论块
    # news-title 结构：<div class="news-title">...</div>
    # 我们要插在 news-title 的 </div> 之后、news-content 的 </div> 之前
    m = re.search(r'(<div class="news-title">.*?</div>)', news_item_html, re.DOTALL)
    if not m:
        return news_item_html
    end_pos = m.end()
    return news_item_html[:end_pos] + comment_html + news_item_html[end_pos:]


# === 注入 CSS / JS ===

INJECT_CSS = """
            /* === Commentary Enhancer === */
            .news-item {
                position: relative;
            }
            .news-comment {
                margin-top: 12px;
                padding: 12px 14px;
                background: #faf9ff;
                border-left: 3px solid #7c3aed;
                border-radius: 6px;
                font-size: 13.5px;
                color: #374151;
                line-height: 1.6;
            }
            .news-comment .comment-label {
                font-size: 11px;
                color: #7c3aed;
                font-weight: 600;
                margin-bottom: 6px;
                display: block;
            }
            .news-comment .comment-body {
                display: -webkit-box;
                -webkit-line-clamp: 2;
                -webkit-box-orient: vertical;
                overflow: hidden;
                white-space: normal;
                word-break: break-word;
            }
            .news-comment[data-collapsed="false"] .comment-body {
                display: block;
                overflow: visible;
                -webkit-line-clamp: unset;
            }
            .news-comment .comment-toggle {
                margin-top: 6px;
                padding: 0;
                background: none;
                border: none;
                color: #7c3aed;
                font-size: 12px;
                cursor: pointer;
                display: inline-block;
            }
            .news-comment .comment-toggle:hover {
                text-decoration: underline;
            }
            .news-comment-error {
                background: #fef2f2;
                border-left-color: #dc2626;
                color: #991b1b;
            }
            .news-comment-error .comment-label {
                color: #dc2626;
            }
            body.dark-mode .news-comment {
                background: #1e1b3a;
                color: #c4b5fd;
                border-left-color: #a78bfa;
            }
            body.dark-mode .news-comment .comment-label,
            body.dark-mode .news-comment .comment-toggle { color: #a78bfa; }
"""

INJECT_JS = """
            // === Commentary Enhancer ===
            document.addEventListener('click', function (ev) {
                var btn = ev.target.closest && ev.target.closest('.comment-toggle');
                if (!btn) return;
                var wrap = btn.closest('.news-comment');
                if (!wrap) return;
                var collapsed = wrap.getAttribute('data-collapsed') !== 'false';
                wrap.setAttribute('data-collapsed', collapsed ? 'false' : 'true');
                btn.textContent = collapsed ? '收起 ▴' : '展开 ▾';
            });
"""


def _inject_css_js(html_content: str) -> str:
    if "/* === Commentary Enhancer ===" in html_content:
        return html_content  # 已注入

    # CSS 注入：找第一个 </style>
    html_content = html_content.replace(
        "</style>", INJECT_CSS + "\n        </style>", 1
    )

    # JS 注入：找最后一个 </script>（页面主 JS 块在文档末尾）
    # 用 rfind + 切片而不是 replace，避免误伤外部 CDN script
    last_script_pos = html_content.rfind("</script>")
    if last_script_pos > 0:
        # 确保这不是外部 script（外部 script 有 src 属性，我们要找内联的）
        # 简单策略：在最后一个 </script> 前插入，通常页面主 JS 在最后
        html_content = (
            html_content[:last_script_pos]
            + INJECT_JS + "\n        "
            + html_content[last_script_pos:]
        )

    return html_content


# === 主入口 ===

def enhance_html_with_comments(html_content: str) -> str:
    """对完整的 HTML 文档做后处理：抽取新闻 → 调评论 → 注入。

    未启用 / 缺 key 时直接原样返回。任何失败都降级到原 HTML，绝不破坏抓取流程。
    """
    if not ENABLED:
        return html_content
    if not COMMENTARY_KEY:
        print("[commentary] COMMENTARY_ENABLED=true 但缺少 COMMENTARY_API_KEY，跳过", file=sys.stderr)
        return html_content

    items: list[tuple[int, dict, str]] = []  # (matches 中的全局序号, 字段, 原始 news-item HTML)
    matches = list(NEWS_ITEM_PATTERN.finditer(html_content))
    for idx, m in enumerate(matches):
        block = m.group(1)
        fields = _extract_fields(block)
        if fields and fields["title"]:
            items.append((idx, fields, block))

    if not items:
        print("[commentary] 未在 HTML 里找到任何新闻条目，跳过", file=sys.stderr)
        return html_content

    cache = _load_cache()
    cache_keys = {i: _key_of(it["source"], it["title"]) for i, it, _ in items}

    todo = [(i, it) for i, it, _ in items if cache_keys[i] not in cache]
    cached_count = len(items) - len(todo)
    truncated = 0
    if MAX_ITEMS > 0 and len(todo) > MAX_ITEMS:
        truncated = len(todo) - MAX_ITEMS
        todo = todo[:MAX_ITEMS]
    cap_desc = f"cap={MAX_ITEMS}" if MAX_ITEMS > 0 else "cap=unlimited(评所有抓到的)"
    trunc_desc = f" truncated={truncated}" if truncated else ""
    search_desc = "ON(Codex web_search)" if SEARCH else "OFF"
    print(
        f"[commentary] total={len(items)} cached={cached_count} "
        f"to_query={len(todo)}{trunc_desc} model={MODEL} thinking={THINKING} "
        f"search={search_desc} conc={CONCURRENCY} {cap_desc}",
        file=sys.stderr,
    )

    # 并发调评论
    if todo:
        def _job(spec):
            i, it = spec
            current_date = datetime.now().strftime("%Y年%m月%d日")
            prompt = PROMPT_TEMPLATE.format(
                current_date=current_date,
                source=it["source"],
                title=it["title"],
                url=it["url"] or "(无)"
            )
            try:
                text = _call_with_retry(prompt)
                return i, {"comment": text, "error": None}
            except Exception as e:
                return i, {"comment": "", "error": str(e)[:200]}

        with ThreadPoolExecutor(max_workers=max(1, CONCURRENCY)) as pool:
            futures = {pool.submit(_job, spec): spec for spec in todo}
            done = 0
            for fut in as_completed(futures):
                spec = futures[fut]
                i = spec[0]
                try:
                    _, r = fut.result()
                except Exception as e:
                    r = {"comment": "", "error": str(e)[:200]}
                done += 1
                if not r.get("error"):
                    cache[cache_keys[i]] = r["comment"]
                tag = "OK" if not r.get("error") else "ERR"
                title = spec[1]["title"][:30]
                print(f"  [{done}/{len(todo)}] {tag}: {title}", file=sys.stderr)

        _save_cache(cache)

    # 注入 CSS / JS（这会改变后续字符串的字节位置，但不影响 finditer 顺序）
    html_content = _inject_css_js(html_content)

    # 按文档顺序逐条替换。re.sub 的 callback 顺序与 finditer 一致，
    # 用 counter 跟踪当前是第几个 news-item，再用 items_lookup 查它对应的字段。
    items_lookup = {global_idx: fields for global_idx, fields, _ in items}

    counter = {"idx": 0}

    def _replace(m: re.Match) -> str:
        i = counter["idx"]
        counter["idx"] += 1
        it = items_lookup.get(i)
        if it is None:
            return m.group(1)
        key = _key_of(it["source"], it["title"])
        if key in cache:
            return _inject_into_news_item(
                m.group(1), _build_comment_block(cache[key])
            )
        return m.group(1)

    return NEWS_ITEM_PATTERN.sub(_replace, html_content)
