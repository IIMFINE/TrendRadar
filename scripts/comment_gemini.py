#!/usr/bin/env python3
"""
comment_gemini —— Gemini 3.1 Pro 评论员视角，对每一条热点逐条深挖。

调用方式：subprocess 跑 `/home/pan/.local/bin/llms`，
和 ~/.claude/skills/gemini-helper 用的是同一条 grounding 通道。

设计要点：
- **一条新闻一次调用**：请求小、超时风险低、可独立重试。
- **失败自动重试 3 次**（指数退避），仍失败时把错误写进结果而不是整批中断。
- **按分类切换"评论员视角"**：不同主题用不同分析维度。

用法:
  python3 scripts/classify_gemini.py < hotspots.json | python3 scripts/comment_gemini.py
  python3 scripts/comment_gemini.py --no-search           # 关闭联网，更快（不推荐）
  python3 scripts/comment_gemini.py --model gemini-3-pro-preview
  python3 scripts/comment_gemini.py --concurrency 4       # 4 路并发
  python3 scripts/comment_gemini.py --limit-per-group 6   # 每组截断到 6 条

默认行为（v2）:
- 联网搜索（Gemini grounding）**默认开启**，抓多少条评多少条，不做数量截断。
- `--limit-per-group 0`（默认）= 不限制；传正整数才截断。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hotspot_lib import render_hotspot_line  # noqa: E402

DEFAULT_MODEL = "gemini-3.1-pro-preview"
LLMS_BIN = "/home/pan/.local/bin/llms"

# 不同分类用不同"评论员视角"
ANGLE_PRESETS: dict[str, str] = {
    "中国": "重点分析对中国政治格局、经济发展、对外关系的影响。指出政策信号和潜在变动。",
    "台海": "重点分析对两岸政治、东亚地缘、半导体产业链的影响。",
    "美国": "重点分析对美国国内政治、美联储/美元、对华政策的影响。",
    "经济": "重点分析对资本市场短期反应、行业格局中长期演变、普通人钱包的影响。",
    "社会": "重点分析对民生切身体验、社会治理、舆论走向的影响。",
    "国际": "重点分析对全球经济格局、民生（粮食/能源/物价）、政治变动（联盟/制裁）的影响。",
    "俄罗斯": "重点分析对俄罗斯经济（油气/制裁）、政治稳定、地缘联盟的影响。",
    "乌克兰": "重点分析对俄乌战局、欧洲安全、能源与粮食市场的影响。",
    "伊朗": "重点分析对中东地缘、油价、伊核谈判、伊朗内部政治的影响。",
    "中东": "重点分析对地区冲突、油价/航运、宗教与民族矛盾的影响。",
    "非洲": "重点分析对资源/矿产供应、新殖民博弈、债务与民生的影响。",
    "东亚": "重点分析对中日韩朝关系、芯片/汽车产业链、地缘安全的影响。",
    "电子": "重点分析对芯片/手机/PC 产业链、关键企业竞争格局的影响。",
    "科技": "重点分析对技术路径选择、产业链上下游、企业护城河的影响。",
    "能源": "重点分析对能源结构转型、上下游价格、地缘博弈的影响。",
    "AI": "重点分析对模型/算力/应用层的影响，以及对就业、监管、地缘的传导。",
    "其他": "梳理这条新闻的实际重要性，以及可能的外溢影响。",
}

PROMPT_TEMPLATE = """你是资深时事评论员。请联网搜索这条热点的最新背景，然后用评论员视角深挖背后逻辑。

【热点】[{platform}#{rank}] {title}
【URL】{url}
【主题分类】{category}
【分析维度】{angle}

输出要求：
1) 先用一句话点出**真实驱动因素**（不是新闻表面陈述）。
2) 再用 80~150 字分析**对哪些群体/市场/格局产生哪些具体影响**。
3) 优先引用联网搜出的最新事实/数据；如证据不足或新闻可信度存疑，直接指出。
4) 只输出评论正文（一段或两段），不要标题、不要 Markdown 格式符号、不要寒暄。"""


def call_llms_with_retry(
    prompt: str,
    *,
    model: str,
    search: bool,
    retries: int = 3,
    base_delay: float = 2.0,
    timeout: int = 180,
) -> tuple[str, list[str]]:
    """调用 llms CLI，失败时指数退避重试。

    返回 (评论正文, 来源链接列表)。来源行由 llms 自己附在末尾（`— 来源 —` 之后），
    我们把这部分剥离出来单独返回。
    """
    cmd = [LLMS_BIN, "-m", model]
    if not search:
        cmd.append("--no-search")
    cmd.append(prompt)

    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if r.returncode != 0:
                last_err = (r.stderr or "").strip().splitlines()[-1] if r.stderr else "no stderr"
                raise RuntimeError(f"llms exit={r.returncode}: {last_err}")
            text = (r.stdout or "").strip()
            if not text:
                raise RuntimeError("empty stdout")
            return _split_body_sources(text)
        except (subprocess.TimeoutExpired, RuntimeError) as e:
            last_err = str(e)
            if attempt < retries:
                delay = base_delay * (2 ** (attempt - 1))
                print(f"  [retry {attempt}/{retries - 1}] {last_err} → wait {delay}s", file=sys.stderr)
                time.sleep(delay)
            else:
                raise RuntimeError(f"llms failed after {retries} attempts: {last_err}") from e
    raise RuntimeError("unreachable")


def _split_body_sources(text: str) -> tuple[str, list[str]]:
    """llms 输出末尾带"— 来源 —"段，把它切开。"""
    marker = "\n— 来源 —"
    if marker not in text:
        return text.strip(), []
    body, sources_raw = text.rsplit(marker, 1)
    sources: list[str] = []
    for line in sources_raw.strip().splitlines():
        line = line.strip()
        if line.startswith("•"):
            # 同时收集 "• title" 这一行
            sources.append(line[1:].strip())
        elif line:
            sources.append(line)
    return body.strip(), sources


def comment_one(item: dict, angle: str, *, model: str, search: bool, retries: int) -> dict:
    """对单条热点生成评论。"""
    prompt = PROMPT_TEMPLATE.format(
        platform=item["platform"],
        rank=item["rank"],
        title=item["title"],
        url=item.get("url") or "(无)",
        category=item.get("category", "其他"),
        angle=angle,
    )
    try:
        body, sources = call_llms_with_retry(
            prompt, model=model, search=search, retries=retries
        )
        return {**item, "comment": body, "sources": sources, "error": None}
    except Exception as e:
        return {**item, "comment": "", "sources": [], "error": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser(description="commentary via Gemini Pro (per-item, with retry)")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="Gemini 模型，默认 gemini-3.1-pro-preview")
    # 联网搜索：默认开启，用 --no-search 才关。
    # 采用互斥的 --search / --no-search 结构，让"开"这条路径也能显式声明。
    search_group = ap.add_mutually_exclusive_group()
    search_group.add_argument(
        "--search",
        dest="search",
        action="store_true",
        default=True,
        help="开启 Gemini 联网搜索 grounding（默认，即使不加也是开启）",
    )
    search_group.add_argument(
        "--no-search",
        dest="search",
        action="store_false",
        help="关闭 Google 联网搜索 grounding（更快，但会失去时效信息）",
    )
    ap.add_argument("--retries", type=int, default=3, help="单条失败重试次数（默认 3）")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="同时并发的评论请求数，默认 3（更高有触发限流风险）")
    ap.add_argument("--limit-per-group", type=int, default=0,
                    help="每组最多评论几条，0 表示不限制（抓多少评多少，默认）")
    ap.add_argument("--skip", default="其他", help="跳过这些分类，逗号分隔")
    ap.add_argument("--input", default="-")
    ap.add_argument("--output", default="", help="同时写 markdown 到此路径")
    args = ap.parse_args()

    if not shutil.which(LLMS_BIN) and not Path(LLMS_BIN).exists():
        print(f"ERROR: {LLMS_BIN} not found", file=sys.stderr)
        return 1

    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text("utf-8")
    payload = json.loads(raw)

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    use_search = args.search  # 新版：直接读 dest=search，True 表示开启联网

    # 把所有待评论条目铺平
    jobs: list[tuple[int, dict, str]] = []  # (序号, item, angle)
    cat_order: list[str] = []
    groups_truncated: dict[str, list[dict]] = {}

    # limit_per_group == 0 表示不截断，抓多少评多少
    per_group_cap = args.limit_per_group if args.limit_per_group > 0 else None

    for g in payload.get("groups", []):
        cat = g["category"]
        if cat in skip:
            continue
        cat_order.append(cat)
        sorted_items = sorted(g["items"], key=lambda x: x["rank"])
        items = sorted_items[:per_group_cap] if per_group_cap else sorted_items
        groups_truncated[cat] = items
        angle = ANGLE_PRESETS.get(cat, ANGLE_PRESETS["其他"])
        for it in items:
            jobs.append((len(jobs), {**it, "category": cat}, angle))

    cap_desc = f"{per_group_cap}/group" if per_group_cap else "unlimited (评所有抓到的)"
    print(f"[comment_gemini] total jobs={len(jobs)} model={args.model} "
          f"search={'ON (Gemini grounding)' if use_search else 'OFF'} "
          f"concurrency={args.concurrency} cap={cap_desc}",
          file=sys.stderr)

    # 按提交顺序保存结果
    results: dict[int, dict] = {}

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {
            pool.submit(
                comment_one, it, angle,
                model=args.model, search=use_search, retries=args.retries,
            ): idx
            for idx, it, angle in jobs
        }
        done = 0
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"error": str(e), "comment": "", "sources": []}
            results[idx] = r
            done += 1
            tag = "OK" if not r.get("error") else "ERR"
            title = r.get("title", "")[:30]
            print(f"  [{done}/{len(jobs)}] {tag}: [{r.get('platform','?')}#{r.get('rank','?')}] {title}",
                  file=sys.stderr)

    # 渲染 Markdown
    md_parts: list[str] = [
        "# 热点评论员视角分析\n",
        f"- 数据源: `{payload.get('source_db', '')}`",
        f"- 分类模型: `{payload.get('model', '')}`",
        f"- 评论模型: `{args.model}` (联网搜索: {'开' if use_search else '关'}, 单条独立调用 + {args.retries} 次重试)\n",
    ]

    all_sources: list[str] = []
    job_lookup: dict[tuple[str, int, str], int] = {}
    for idx, it, _ in jobs:
        job_lookup[(it["platform"], it["rank"], it["title"])] = idx

    for cat in cat_order:
        items = groups_truncated[cat]
        if not items:
            continue
        md_parts.append(f"### {cat}\n")
        for it in items:
            idx = job_lookup[(it["platform"], it["rank"], it["title"])]
            r = results.get(idx, {})
            header = f"- **[{it['platform']}#{it['rank']}] {it['title']}**"
            if r.get("error"):
                md_parts.append(header)
                md_parts.append(f"  > 评论生成失败：{r['error']}\n")
            else:
                md_parts.append(header)
                # 缩进评论正文
                for line in r["comment"].splitlines():
                    md_parts.append(f"  {line}")
                md_parts.append("")
                all_sources.extend(r.get("sources", []))
        md_parts.append("")

    # 去重附录
    if all_sources:
        seen: set[str] = set()
        md_parts.append("---\n## 联网检索来源\n")
        for s in all_sources:
            if s in seen:
                continue
            seen.add(s)
            md_parts.append(f"- {s}")
            if len(seen) >= 50:
                break
        md_parts.append("")

    final = "\n".join(md_parts).rstrip() + "\n"
    sys.stdout.write(final)
    if args.output:
        Path(args.output).write_text(final, "utf-8")

    failed = sum(1 for r in results.values() if r.get("error"))
    print(f"[comment_gemini] done. ok={len(results)-failed} failed={failed}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
