#!/usr/bin/env python3
"""
classify_gemini —— 用 gemini-3.5-flash 把热点归类到固定分组。

输出格式：单一 JSON 对象，按主题聚合。

走 aicodemirror 的 Anthropic 兼容端点 (/v1/messages) 调 Gemini，
轻量、稳定、不走 grounding（归类是确定性任务不需要联网）。

用法:
  python3 scripts/fetch_hotspots.py | python3 scripts/classify_gemini.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hotspot_lib import call_messages, chunked, render_hotspot_line  # noqa: E402

DEFAULT_MODEL = "gemini-3.5-flash"

CATEGORIES = [
    "中国", "台海", "伊朗", "中东", "美国",
    "经济", "社会", "国际", "俄罗斯", "乌克兰",
    "非洲", "东亚", "电子", "能源", "科技", "AI",
    "其他",
]

SYS_PROMPT = f"""你是新闻归类助手。将给定的若干条新闻标题按主题分到下列分组中之一（每条只能分到一组，最相关的那个）：

可选分组：{', '.join(CATEGORIES)}

规则：
- "中国"=与中国大陆政治、社会、政策、领导人相关
- "台海"=两岸关系、台湾政治、对台军事
- "伊朗"/"中东"=优先"伊朗"，纯中东其他国家用"中东"
- "美国"=美国国内政治、经济、领导人、政策
- "经济"=股市/楼市/通胀/汇率/财政等经济议题
- "社会"=民生、事故、犯罪、教育、医疗
- "国际"=多国外交、跨国组织、全球性议题，无法归入具体国家时
- "俄罗斯"/"乌克兰"=各自国家或俄乌战争
- "非洲"=非洲大陆国家
- "东亚"=日韩朝（不含中国）
- "电子"=手机/芯片/PC/消费电子硬件
- "能源"=石油/电力/新能源/核能/光伏
- "科技"=互联网/航天/生物/材料等非 AI 非电子非能源的科技
- "AI"=人工智能、大模型、AI 应用
- 无法明确归类的用"其他"

输出 JSON：
{{
  "categorized": [
    {{"index": 1, "category": "中国", "reason": "理由不超过15字"}},
    ...
  ]
}}
只输出 JSON，不要任何解释或代码块。"""


def parse_json_block(text: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"no JSON object in: {text[:200]}")
    return json.loads(m.group(0))


def classify_batch(items: list[dict], model: str, max_tokens: int) -> list[dict]:
    lines = [render_hotspot_line(i + 1, it) for i, it in enumerate(items)]
    user = "请对下列新闻标题归类：\n" + "\n".join(lines)
    text = call_messages(model=model, system=SYS_PROMPT, user=user, max_tokens=max_tokens)
    data = parse_json_block(text)
    cat_by_idx: dict[int, dict] = {c["index"]: c for c in data.get("categorized", [])}

    out: list[dict] = []
    for i, it in enumerate(items, 1):
        c = cat_by_idx.get(i, {})
        out.append({**it, "category": c.get("category", "其他"), "reason": c.get("reason", "")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="classify hotspots via fast LLM")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--batch-size", type=int, default=25)
    ap.add_argument("--max-tokens", type=int, default=2500)
    ap.add_argument("--input", default="-", help="path or - for stdin (output of fetch_hotspots)")
    args = ap.parse_args()

    raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text("utf-8")
    payload = json.loads(raw)
    items = payload["items"]

    enriched: list[dict] = []
    for batch in chunked(items, args.batch_size):
        enriched.extend(classify_batch(batch, args.model, args.max_tokens))

    # 按分类聚合
    grouped: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for it in enriched:
        grouped.setdefault(it["category"], []).append(it)

    out = {
        "source_db": payload.get("source_db"),
        "model": args.model,
        "total": len(enriched),
        "groups": [
            {"category": cat, "count": len(grouped[cat]), "items": grouped[cat]}
            for cat in CATEGORIES
            if grouped[cat]
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
