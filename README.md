<div align="center">

<img src="logo.png" width="128" alt="DeepDeep" />

# DeepDeep

**简体中文** | [English](README_EN.md)

AstrBot 搜索管理器：让 DeepSeek 搜索、AstrBot 内置网页搜索和各种搜索插件**分工协作**，自带自动降级、费用上限、用量统计和调用日志

[![CI](https://github.com/liquidtoy001/astrbot_plugin_deepdeep/actions/workflows/ci.yml/badge.svg)](https://github.com/liquidtoy001/astrbot_plugin_deepdeep/actions/workflows/ci.yml)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.26.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)

</div>

## 它解决什么问题

装了好几个搜索工具之后，常见的情况是：模型在一堆功能重叠的工具里挑错、某个服务欠费或故障就搜不了、不知道每个工具用了多少次花了多少钱、出了问题也查不到当时调用了什么。

DeepDeep 把这些搜索工具**管起来**：

- **分工**：通用搜索交给最合适的一个，图片、视频、学术、GitHub 等交给各自的专用工具，并在每次请求里告诉模型谁擅长什么
- **路由与降级**：默认不隐藏任何工具，只告诉模型通用搜索先用谁、没搜到或结果不好再换谁；DeepSeek 没有 Key、超出预算、故障或单次调用失败时，引导模型改用其他搜索工具，同一轮对话里就能接着搜
- **费用控制**：人民币 / 美元计费，日 / 月费用上限；超出上限时不会悄悄改用同样走 DeepSeek 计费的工具
- **用量统计**：每个搜索工具的调用次数、成功率、失败、平均耗时
- **调用日志**：记录**所有工具**的每次调用，持久保存、可筛选、可导出，排查问题有据可查
- **可视化面板**：在 AstrBot WebUI 里查看和修改上述一切，保存即生效
- **零额外依赖**，只用 AstrBot 自带的库

> **关于 AstrBot 内置的 DeepSeek 搜索**：AstrBot 4.29.1 起在“网页搜索”里内置了 DeepSeek 搜索服务。如果只需要基础的 DeepSeek 搜索，用内置的就够了。DeepDeep 适合同时用多个搜索工具、需要自动降级、费用控制、统计和日志的场景；它的搜索结果还会带上发布日期、相关度和权威度，便于模型判断信息新旧（内置版本只返回标题、链接和摘要）。两者可以同时开启，DeepDeep 会把内置的 DeepSeek 搜索当作备用。

## 安装

要求 **AstrBot ≥ 4.26.0**（插件页面和 `astrbot.api.web` 自 4.26.0 起提供）。已在 4.26.0 和 4.29.1 上实际加载验证；与 AstrBot 内置 DeepSeek 搜索的配合需要 4.29.1 及以上。

在 AstrBot WebUI 的插件市场搜索「DeepDeep」安装，或在插件页选择「从链接安装」：

```text
https://github.com/liquidtoy001/astrbot_plugin_deepdeep
```

如果已经在 AstrBot 里添加过 DeepSeek 官方提供商（api_base 为 `https://api.deepseek.com`），插件会自动复用它的 Key，装完即可使用；否则在插件配置里填入 API Key。

## 搜索分工

每条分工规则写成“工具名规则 = 擅长什么”（支持 `*` 通配）。匹配的工具视为**专用搜索**：始终保留，不算通用搜索的备用。默认规则：

| 规则 | 擅长 |
|---|---|
| `*image*` | 找图片 |
| `*video*` | 找视频（B 站、YouTube 等视频平台） |
| `*music*` / `*map*` | 找音乐 / 地点和地图 |
| `*finance*` | 股票、汇率等财经行情 |
| `*academic*`、`*scholar*` / `*paper*` | 学术论文和期刊 / arXiv 等论文 |
| `*github*`、`search_repositories`、`search_code`、`search_issues`、`search_pull_requests` | GitHub 仓库、代码、Issue、PR |
| `*aur*` / `*code-context*` / `*webshot*` | AUR 软件包 / 编程文档 / 网页截图 |
| `astr_kb_search` | 本地知识库 |

通用搜索还可以**按问题类型分工**（「按问题类型分工」，每条写成“问题类型 = 工具名规则, …”）：本次请求里第一个存在的工具作为这类问题的首选，其余问题仍先用 DeepDeep。默认一条：

| 问题类型 | 首选 |
|---|---|
| 编程、软件版本、安全漏洞、官方文档等技术问题 | `deepdeep_tavily_search`（DeepDeep 自带）或 `web_search_tavily`（AstrBot 内置） |

这条来自 2026-10 的多引擎对比：DeepSeek 平均 7.6 分（满分 10）最高，但技术题只有 6.8；Tavily 技术题 8.8 分，不过不带发布日期、时效题较弱。分工后模型 9/9 次把技术题交给 Tavily，其余仍走 DeepDeep，结果质量平均从 7.2 提高到 8.4。

每次请求时，DeepDeep 在系统提示末尾加一小段**搜索工具分工**，只列出本次实际可用的工具，例如：

```text
【搜索工具分工】先选最对口的工具，结果不理想时再换：
- 新闻时事、中文资料、海外新闻、比赛和最新动态：优先 deepdeep_web_search；没有结果或结果不理想时再用 deepdeep_tavily_search、web_search_serpapi、searxng_web_search_general 等 10 个
- 编程、软件版本、安全漏洞、官方文档等技术问题：优先 deepdeep_tavily_search；不理想时再用 deepdeep_web_search
- 读取网页全文：优先 deepdeep_web_fetch；读取失败时再用 searxng_web_fetch_url
- 找图片：searxng_web_search_images
- 找视频（B 站、YouTube 等视频平台）：searxng_web_search_videos
- GitHub 上的仓库、代码、Issue 和 PR：search_code、search_issues、search_pull_requests 等 5 个
```

这段内容只取决于工具集和配置，工具集不变时完全相同，不会破坏模型服务端的提示词缓存。可以在设置中关闭。

## 实测：分工说明有没有用

用作者部署里的真实工具列表（79 个工具，含 SearXNG、Tavily、arXiv 和 GitHub 的 MCP 工具、知识库等）和 39 个问题，让 deepseek-flash 各回答 3 次，看它**第一个调用的工具**是否最对口。问题覆盖：中文新闻和时事、英文与技术问题、读网页、图片、视频、音乐、地点、arXiv 和期刊论文、GitHub 仓库 / Issue / 代码 / PR、知识库，以及 4 个**不需要搜索**的问题（讲笑话、算术、翻译、写诗）。

| 方式 | 首选工具 | 可接受 | 选错 | 没调用工具 |
|---|---|---|---|---|
| 不装 DeepDeep | 92 / 117 | 7 | 6 | 12 |
| 装 DeepDeep，但不加分工说明 | 85 / 117 | 12 | 8 | 12 |
| **只引导（默认）** | **95 / 117** | 6 | 7 | 9 |
| DeepSeek 优先（隐藏同类工具） | 94 / 117 | 5 | 8 | 10 |

- 多一个搜索工具却不说明分工时，选择明显变差；加上分工说明后恢复，并略好于不装插件
- 提升集中在专用场景：问 B 站视频时三次都改用视频搜索（没有说明时三次都用通用搜索），找歌时更常去用音乐搜索
- 4 个不需要搜索的问题，四种方式都是 12 次全部没有调用工具：分工说明不会引起多余的搜索
- 计为“选错”的里有一部分其实合理：问“Kubernetes 最新版本”“AstrBot 最近合并了哪些 PR”时，模型用了 GitHub MCP 的 `get_latest_release`、`list_pull_requests`
- 两种模式的选择质量接近；“只引导”不改变工具列表、不影响提示词缓存，所以作为默认
- 局限：测试用的是简短人设，没有长对话历史和完整系统提示；每题 3 次；只测了 deepseek-flash。所有评测共花费约 1.2 元

## 对提示词缓存和 token 的影响

- **分工说明**放在系统提示末尾，通常 150–250 token，每类最多列 3 个工具。内容只取决于本次可用的工具和配置，工具不变时每次完全相同，命中缓存后几乎不花钱（deepseek-flash 缓存命中价格为每百万 token 0.02–0.04 元）
- **“只引导”模式（默认）**：工具列表和分工说明始终不变，DeepSeek 不可用时由 DeepDeep 的工具结果告诉模型改用谁，**不会让缓存失效**。代价是所有搜索工具的定义都发给模型（以 SearXNG 插件加内置 Tavily 为例约多 1000 token），但这些 token 能命中缓存，几乎不额外花钱
- **“DeepSeek 优先”模式**：隐藏同类工具，请求更短、选择更集中；但 DeepSeek 在可用 / 不可用之间切换（超出预算、熔断打开或恢复）、对话中途放出备用工具时，工具列表会变化，**每次变化让缓存失效一次**（deepseek-flash 未命中价格约为命中的 50 倍）。适合上下文较短的场景
- 两种模式下，修改路由或分工设置、升级插件、增删其他插件或 MCP 工具都会改变工具列表或说明，各让缓存失效一次
- **实测**（作者的部署，单个会话上下文约 80 万 token）：同一会话连续对话时缓存命中率 99.8%–100%；缓存失效只出现在插件升级后的第一次请求。上下文很长时，一次缓存失效约等于“上下文长度 × 未命中单价”，例如 80 万 token 约 0.8–1.6 元

## 路由与降级

不想用 DeepDeep 自带的 DeepSeek 搜索（比如改用 AstrBot 内置的 DeepSeek 搜索）时，关掉设置里的「启用 DeepDeep 自带的搜索和抓取」即可：每次请求都会把 `deepdeep_web_search` / `deepdeep_web_fetch` 拿掉，不改 AstrBot 的工具开关，分工说明、调用日志、健康检测照常工作，分工说明里的首选换成其他通用搜索。

通用搜索按**路由模式**决定由谁负责：

| 模式 | DeepSeek 可用时 | DeepSeek 故障时 | 超出费用上限时 |
|---|---|---|---|
| **只引导**（默认） | 所有工具都给模型，分工说明写明先用 DeepDeep，没搜到或结果不好再换 | 工具不变；模型调用 DeepDeep 时收到“请改用 X”的建议 | 同左，建议里**不包括**同样走 DeepSeek 计费的工具 |
| DeepSeek 优先 | 只给模型 DeepDeep 的通用搜索，隐藏同类工具 | 换回其他通用搜索工具 | 换回其他通用搜索工具，**但不包括**同样走 DeepSeek 计费的工具 |
| 只用 DeepSeek | 只用 DeepDeep | 仍只用 DeepDeep，模型收到“暂不可用”的说明 | 同左 |
| 暂停 DeepSeek | 只用其他搜索工具 | 同左 | 同左 |
| 不干预 | 所有搜索工具并存 | 同左 | 同左 |

“故障”包括没有 API Key、连续失败触发熔断、单次调用失败。DeepSeek **没搜到结果或结果相关度都很低**时不算故障，但也会建议模型换一个通用搜索工具再试；网页抓取失败时只推荐其他抓取工具。专用搜索在任何模式下都保留。

注意：“只引导”模式下所有工具对模型可见，超出费用上限后 DeepDeep 只能**建议**模型不要使用 AstrBot 内置的 DeepSeek 搜索等同样走 DeepSeek 计费的工具，不能强制拦截。需要严格控制这部分费用时，请使用“DeepSeek 优先”模式，或不同时开启内置的 DeepSeek 搜索。

为了不和 AstrBot 及其他插件冲突：

- 只改动单次请求的工具列表，**不修改 AstrBot 全局的工具开关**，也不改其他插件的配置
- 本次请求里没有 DeepDeep 工具时（例如人格只启用了部分工具、本插件在当前会话被禁用）什么都不动
- 只隐藏 DeepDeep 能替代的那一类：请求里只有搜索工具时，其他插件的网页抓取工具保留，反之亦然
- 路由在其他插件的 `on_llm_request` 钩子之后执行，它们追加的工具也能被正确识别

## 兼容性

2026-10 对照 AstrBot 插件市场整理。下表中的工具会被识别为通用搜索的备用：“只引导”模式下写进分工说明的“再用”名单，“DeepSeek 优先”模式下在 DeepSeek 可用时隐藏、需要时放出：

| 来源 | 搜索工具 | 抓取工具 |
|---|---|---|
| AstrBot 内置网页搜索 | `web_search_tavily`、`web_search_bocha`、`web_search_brave`、`web_search_firecrawl`、`web_search_baidu`、`web_search_exa`、`web_search_anysearch`、`web_search_serpapi`、`web_search_deepseek`¹（SerpApi 的图片、学术、财经工具为专用搜索） | `tavily_extract_web_page`、`firecrawl_extract_web_page`、`exa_get_contents`、`deepseek_fetch_web_page`¹ |
| [web_searcher_pro](https://github.com/zouyonghe/astrbot_plugin_web_searcher_pro)（SearXNG） | `searxng_web_search_*`（图片、视频、音乐、地图、学术为专用搜索） | `searxng_web_fetch_url` |
| [kimi_web_search](https://github.com/Tsukumi233/astrbot_plugin_kimi_web_search) | `kimi_web_search` | `kimi_web_fetch` |
| [grok_web_search](https://github.com/piexian/astrbot_plugin_grok_web_search) / grok_suite | `grok_web_search` | `grok_web_fetch` |
| [mmx_cli_tool](https://github.com/piexian/astrbot_plugin_mmx_cli_tool) | `mmx_web_search` | |
| [anysearch](https://github.com/AgIzT/astrbot_plugin_anysearch) | `anysearch_search`、`anysearch_batch_search` | `anysearch_extract` |
| [gemini_search](https://github.com/Chris95743/astrbot_plugin_gemini_search) | `gemini_search` | `web_fetch` |
| [exa_web_search](https://github.com/piexian/astrbot_plugin_exa_web_search) | `exa-search` | `web_fetch_exa` |
| 其他 DeepSeek 搜索插件（[SkyL3gend](https://github.com/SkyL3gend/astrbot_plugin_deepseek_search)、[Fantasality](https://github.com/Fantasality/astrbot_plugin_deepseek_search)） | `deepseek_web_search`¹、`web_search_deepseek`¹ | `deepseek_fetch_web_page`¹ |

¹ 同样走 DeepSeek 计费：超出 DeepDeep 的费用上限后不会被推荐或放出。

不在表里的搜索工具：在面板的「工具路由预览」里看是否被识别，没有的话把工具名加进「可互相替代的通用搜索」或「搜索分工规则」，也欢迎提 issue 补充默认规则。DeepDeep 的工具名（`deepdeep_web_search`、`deepdeep_web_fetch`）和命令名（`/deepdeep`）都不与上述插件重名。

## DeepDeep 自带的搜索工具

| 工具 | 作用 |
|---|---|
| `deepdeep_web_search(query, count)` | 通过 [DeepSeek Search API](https://api-docs.deepseek.com/zh-cn/guides/search) 联网搜索。每条结果带长摘要、发布日期、相关度和权威度；支持 `site:`、`-site:`、`"精确短语"`、`lang:zh,en`、`after:2026-01-01`、`before:2026-06` 等语法 |
| `deepdeep_web_fetch(url)` | 通过 DeepSeek Fetch API 读取网页全文（Markdown） |
| `deepdeep_tavily_search(query, count)` | 通过 Tavily 搜索，负责技术类问题，也是 DeepSeek 的第一备用。Key 默认复用 AstrBot「网页搜索」里的 Tavily Key（内置网页搜索选了别家也能用）；没有 Key、或内置网页搜索本身就是 Tavily 时自动不提供。Tavily 免费额度每月 1000 次 |

工具结果第一行是统一的状态行，带 6 位请求 ID，可与日志、面板对照：

```text
[DeepSeek 搜索 #a1b2c3] 成功 · 6 条结果 · 456 ms
[DeepSeek 搜索 #d4e5f6] 失败 · HTTP 402 · 已重试 1 次
[DeepSeek 抓取 #0a0b0c] 未调用 · 已达到今日费用上限 ¥0.500
```

每次向 DeepSeek 要至少 10 条结果（按次计费，结果多少不影响费用），给模型看的仍是 `count` 条左右：

- 达到最低相关度的结果不足 3 条时，用排在后面的结果补足，并标明“第 N 条起相关度较低”，免得模型只看到一两条就认定没查到
- 问“最新、最近”或用 `after:`/`before:` 限定时间时，不按相关度过滤（新内容的分数通常偏低），提醒模型结果按相关度而不是时间排序，“本次最新一条发布于”按全部结果计算，并把日期更新但相关度靠后的结果（最多 2 条）附在后面

## 插件面板

在 AstrBot WebUI 的插件页点开本插件，进入「DeepDeep 面板」（顶部导航栏可以直接跳到各区块）：

- **状态**：正常 / 已降级及原因、Key 来源、测试连接、解除熔断
- **配置检查**：DeepDeep 的 DeepSeek / Tavily Key、AstrBot 内置网页搜索当前提供商的 Key、其他搜索插件的 API Key 和 SearXNG 地址有没有填；缺少配置时写明去哪里补（只看“填了没有”，不读取 Key 的内容）
- **处理提醒**：每条提醒可以「忽略」，插件和 MCP 工具还可以一键「停用」（调用 AstrBot 的工具开关，点两次确认）；处理过的收进「已处理」，可随时重新启用或恢复提醒
- **主力搜索工具**：通用搜索、网页读取的首选和备用工具，各专用搜索的分工（与告诉模型的分工说明一致），每个工具带健康状态；近 24 小时表现异常的工具会在这里提示
- **费用**：今日 / 本月费用和上限进度（计费币种，并按汇率换算另一种币种）、近 30 天 DeepSeek 计费次数
- **各搜索工具用量**：近 24 小时 / 7 天 / 30 天每个搜索工具的调用次数、成功率、失败、拦截、平均耗时
- **网络连通性**：每个搜索工具走云端 API 还是自建服务（内网）、服务商在国内还是国外、能搜到国内 / 国外哪些网站；一键检测（见下文）
- **设置**：常用的自带搜索开关、路由模式、计费币种、费用上限放在外面，汇率、熔断、匹配规则、分工规则、调用日志收在「高级设置」里；修改后自动保存、立即生效（与 AstrBot 插件配置页是同一份配置）
- **工具路由预览**：每个工具的类型（DeepDeep / 备用 / 专用及其擅长）以及在 DeepSeek 正常、故障、超出预算时是否可见；默认只显示前 6 个搜索工具
- **调用日志**：按时间、来源、工具、结果、关键词或请求 ID 筛选，分页，展开详情，导出 JSON Lines，清空
- **诊断信息**：一键生成，反馈问题时附上（不含 API Key）

## 调用日志与统计

DeepDeep 通过 AstrBot 的工具调用钩子（`on_using_llm_tool` / `on_llm_tool_respond`）记录**所有工具**的调用，保存在 `data/plugin_data/astrbot_plugin_deepdeep/calls.db`（SQLite），重启不丢，默认保留 14 天（另有 50000 条上限）。「各搜索工具用量」和 `/deepdeep stats` 的数据都来自这里。

| 记录内容 | DeepDeep 工具 | 其他工具（默认） | 其他工具（开启完整记录后） |
|---|---|---|---|
| 工具名、来源（内置 / 插件名 / MCP 服务）、会话、耗时、成功或失败 | ✅ | ✅ | ✅ |
| 调用参数、返回结果 | ✅（可关闭） | ❌ 只记返回内容的长度 | ✅ 结果最多保存 8000 字 |
| 请求过程、错误原文、结果的相关度和日期、降级情况 | ✅ | — | — |

- 工具抛出异常时 AstrBot 不会返回结果，这类调用会在对话结束时标记为“失败”；AstrBot 重启时还没结束的调用标记为“中断”
- 只记录本插件已启用的会话里的调用
- DeepDeep 的调用同时写入 AstrBot 官方「追踪」（Trace）

## 搜索工具健康检测

有的搜索工具会“看起来能用、其实已经失效”，比如自建 SearXNG 的服务器 IP 被 DuckDuckGo、Brave 等拦截后，只剩一两个引擎返回和问题毫无关系的结果。DeepDeep 从调用日志判断每个搜索工具近 24 小时的健康状况：

| 算作有问题 | 判断方式 |
|---|---|
| 失败 | 工具报错，或返回的只是一小段错误信息（被拦截、验证码、限流、超时等） |
| 空结果 | 没有返回内容，或明确说“没有找到” |
| 结果不相关 | 结果里几乎找不到搜索词（开头和 JSON 里回显的搜索词不算） |

至少 3 次调用才下结论：七成以上有问题为「基本失效」，三成半以上或最近连续 3 次有问题为「不稳定」。相关度在内存里计算，数据库只保存分数，不保存其他工具的结果内容。

**只做提示**：显示在面板顶部、「各搜索工具用量」和 `/deepdeep stats` 里，不会自动隐藏工具或改动分工说明（改动会让提示词缓存失效）。需要时请在 AstrBot 里关掉失效的工具。

## 网络连通性

搜索工具分两种，网络问题的表现完全不同：

- **云端 API**（DeepSeek、SerpApi、Tavily、博查……）：搜索在服务商的服务器上完成，你的服务器只要连得上它的接口；能搜到国内还是国外网站取决于服务商。DeepDeep 的网页抓取也由 DeepSeek 的服务器去抓，服务器自己打不开的网站也能读
- **自建服务（内网）**（如 `http://searxng:8080`）：请求从你的服务器发往各搜索引擎，服务器访问不了、或者服务器 IP 被拦截的引擎就没有结果；直接抓网页的工具同理

面板里列出每个后端的运行方式、接口地址、服务商所在地和国内 / 国外网站覆盖。点「检测连通性」会从服务器打开几个国内外网站首页、访问各接口地址，并真实查询一次 SearXNG（地址从其他插件的配置里自动读取），列出能用和被拦截的引擎。检测不调用任何计费接口。

## 配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| DeepSeek API Key | 空 | 留空时复用 DeepSeek 官方提供商的 Key |
| 启用 DeepDeep 自带的搜索和抓取 | 开 | 关闭后由其他搜索工具负责，见「路由与降级」 |
| 启用 DeepDeep 自带的 Tavily 搜索 | 开 | 没有 Key 或内置网页搜索就是 Tavily 时自动不提供 |
| Tavily API Key | 空 | 留空时复用 AstrBot「网页搜索」里的 Tavily Key |
| 路由模式 | 只引导 | 见「路由与降级」 |
| 计费币种 | 人民币 | 中国站每千次 ¥5，国际站每千次 $0.75，按你的账户选择 |
| 每日 / 每月费用上限 | 0 / 30 | 按计费币种计算，0 表示不限制；按服务器本地日期统计 |
| 汇率 | 7.1 | 1 美元兑人民币，只用于换算显示 |
| 可互相替代的通用搜索 | 见「兼容性」 | 工具名规则，支持 `*` 通配 |
| 搜索分工规则 | 见「搜索分工」 | 每条“规则 = 擅长什么” |
| 按问题类型分工 | 技术问题 → Tavily | 每条“问题类型 = 工具名规则, …” |
| 向模型说明搜索工具分工 | 开 | |
| 熔断阈值 / 冷却 | 3 次 / 300 秒 | 余额不足或 Key 无效时立即暂停 30 分钟 |
| 默认结果数 / 上限 | 5 / 10 | DeepSeek 按请求次数计费，与结果数无关；插件每次向 DeepSeek 要至少 10 条用于补足和找最新结果 |
| 最低相关度 | 0.5 | 达标的不足 3 条时用后面的结果补足 |
| 搜索 / 抓取字数上限 | 6000 / 8000 | |
| 附带用户原始问题 | 开 | 把用户消息前 128 字作为 `ori_question` 发给 DeepSeek |
| 缓存时长 | 600 秒 | 0 为关闭 |
| 超时 / 重试 / 限速 | 20 秒 / 2 次 / 5 QPS | DeepSeek 按账号限速，搜索和抓取各 5 QPS |
| 记录工具调用日志 | 开 | |
| 记录 DeepDeep 的搜索词和结果 | 开 | |
| 记录其他工具的完整参数和结果 | 关 | 默认只记名称、来源、耗时和状态 |
| 调用日志保留天数 | 14 | |

## 管理命令（仅管理员）

| 命令 | 作用 |
|---|---|
| `/deepdeep stats` | 今日 DeepSeek 用量、今日 / 本月费用、当前状态、近 24 小时各搜索工具调用次数，以及可能失效、配置不完整的搜索工具 |
| `/deepdeep test` | 测试 DeepSeek Key 和网络（计费 1 次） |
| `/deepdeep reset` | 解除熔断 |

## 费用

DeepDeep 自带工具使用 DeepSeek Search & Fetch API：

| 账户 | 搜索 | 抓取 |
|---|---|---|
| 中国站（人民币） | 每 1000 次成功请求 5 元 | 每 1000 次成功请求 5 元 |
| 国际站（美元） | 每 1000 次成功请求 0.75 美元 | 每 1000 次成功请求 0.75 美元 |

与返回结果数无关；HTTP 非 200、搜索无结果、抓取无内容和超时都不计费。DeepSeek 公告中“Harness、Claude Code、Codex 的服务端工具调用在 2026 年 11 月 1 日前免费”**不适用于本插件**。面板和命令里的费用为本地估算，以 DeepSeek 平台账单为准。其他搜索工具的费用由各自服务计费，DeepDeep 只统计调用次数。

## 排查问题

同一个请求 ID 出现在三处：工具结果第一行（开启 AstrBot 的“显示工具调用结果”后聊天里也能看到）、面板「调用日志」（在关键词框输入 ID 即可找到）、AstrBot 日志（所有日志以 `[deepdeep]` 开头）。

| 日志级别 | 内容 |
|---|---|
| DEBUG | 每次调用的结果、耗时、重试次数；隐藏模式下每次请求隐藏了哪些工具 |
| INFO | 状态变化：DeepSeek 变为不可用 / 恢复、熔断打开、降级、设置保存 |
| WARNING | 调用失败（含 HTTP 状态和 DeepSeek 返回的错误） |

日志里不会出现 API Key；搜索词只在 DEBUG 级别出现，并遵循“记录搜索词”开关。反馈问题时请附上面板生成的「诊断信息」。

## 常见问题

**DeepSeek 搜索是模型自带的吗？**

不是。DeepDeep 调用的是 DeepSeek 单独提供的 Search / Fetch 接口，和用哪个聊天模型无关，AstrBot 用 deepseek-flash、Qwen、GLM 或其他模型都能用。DeepSeek 另有一种由服务器在生成回答时自己搜索的方式（Responses API 的 `web_search`），目前只支持 deepseek-v4-pro，AstrBot 用不上。

**和 AstrBot 内置的 DeepSeek 搜索、其他 DeepSeek 搜索插件是什么关系？**

它们提供搜索工具本身；DeepDeep 负责多个搜索工具之间的分工、路由、降级、费用、统计和日志。可以同时安装，DeepDeep 会按规则把它们当作备用。

## 隐私

- 发送给 DeepSeek 的内容：模型生成的搜索词、用户消息的前 128 字（可关闭）、要抓取的链接
- 不使用 `localization` 参数，不发送用户 IP
- 用量统计保存在 `data/plugin_data/astrbot_plugin_deepdeep/usage.json`，只有每天的次数
- 调用日志保存在本地 `calls.db`，只有登录 AstrBot 管理面板的用户能查看，默认保留 14 天，可随时清空
- 其他工具的参数和结果默认不保存；开启「记录其他工具的完整参数和结果」后会保存，其中可能包含用户的聊天内容

## 开发

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)：插件框架；插件面板、工具钩子、配置和国际化均按其官方文档实现
- [DeepSeek Search & Fetch API](https://api-docs.deepseek.com/zh-cn/guides/search)：DeepDeep 自带搜索工具使用的接口
- [astrbot_plugin_qq_group_daily_analysis](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)：CI 检查、按 `metadata.yaml` 版本号自动发布、问题模板的组织方式参考了该项目（本项目的工作流为独立编写）
- 「兼容性」一节列出的各搜索插件：默认规则依据它们公开的工具名整理，本项目未使用它们的代码

## 许可证

[MIT](LICENSE)
