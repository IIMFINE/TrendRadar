---
description: 通过 MCP 服务获取最新热点，Gemini 归类与评论员视角深挖
argument-hint: "[--limit N] [--platforms a,b] [--skip 其他,社会] [--no-classify] [--no-comment]"
allowed-tools: mcp__trendradar__get-latest-news, mcp__trendradar__trigger-crawl, Bash, Read, Write
---

# /hotspot —— 热点深挖管线（MCP 版）

你是一个执行器。用户运行了 `/hotspot $ARGUMENTS` 来获取最新一轮热点的分类与评论员深度分析。

## 数据流（新版 MCP）

```
trendradar MCP 服务 (http://127.0.0.1:3333/mcp)
                ↓
    mcp__trendradar__get-latest-news (获取最新热点)
    mcp__trendradar__trigger-crawl (可选：刷新数据)
                ↓
        scripts/classify_gemini.py     (gemini-3.5-flash 归类)
                ↓
        scripts/comment_gemini.py      (gemini-3.1-pro-preview 评论员视角)
                ↓
        终端 Markdown + output/commentary/<时间戳>.md
```

## 你要做的步骤

### 1. 解析 `$ARGUMENTS`

支持的可选标志：

| 标志                       | 默认值                       | 说明                              |
|----------------------------|-----------------------------|----------------------------------|
| `--limit N`                | `50`                         | 最多获取多少条新闻               |
| `--platforms a,b`          | 全部平台                     | 仅保留这些平台                   |
| `--skip 其他,社会`         | `其他`                       | 评论阶段跳过的分类               |
| `--limit-per-group N`      | `0`（不限制）                | 每个分类最多评论几条             |
| `--no-search`              | -                            | 关闭 Gemini 联网搜索             |
| `--no-classify`            | -                            | 跳过归类，按平台分组             |
| `--no-comment`             | -                            | 只归类不评论                     |
| `--refresh`                | -                            | 评论前先触发一次抓取             |

### 2. 准备工作目录与时间戳

```bash
cd /home/pan/as_gpt/bubble-prompt/TrendRadar
mkdir -p output/commentary
ts=$(date +%Y%m%d-%H%M%S)
```

### 3. （可选）刷新数据

如果用户传了 `--refresh`，调用 MCP 工具触发抓取：

```
调用 mcp__trendradar__trigger-crawl 工具
参数: {
  "platforms": ["zhihu", "weibo", ...] 或 null (全部平台),
  "save_to_local": true
}
```

等待完成（一般 30~60 秒）。

### 4. 通过 MCP 获取热点新闻

调用 MCP 工具获取最新热点：

```
调用 mcp__trendradar__get-latest-news 工具
参数: {
  "platforms": ["zhihu", "weibo", ...] 或 null (使用所有平台),
  "limit": LIMIT (默认 50),
  "include_url": false
}
```

将返回的 JSON 数据保存到 `output/commentary/hotspots-$ts.json`。

**注意**：MCP 返回的数据格式已经是标准的新闻列表格式，包含：
- title: 新闻标题
- platform: 平台名称
- rank: 排名
- heat: 热度
- timestamp: 时间戳

### 5. 运行归类与评论管线

```bash
# 归类步骤
python3 scripts/classify_gemini.py \
  < output/commentary/hotspots-$ts.json \
  > output/commentary/classified-$ts.json

# 评论步骤
python3 scripts/comment_gemini.py \
  --limit-per-group PER_GROUP \
  --skip SKIP \
  --search \
  --output output/commentary/commentary-$ts.md \
  < output/commentary/classified-$ts.json
```

- 如果 `--no-classify`：跳过 classify_gemini，按 platform 字段聚合
- 如果 `--no-comment`：跑到 classify 那一步就停

### 6. 输出给用户

- 读取并完整展示 `commentary-$ts.md` 的内容
- 告知用户：
  - 数据落盘位置：`output/commentary/commentary-$ts.md`
  - 数据来源：trendradar MCP 服务
  - 可用选项：`--refresh`、`--skip`、`--limit-per-group` 等

## MCP 工具说明

**get-latest-news**: 获取最新一批爬取的新闻数据
- platforms: 平台ID列表，如 ['zhihu', 'weibo']，不指定则使用所有平台
- limit: 返回条数限制，默认50，最大1000
- include_url: 是否包含URL链接，默认false

**trigger-crawl**: 手动触发一次爬取任务
- platforms: 平台ID列表，不指定则使用所有平台
- save_to_local: 是否保存到本地，默认true

## 注意事项

1. **使用 MCP 服务**：所有数据获取通过 MCP 工具，不直接操作数据库
2. **MCP 服务地址**：http://127.0.0.1:3333/mcp (trendradar-mcp 容器)
3. **确保容器运行**：如果 MCP 工具调用失败，检查 `docker ps | grep trendradar-mcp`
4. **数据格式兼容**：MCP 返回的数据格式与原 fetch_hotspots.py 一致
5. **报错处理**：MCP 工具失败时，说明是哪个工具调用失败及错误信息
