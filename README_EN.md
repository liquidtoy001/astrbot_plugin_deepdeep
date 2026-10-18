<div align="center">

<img src="logo.png" width="128" alt="DeepDeep" />

# DeepDeep

[简体中文](README.md) | **English**

A search manager for AstrBot: lets DeepSeek search, AstrBot's built-in web search and community search plugins **work together by specialty**, with automatic fallback, spending caps, usage stats and a call log

[![CI](https://github.com/liquidtoy001/astrbot_plugin_deepdeep/actions/workflows/ci.yml/badge.svg)](https://github.com/liquidtoy001/astrbot_plugin_deepdeep/actions/workflows/ci.yml)
![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.26.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)

</div>

## What it solves

With several search tools installed, the model often picks the wrong one among overlapping tools, search breaks when one service runs out of credit or goes down, you can't tell how much each tool is used or costs, and when something goes wrong there is no record of what was called.

DeepDeep **manages** your search tools:

- **Division of labor**: general web search goes to the best general engine; images, videos, papers, GitHub and so on go to their specialist tools, and each request tells the model which tool is good at what
- **Routing and fallback**: by default no tool is hidden; the model is told which general search to use first and which to try when results are empty or poor. With no key, over budget, during an outage or after a failed call, the model is steered to other search tools, even in the middle of a conversation
- **Cost control**: CNY or USD billing, daily and monthly caps; hitting a cap never silently switches to another tool billed by the same DeepSeek account
- **Usage stats**: calls, success rate, failures and average latency for every search tool
- **Call log**: every call of **every tool** is stored persistently, filterable and exportable
- **Dashboard**: view and change all of the above in the AstrBot WebUI; changes apply immediately
- **No extra dependencies**, only libraries bundled with AstrBot

> **About AstrBot's built-in DeepSeek search**: since AstrBot 4.29.1, DeepSeek is available as a built-in web search provider. If basic DeepSeek search is all you need, the built-in one is enough. DeepDeep is for setups with several search tools that need fallback, cost control, stats and logs. Its own results also include publish date, relevance and authority, so the model can tell new from old (the built-in tool returns title, link and summary only). Both can be enabled together; DeepDeep treats the built-in DeepSeek search as a fallback.

## Installation

Requires **AstrBot ≥ 4.26.0** (plugin pages and `astrbot.api.web` arrived in 4.26.0). Verified to load on 4.26.0 and 4.29.1; working alongside AstrBot's built-in DeepSeek search needs 4.29.1 or later.

Search for "DeepDeep" in the AstrBot plugin market, or install from URL:

```text
https://github.com/liquidtoy001/astrbot_plugin_deepdeep
```

If you already added an official DeepSeek provider (api_base `https://api.deepseek.com`), DeepDeep reuses its key and works right away. Otherwise enter an API key in the plugin settings.

## Division of labor

Each specialist rule reads "tool pattern = what it is good at" (`*` wildcard). Matching tools are **specialists**: always kept and never hidden as fallbacks. Defaults:

| Pattern | Good at |
|---|---|
| `*image*` | Images |
| `*video*` | Videos (Bilibili, YouTube and other video sites) |
| `*music*` / `*map*` | Music / places and maps |
| `*finance*` | Stocks, exchange rates and other market data |
| `*academic*`, `*scholar*` / `*paper*` | Papers and journals / arXiv papers |
| `*github*`, `search_repositories`, `search_code`, `search_issues`, `search_pull_requests` | GitHub repositories, code, issues, PRs |
| `*aur*` / `*code-context*` / `*webshot*` | AUR packages / programming docs / page screenshots |
| `astr_kb_search` | Local knowledge base |

General search can also be **split by question type** ("Question-type rules", each "question type = tool patterns, ..."): the first matching tool present in the request becomes the first choice for that type, everything else still goes to DeepDeep first. The default sends programming, software versions, vulnerabilities and official docs to `deepdeep_tavily_search` (built in) or `web_search_tavily` (AstrBot). In a 2026-10 comparison DeepSeek scored highest overall (7.6/10) but only 6.8 on technical questions, where Tavily scored 8.8; with the rule the model sent 9/9 technical questions to Tavily and result quality rose from 7.2 to 8.4.

On each request DeepDeep appends a short **search tool guide** to the system prompt, listing only the tools actually available in that request. Its content depends only on the tool set and settings, so it stays identical while the tool set is unchanged and does not break prompt caching. It can be turned off.

## Does the guide help? (measured)

Using the author's real tool list (79 tools, including SearXNG, Tavily, arXiv and GitHub MCP tools and the knowledge base) and 39 questions, deepseek-flash answered each 3 times and we checked whether its **first tool call** was the best fit. The questions cover Chinese news, English and technical questions, reading pages, images, videos, music, places, arXiv and journal papers, GitHub repos / issues / code / PRs, the knowledge base, and 4 questions that **need no search** (a joke, arithmetic, translation, a poem).

| Setup | Best tool | Acceptable | Wrong tool | No tool |
|---|---|---|---|---|
| Without DeepDeep | 92 / 117 | 7 | 6 | 12 |
| DeepDeep without guide | 85 / 117 | 12 | 8 | 12 |
| **Guide only (default)** | **95 / 117** | 6 | 7 | 9 |
| DeepSeek first (hide overlapping) | 94 / 117 | 5 | 8 | 10 |

- Adding a search tool without a guide made choices clearly worse; the guide recovers that and does slightly better than no plugin
- Gains are in specialist cases: Bilibili questions went to video search 3 of 3 times (0 of 3 without a guide), song requests used music search more often
- The 4 no-search questions never triggered a tool call in any setup (12 of 12): the guide does not cause unnecessary searches
- Some "wrong" picks are reasonable: for "latest Kubernetes version" and "recently merged AstrBot PRs" the model used the GitHub MCP's `get_latest_release` and `list_pull_requests`
- Both modes choose about equally well; guide only never changes the tool list and keeps prompt caching intact, so it is the default
- Limits: short persona without long history or a full system prompt, 3 runs per question, deepseek-flash only. All evaluation runs cost about ¥1.2

## Effect on prompt caching and tokens

- **The guide** sits at the end of the system prompt, usually 150–250 tokens with at most 3 tools per line. It is identical while the tool set is unchanged and costs almost nothing once cached (deepseek-flash cache hits are ¥0.02–0.04 per million tokens)
- **Guide only (default)**: the tool list and guide never change; when DeepSeek is unavailable, DeepDeep's tool result tells the model what to use instead, so **the cache is never invalidated** by DeepDeep. All search tool definitions are sent (about 1,000 more tokens with SearXNG plus Tavily), but they are cached and cost almost nothing
- **DeepSeek first**: hides overlapping tools for shorter, more focused requests, but every switch between available and unavailable, and every mid-conversation restore, changes the tool list and **invalidates the cache once** (misses cost about 50× hits on deepseek-flash). Better for short contexts
- In both modes, changing routing or specialist settings, upgrading the plugin, or adding or removing other plugins or MCP tools invalidates the cache once
- **Measured** on the author's deployment (about 800k tokens of context): 99.8–100% cache hits across consecutive turns; misses only after plugin upgrades. With long contexts one miss costs about "context length × miss price", e.g. ¥0.8–1.6 for 800k tokens

## Routing and fallback

If you would rather not use DeepDeep's own DeepSeek search (for example because you switched to AstrBot's built-in DeepSeek search), turn off "Enable DeepDeep's own search and fetch". The two tools are then removed from every request without touching AstrBot's tool switches; the guide, call log and health checks keep working, and the guide names another general search as the first choice.

| Mode | DeepSeek available | DeepSeek down | Over spending cap |
|---|---|---|---|
| **Guide only** (default) | All tools visible; the guide says use DeepDeep first and switch when results are empty or poor | Tools unchanged; calling DeepDeep returns "use X instead" | Same, suggestions **exclude** DeepSeek-billed tools |
| DeepSeek first | Only DeepDeep for general search, overlapping tools hidden | Other general search tools come back | Other general tools come back, **except** DeepSeek-billed ones |
| DeepSeek only | DeepDeep only | Still DeepDeep only; the model is told search is unavailable | Same |
| Pause DeepSeek | Other search tools only | Same | Same |
| Don't interfere | All search tools | Same | Same |

"Down" means no API key, an open circuit breaker or a failed call. **Empty or uniformly low-relevance results** are not failures, but the model is still advised to try another general search tool; a failed page fetch only suggests other fetch tools. Specialists are kept in every mode.

Note: in guide-only mode every tool is visible, so once the spending cap is reached DeepDeep can only **advise** the model not to use other DeepSeek-billed tools such as AstrBot's built-in DeepSeek search; it cannot block them. For strict control, use DeepSeek-first mode or don't enable the built-in DeepSeek search at the same time.

To avoid conflicts with AstrBot and other plugins:

- Only the tool list of a single request is changed; **AstrBot's global tool switches and other plugins' settings are never touched**
- Requests without DeepDeep's tools (for example a persona with a tool whitelist, or the plugin disabled in that session) are left alone
- Only the kind DeepDeep can replace is hidden: if a request has only the search tool, other plugins' fetch tools stay, and vice versa
- Routing runs after other plugins' `on_llm_request` hooks, so tools they add are recognized too

## Compatibility

Compiled against the AstrBot plugin market as of 2026-10. These tools are recognized as general search fallbacks: listed as alternatives in the guide in guide-only mode, hidden while DeepSeek is available and restored when needed in DeepSeek-first mode:

| Source | Search tools | Fetch tools |
|---|---|---|
| AstrBot built-in web search | `web_search_tavily`, `web_search_bocha`, `web_search_brave`, `web_search_firecrawl`, `web_search_baidu`, `web_search_exa`, `web_search_anysearch`, `web_search_serpapi`, `web_search_deepseek`¹ (SerpApi image, scholar and finance tools count as specialists) | `tavily_extract_web_page`, `firecrawl_extract_web_page`, `exa_get_contents`, `deepseek_fetch_web_page`¹ |
| [web_searcher_pro](https://github.com/zouyonghe/astrbot_plugin_web_searcher_pro) (SearXNG) | `searxng_web_search_*` (images, videos, music, maps and academic are specialists) | `searxng_web_fetch_url` |
| [kimi_web_search](https://github.com/Tsukumi233/astrbot_plugin_kimi_web_search) | `kimi_web_search` | `kimi_web_fetch` |
| [grok_web_search](https://github.com/piexian/astrbot_plugin_grok_web_search) / grok_suite | `grok_web_search` | `grok_web_fetch` |
| [mmx_cli_tool](https://github.com/piexian/astrbot_plugin_mmx_cli_tool) | `mmx_web_search` | |
| [anysearch](https://github.com/AgIzT/astrbot_plugin_anysearch) | `anysearch_search`, `anysearch_batch_search` | `anysearch_extract` |
| [gemini_search](https://github.com/Chris95743/astrbot_plugin_gemini_search) | `gemini_search` | `web_fetch` |
| [exa_web_search](https://github.com/piexian/astrbot_plugin_exa_web_search) | `exa-search` | `web_fetch_exa` |
| Other DeepSeek search plugins ([SkyL3gend](https://github.com/SkyL3gend/astrbot_plugin_deepseek_search), [Fantasality](https://github.com/Fantasality/astrbot_plugin_deepseek_search)) | `deepseek_web_search`¹, `web_search_deepseek`¹ | `deepseek_fetch_web_page`¹ |

¹ Also billed by DeepSeek: never suggested or restored once DeepDeep's spending cap is reached.

Tool not listed? Check the dashboard's "Tool routing preview". If it isn't recognized, add it to the general search patterns or the specialist rules, and feel free to open an issue. DeepDeep's tool names (`deepdeep_web_search`, `deepdeep_web_fetch`) and command (`/deepdeep`) don't clash with any of the plugins above.

## DeepDeep's own search tools

| Tool | Purpose |
|---|---|
| `deepdeep_web_search(query, count)` | Web search through the [DeepSeek Search API](https://api-docs.deepseek.com/guides/search). Each result has a long summary, publish date, relevance and authority. Supports `site:`, `-site:`, `"exact phrase"`, `lang:zh,en`, `after:2026-01-01`, `before:2026-06` |
| `deepdeep_web_fetch(url)` | Reads a full page as Markdown through the DeepSeek Fetch API |
| `deepdeep_tavily_search(query, count)` | Tavily search for technical questions and as DeepSeek's first fallback. Reuses the Tavily key from AstrBot's web search settings by default (even when the built-in provider is something else); not offered without a key or when the built-in provider already is Tavily |

The first line of each result is a status line with a 6-character request ID that also appears in the logs and dashboard, e.g. `[DeepSeek 搜索 #a1b2c3] 成功 · 6 条结果 · 456 ms`. DeepDeep asks DeepSeek for at least 10 results per call (billing is per request, not per result) and shows the model about `count` of them. If fewer than 3 results pass the relevance threshold, the next ones are added and marked as less relevant. For "latest" questions or queries with `after:`/`before:`, results are not filtered by relevance (fresh content tends to score low), the model is reminded that results are sorted by relevance, not date, the "newest result" date covers all returned results, and up to 2 newer but lower-ranked results are appended.

## Dashboard

Open the plugin in the AstrBot WebUI and choose "DeepDeep Dashboard" (the sticky top bar jumps to each section):

- **Status**: healthy or degraded and why, key source, connection test, circuit reset
- **Setup check**: whether DeepDeep's DeepSeek/Tavily keys, the key of AstrBot's selected built-in web search provider, and other search plugins' API keys and SearXNG URL are filled in, with where to fix each (only checks whether a value is set, never reads it)
- **Acting on warnings**: each warning can be ignored, and plugin/MCP tools can be stopped with one click (uses AstrBot's own tool switch, confirm twice); handled items move to a folded "Handled" section where they can be re-enabled or un-ignored
- **Main search tools**: first choice and fallbacks for general search and page reading, plus each specialist tool's job, with health badges; tools that misbehaved in the last 24 hours are flagged here
- **Spending**: today and this month against the caps, in the billing currency plus the converted other currency; DeepSeek calls over 30 days
- **Search tool usage**: calls, success rate, failures, blocked calls and average latency per search tool over 24 hours, 7 days or 30 days
- **Network**: whether each search backend is a cloud API or a self-hosted (intranet) service, where the provider is, and how well it covers Chinese and international sites; a one-click check (see below)
- **Settings**: the own-search switch, routing mode, currency and caps up front; exchange rate, circuit breaker, patterns, specialist rules and call log options under "Advanced". Changes save automatically
- **Tool routing preview**: each tool's role (DeepDeep, fallback, or specialist and its specialty) and whether it is visible when DeepSeek is healthy, down or over budget
- **Call log**: filter by time, source, tool, outcome, keyword or request ID; paging, details, JSON Lines export, clear
- **Diagnostics**: one click, attach to bug reports (contains no API key)

## Search tool health

Some search tools look fine but have effectively stopped working, e.g. a self-hosted SearXNG whose server IP is blocked by DuckDuckGo and Brave, leaving one engine that returns unrelated pages. From the call log DeepDeep rates each search tool over the last 24 hours: errors (including short error messages such as CAPTCHA or rate limits), empty results, and results that barely contain the query terms all count as problems. With at least 3 calls, 70% problems means "broken" and 35% (or 3 problems in a row) means "unstable". Relevance is computed in memory and only the score is stored.

This is **advisory only**: it shows on the dashboard and in `/deepdeep stats` and never hides tools or changes the guide (which would invalidate the prompt cache).

## Network

Cloud APIs (DeepSeek, SerpApi, Tavily, ...) search on the provider's servers, so your server only needs to reach the API. Self-hosted services such as `http://searxng:8080` send requests from your server, so engines your server cannot reach, or that block its IP, return nothing. "Check connectivity" opens a few Chinese and international sites and every API endpoint from the server and runs one real SearXNG query (its URL is read from the other plugin's config), listing working and blocked engines. No billed API is called.

## Call log and stats

DeepDeep records calls of **all tools** through AstrBot's tool hooks (`on_using_llm_tool` / `on_llm_tool_respond`) into `data/plugin_data/astrbot_plugin_deepdeep/calls.db` (SQLite). Records survive restarts and are kept 14 days by default (50,000 rows max). "Search tool usage" and `/deepdeep stats` read from here.

- DeepDeep's own calls: arguments, results and full details (request attempts, raw errors, relevance and dates, fallback). Arguments and results can be turned off
- Other tools: by default only name, source, session, duration and outcome. Arguments and results are stored only after enabling "Record full args and results of other tools"
- Calls that raise inside AstrBot are marked failed when the conversation ends; calls still running at restart are marked interrupted
- DeepDeep's calls are also written to AstrBot's official Trace

## Admin commands

| Command | Purpose |
|---|---|
| `/deepdeep stats` | Today's DeepSeek usage, spending, status, per-tool search calls in the last 24 hours search tools that look broken and incomplete setup |
| `/deepdeep test` | Test the DeepSeek key and network (billed once) |
| `/deepdeep reset` | Reset the circuit breaker |

## Pricing

DeepDeep's own tools use the DeepSeek Search & Fetch API: **¥5 per 1,000 successful requests** on the China site, **$0.75** on the international site, for search and fetch each, regardless of result count. Non-200 responses, empty results and timeouts are free. DeepSeek's note that server-side tool calls in Harness, Claude Code and Codex are free until 2026-11-01 **does not apply** here. Costs shown are local estimates; your DeepSeek bill is authoritative. Other search tools are billed by their own services; DeepDeep only counts their calls.

## Privacy

- Sent to DeepSeek: search queries written by the model, the first 128 characters of the user's message (optional), URLs to fetch
- No `localization` parameter, no user IPs
- Usage stats (`usage.json`) only hold daily counts
- The call log stays local, is visible only to AstrBot dashboard users, is kept 14 days by default and can be cleared at any time
- Other tools' arguments and results are not stored by default; when enabled they may include users' chat content

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) (Chinese).

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Credits

- [AstrBot](https://github.com/AstrBotDevs/AstrBot): plugin framework; views, tool hooks, config and i18n follow its official docs
- [DeepSeek Search & Fetch API](https://api-docs.deepseek.com/guides/search)
- [astrbot_plugin_qq_group_daily_analysis](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis): the CI, release-on-version-bump and issue template layout were modeled on it (workflows written independently)
- The search plugins listed under Compatibility: default rules are based on their public tool names; none of their code is used

## License

[MIT](LICENSE)
