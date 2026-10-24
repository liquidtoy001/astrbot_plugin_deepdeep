// DeepDeep 面板的英文词典。界面文字和后端返回的说明都以中文为准，英文模式下查这里：
// - t(中文模板, 变量)：面板自己的文字，模板里的 {name} 换成变量
// - tr(后端文字)：后端返回的说明。先整句查词典，再按 PATTERNS 匹配带变量的句子，再按句号、分号拆开逐句翻译；
//   都查不到时原样显示中文（比如工具自己返回的错误信息）
// - applyStatic(根节点)：翻译 index.html 里写死的文字和 placeholder / aria-label / title
// 用普通脚本加载（不是模块），挂在 window.DeepDeepI18n 上。
(function () {
  "use strict";

  const EN = {
    // ---------- 顶部、导航 ----------
    面板语言: "Dashboard language",
    "跟随 AstrBot": "Follow AstrBot",
    中文: "中文",
    刷新: "Refresh",
    解除熔断: "Reset breaker",
    测试连接: "Test connection",
    页面: "Pages",
    概览: "Overview",
    用量: "Usage",
    调用日志: "Call log",
    设置: "Settings",
    工具与诊断: "Tools & diagnostics",
    "加载中…": "Loading…",

    // ---------- 横幅、状态 ----------
    "未配置 API Key": "No API key configured",
    已达今日费用上限: "Daily spending cap reached",
    已达本月费用上限: "Monthly spending cap reached",
    "连续失败，已暂停": "Paused after repeated failures",
    只引导: "Guide only",
    "DeepSeek 优先": "DeepSeek first",
    "只用 DeepSeek": "DeepSeek only",
    "暂停 DeepSeek": "DeepSeek paused",
    不干预: "Don't interfere",
    "不隐藏任何工具，只在分工说明里写清先用谁、什么情况再换；工具列表不变，不影响提示词缓存":
      "Never hides tools; the guide says which tool to use first and when to switch. The tool list never changes, so prompt caching is unaffected.",
    "DeepSeek 可用时隐藏同类工具，选择更集中；每次切换会让提示词缓存失效一次":
      "Hides overlapping tools while DeepSeek works, for a more focused choice. Each switch invalidates the prompt cache once.",
    "始终隐藏其他通用搜索工具，DeepSeek 不可用时也不降级": "Always hides other general search tools, with no fallback even when DeepSeek is down.",
    "隐藏 DeepDeep 的工具，只用其他搜索（与关闭 DeepSeek 搜索开关效果相同）":
      "Hides DeepDeep's tools and uses other search only (same as turning off DeepSeek search).",
    "所有搜索工具并存，由模型自己选": "All search tools stay; the model picks.",
    "DeepSeek 搜索正常": "DeepSeek search is working",
    "路由模式：{mode}": "Routing mode: {mode}",
    "DeepSeek 搜索已关闭，由其他搜索工具负责": "DeepSeek search is off; other search tools take over",
    "分工说明、调用日志和健康检测照常工作。可在“设置”里重新开启。":
      "The guide, call log and health checks keep working. Turn it back on in Settings.",
    "已手动暂停 DeepSeek，正在使用其他搜索工具": "DeepSeek is paused manually; using other search tools",
    "DeepSeek 暂不可用": "DeepSeek is unavailable",
    "当前为“只用 DeepSeek”模式，不会降级。": "The mode is \"DeepSeek only\", so there is no fallback.",
    "模型调用 DeepDeep 时会收到改用其他搜索工具的建议。": "When the model calls DeepDeep it is told to use other search tools.",
    "已自动降级到其他搜索工具。": "Fell back to other search tools automatically.",
    "DeepSeek 搜索正常，费用接近上限": "DeepSeek search is working; spending is near the cap",
    "上限 {amount}": "Cap {amount}",
    不限制: "No cap",
    "{n} 次": "{n}",
    "缓存 {cache} · 无结果 {empty} · 失败 {failed}": "Cached {cache} · empty {empty} · failed {failed}",
    "未找到 DeepSeek API Key": "No DeepSeek API key found",
    "DeepSeek Key：{masked}（{source}）": "DeepSeek key: {masked} ({source})",
    "状态：没有找到 Tavily Key（在 AstrBot「配置 → 网页搜索」里填写，或在插件配置里单独填写）":
      "Status: no Tavily key found (fill it in under AstrBot \"Config → Web search\", or set one in the plugin config)",
    "状态：可用 · Key {masked}（{source}）": "Status: available · key {masked} ({source})",
    "状态：不提供 · {reason}": "Status: not offered · {reason}",
    " · 最近一次失败：{label}（HTTP {status}）": " · last failure: {label} (HTTP {status})",
    "约 {other}，{base}": "≈ {other}; {base}",
    "0 表示不限制": "0 = no cap",
    "按自然月统计，0 表示不限制": "Per calendar month; 0 = no cap",

    // ---------- 概览 ----------
    今日费用: "Spent today",
    本月费用: "Spent this month",
    今日搜索: "Searches today",
    今日抓取: "Fetches today",
    主力搜索工具: "Main search tools",
    "每类问题先用谁、不理想时换谁（和告诉模型的分工说明一致）· 健康状况按近 24 小时的调用判断，只做提示":
      "Which tool goes first for each kind of question and what to switch to (same as the guide given to the model) · health is judged from the last 24 hours and is advisory only",
    已处理: "Handled",
    对话模型内置的联网搜索: "Chat models' built-in web search",
    "有些对话模型自己会联网搜索（比如 Gemini 原生搜索）。这部分搜索不经过工具，DeepDeep 看不到，也不计入费用和调用日志":
      "Some chat models search the web by themselves (e.g. Gemini native search). That search bypasses tools, so DeepDeep can't see it and it isn't counted in spending or the call log.",
    "没有对话模型开启内置联网搜索，联网搜索都由搜索工具负责。": "No chat model has built-in web search on; search tools handle all web search.",
    未启用: "Disabled",
    可能冲突: "May conflict",
    默认在用: "Default",
    已启用: "Enabled",
    "AstrBot 的说明是开启后函数工具会失效，DeepDeep 和其他搜索工具可能都调用不了":
      "AstrBot notes that function tools stop working when this is on, so DeepDeep and other search tools may not be callable",
    会额外计费: "costs extra",
    "和搜索工具作用重复，可能搜两遍": "overlaps with search tools and may search twice",
    "想关掉的话：在 AstrBot「提供商」里编辑 {id}，关闭{what}": "To turn it off: edit {id} under AstrBot \"Providers\" and turn off {what}",
    "「自定义请求体」里的搜索参数": "the search parameter in \"Custom request body\"",
    "（换一个不带搜索的模型）": "it (switch to a model without built-in search)",
    "「启用原生搜索功能」": "\"Enable native search\"",
    没有读取到搜索类工具: "No search tools found",
    首选: "First",
    "等 {n} 个": "{n} in total",
    备用: "Backup",
    专用搜索: "Specialist search",
    "按需求选对口的工具，始终保留": "Pick the tool that fits; always kept",
    "本月已达调用上限、暂不提供：{list}": "Monthly call cap reached, not offered this month: {list}",
    "⚠ {n} 个搜索工具近 24 小时表现异常（只提示，不会改变路由）": "⚠ {n} search tool(s) misbehaved in the last 24 hours (advisory only; routing is unchanged)",
    "近 24 小时没有调用": "No calls in the last 24 hours",
    "（已忽略）": "(ignored) ",
    "（见上方“配置检查”）": " (see \"Setup check\" above)",
    缺少配置: "Missing",
    建议处理: "Needs attention",
    说明: "Note",
    已配置: "OK",
    "停用这 {n} 个工具": "Disable these {n} tools",
    停用这个工具: "Disable this tool",
    "已停用 {tools}，可以在下方“已处理”里重新启用": "Disabled {tools}; you can re-enable under \"Handled\" below",
    "再点一次确认停用（下一次对话的提示词缓存会失效一次）": "Click again to disable (the next chat's prompt cache is invalidated once)",
    忽略: "Ignore",
    "已忽略，问题变化时会重新提醒": "Ignored; you'll be reminded again if the problem changes",
    复制链接: "Copy link",
    "已复制链接，在浏览器里打开": "Link copied; open it in your browser",
    "怎么办：": "Fix: ",
    "{list} 等 {n} 个工具": "{list} and {n} tools in total",
    "{n} 项缺少配置": "{n} missing",
    "{n} 项建议处理": "{n} need attention",
    "配置检查：{parts}": "Setup check: {parts}",
    "其余 {n} 项": "{n} more",
    "✓ 配置检查：搜索工具都已配置好（{n} 项）": "✓ Setup check: all search tools are set up ({n} items)",
    "已停用 {n} 个工具": "{n} tool(s) disabled",
    "已忽略 {n} 条提醒": "{n} reminder(s) ignored",
    "已处理：{parts}": "Handled: {parts}",
    "由 DeepDeep 停用的工具（也可以在 AstrBot 的工具管理里启用）": "Tools disabled by DeepDeep (can also be re-enabled in AstrBot's tool management)",
    " · 已在别处重新启用": " · re-enabled elsewhere",
    重新启用: "Re-enable",
    从列表移除: "Remove from list",
    "已重新启用 {tool}": "Re-enabled {tool}",
    "已忽略的提醒（问题变化、或恢复后再出问题时会重新提醒）": "Ignored reminders (they come back if the problem changes, or recurs after recovering)",
    恢复提醒: "Restore reminder",
    已恢复提醒: "Reminder restored",

    // ---------- 用量 ----------
    "近 30 天 DeepSeek 计费次数": "DeepSeek billable calls, last 30 days",
    搜索: "Search",
    抓取: "Fetch",
    "30 天合计 {sum} 次，预估 {cost}": "{sum} calls in 30 days, est. {cost}",
    "近 30 天计费 {sum} 次": "{sum} billable calls in the last 30 days",
    还没有计费调用: "No billable calls yet",
    "{date}  搜索 {search} · 抓取 {fetch} · {cost}": "{date}  search {search} · fetch {fetch} · {cost}",
    本月调用上限: "Monthly call caps",
    "在「设置 → 高级设置」里设置；达到上限后本月不再提供，下个月自动恢复": "Set under Settings → Advanced. Once reached, the tool is not offered for the rest of the month and comes back next month.",
    本月已用: "Used this month",
    上限: "Cap",
    "已达上限，本月不再提供": "Cap reached, off for this month",
    各搜索工具用量: "Usage by search tool",
    统计范围: "Range",
    "近 24 小时": "Last 24 hours",
    "近 7 天": "Last 7 days",
    "近 30 天": "Last 30 days",
    全部: "All",
    工具: "Tool",
    来源: "Source",
    类型: "Type",
    健康: "Health",
    调用: "Calls",
    成功率: "Success",
    失败: "Failed",
    拦截: "Blocked",
    平均耗时: "Avg time",
    最近一次: "Last call",
    这段时间没有搜索类工具的调用: "No search tool calls in this period",
    "调用日志已关闭，无法统计": "The call log is off, so there are no stats",
    "合计 {n} 次 · 数据来自调用日志；成功率不含被拦截的调用。“健康”按近 24 小时判断：失败、空结果、结果与搜索词不相关都算有问题，鼠标悬停看原因":
      "{n} calls in total · from the call log; success rate excludes blocked calls. Health covers the last 24 hours: failures, empty results and results unrelated to the query count as problems. Hover to see why.",
    基本失效: "Broken",
    不稳定: "Unstable",
    正常: "OK",
    数据不足: "Not enough data",
    成功: "OK",
    无结果: "Empty",
    缓存: "Cached",
    已拦截: "Blocked",
    中断: "Interrupted",
    进行中: "Running",
    备用搜索: "Fallback search",
    其他: "Other",
    "DeepSeek 计费": "billed by DeepSeek",
    "AstrBot 内置": "AstrBot built-in",

    // ---------- 调用日志 ----------
    "所有工具的调用记录 · 点击一行查看详情": "Calls of all tools · click a row for details",
    时间范围: "Time range",
    "近 1 小时": "Last hour",
    "近 14 天": "Last 14 days",
    全部来源: "All sources",
    全部工具: "All tools",
    全部结果: "All results",
    只看问题: "Problems only",
    "关键词或请求 ID": "Keyword or request ID",
    关键词: "Keyword",
    筛选: "Filter",
    导出: "Export",
    清空: "Clear",
    再点一次确认清空: "Click again to clear",
    上一页: "Previous",
    下一页: "Next",
    时间: "Time",
    结果: "Result",
    耗时: "Time taken",
    没有符合条件的调用记录: "No matching calls",
    "加载失败：{error}": "Failed to load: {error}",
    "第 {page} / {pages} 页 · 共 {total} 条": "Page {page} / {pages} · {total} records",
    "所有工具的调用记录 · 已保存 {total} 条（最早 {oldest}），保留 {days} 天 · 点击一行查看详情":
      "Calls of all tools · {total} saved (oldest {oldest}), kept for {days} days · click a row for details",
    "已导出当前筛选条件下的调用日志（JSON Lines）": "Exported the calls matching the current filters (JSON Lines)",
    "已清空 {n} 条调用日志": "Cleared {n} call records",
    概要: "Summary",
    "请求 ID": "Request ID",
    "#{id}（与工具结果第一行、日志一致）": "#{id} (same as the first line of the tool result and the logs)",
    "HTTP 状态": "HTTP status",
    总耗时: "Total time",
    返回给模型的字数: "Characters returned to the model",
    请求参数: "Request",
    搜索词: "Query",
    链接: "URL",
    结果数: "Result count",
    附带正文: "Include content",
    用户原始问题: "User's original question",
    未调用的原因: "Why it wasn't called",
    代码: "Code",
    网络错误: "Network error",
    "{s}s 后重试": "retry in {s}s",
    "请求过程（{n} 次）": "Attempts ({n})",
    次: "#",
    状态: "Status",
    错误: "Error",
    重试: "Retry",
    "DeepSeek 返回": "DeepSeek response",
    触发熔断: "Tripped breaker",
    降级: "Fallback",
    路由模式: "Routing mode",
    放出的备用工具: "Fallback tools shown",
    无可用备用工具: "No fallback tools available",
    "撤下 DeepSeek 工具": "DeepSeek tools removed",
    是: "Yes",
    否: "No",
    "(无标题)": "(untitled)",
    "返回结果（相关度低于阈值的不会给模型；查询带 after:/before: 或问“最新”时不过滤）":
      "Results (results below the relevance threshold are not given to the model; no filtering for after:/before: queries or \"latest\" questions)",
    "标题 / 链接": "Title / URL",
    日期: "Date",
    相关度: "Relevance",
    权威度: "Authority",
    网页: "Page",
    标题: "Title",
    正文字数: "Content length",
    是否截断: "Truncated",
    "是（超过抓取字数上限）": "Yes (over the fetch length limit)",
    "原始记录（JSON）": "Raw record (JSON)",
    "记录 ID": "Record ID",
    会话: "Session",
    返回内容长度: "Result length",
    "{n} 字": "{n} chars",
    调用参数: "Arguments",
    返回结果: "Result",
    "未记录搜索词和结果：设置里关闭了「记录 DeepSeek 的搜索词、链接和返回结果」。":
      "Query and results not recorded: \"Record DeepSeek queries, URLs and results\" is off in Settings.",
    "未记录参数和结果：默认只记录其他工具的名称、耗时和状态。需要时在「设置 → 高级设置」中开启「记录其他工具的完整参数和结果」，之后的调用会保存完整内容。":
      "Arguments and results not recorded: by default only the name, time and status of other tools are kept. Turn on \"Record full arguments and results of other tools\" under Settings → Advanced to keep them for later calls.",

    // ---------- 设置 ----------
    修改后自动保存: "Changes save automatically",
    "启用 DeepSeek 搜索": "Enable DeepSeek search",
    "DeepDeep 的主力搜索，按次计费。关闭后由其他搜索工具负责": "DeepDeep's main search, billed per call. When off, other search tools take over.",
    "启用 Tavily 搜索": "Enable Tavily search",
    "专门负责编程、软件版本、官方文档等技术问题。自动使用 AstrBot「网页搜索」里填的 Tavily Key，免费额度每月 1000 次":
      "Handles technical questions such as programming, software versions and official docs. Uses the Tavily key from AstrBot \"Web search\" automatically; the free tier is 1000 calls a month.",
    计费币种: "Billing currency",
    "人民币（中国站，每千次 ¥5）": "CNY (China site, ¥5 per 1000)",
    "美元（国际站，每千次 $0.75）": "USD (international site, $0.75 per 1000)",
    "按你的 DeepSeek 账户选择": "Match your DeepSeek account",
    "每月费用上限（": "Monthly spending cap (",
    "每日费用上限（": "Daily spending cap (",
    "）": ")",
    高级设置: "Advanced settings",
    "路由模式、每日上限、调用上限、熔断、工具规则、调用日志": "routing mode, daily cap, call caps, breaker, tool rules, call log",
    "只引导（推荐）": "Guide only (recommended)",
    "汇率（1 美元 = ? 元）": "Exchange rate (1 USD = ? CNY)",
    "只用于换算显示另一种币种，0 表示不显示": "Only used to show the other currency; 0 = don't show",
    "Tavily 每月调用上限（次）": "Tavily monthly call cap",
    "按自然月统计成功的调用，缓存命中不算；达到上限后本月不再提供 Tavily，技术问题改由其他搜索负责，下个月自动恢复。0 表示不限制":
      "Counts successful calls per calendar month (cache hits don't count). Once reached, Tavily is not offered for the rest of the month and technical questions go to other search tools; it comes back next month. 0 = no cap",
    "熔断阈值（连续失败次数）": "Breaker threshold (consecutive failures)",
    "熔断冷却（秒）": "Breaker cooldown (seconds)",
    "其他搜索工具每月调用上限（每行：工具名规则 = 次数）": "Monthly call caps for other search tools (one per line: tool pattern = count)",
    "给博查、Brave、Exa 等付费搜索设上限，支持 * 通配；一行匹配多个工具时合计。达到上限后本月不再提供这些工具（不改 AstrBot 的工具开关），下个月自动恢复。用量在「用量」页查看":
      "Cap paid search such as Bocha, Brave or Exa; * wildcard supported, and tools matching one line are added up. Once reached, those tools are not offered for the rest of the month (AstrBot's tool switches are untouched) and come back next month. See the Usage page for counts.",
    "可互相替代的通用搜索（每行一个规则，支持 *）": "Interchangeable general search tools (one pattern per line, * wildcard)",
    "搜索分工规则（每行：工具名规则 = 擅长什么）": "Specialist rules (one per line: tool pattern = what it's good at)",
    "匹配的工具视为专用搜索：始终保留，并告诉模型它擅长什么。效果可以在「工具与诊断 → 工具路由预览」里看":
      "Matching tools are specialists: always kept, and the model is told what they're good at. See the effect under Tools & diagnostics → Tool routing preview.",
    "按问题类型分工（每行：问题类型 = 工具名规则, 工具名规则）": "Routing by question type (one per line: question type = tool pattern, tool pattern)",
    "本次请求里第一个存在的工具作为这类问题的首选，写进分工说明；其余问题仍先用 DeepDeep":
      "The first tool present in the request becomes the first choice for that type and goes into the guide; other questions still go to DeepDeep first.",
    "每次请求向模型说明搜索工具分工（只列出本次可用的工具）": "Tell the model how search tools divide the work on every request (only tools available in that request)",
    "记录所有工具的调用（保存在插件数据目录，重启不丢）": "Record calls of all tools (stored in the plugin data dir, kept across restarts)",
    "记录 DeepSeek 的搜索词、链接和返回结果": "Record DeepSeek queries, URLs and results",
    "记录其他工具的完整参数和结果（可能包含聊天内容，默认只记工具名、耗时和状态）":
      "Record full arguments and results of other tools (may include chat content; by default only name, time and status)",
    保留天数: "Keep for (days)",
    "有未保存的修改…": "Unsaved changes…",
    "有内容不符合要求，修改后会自动保存": "Some values are invalid; fix them and they'll save automatically",
    "保存中…": "Saving…",
    "已自动保存 · {time}": "Saved · {time}",
    "保存失败：{error}": "Save failed: {error}",

    // ---------- 工具与诊断 ----------
    工具路由预览: "Tool routing preview",
    "当前已注册的工具，以及它们在不同情况下是否对模型可见。超出预算时，同样走 DeepSeek 计费的工具不会作为备用":
      "Registered tools and whether the model sees them in each situation. Over budget, tools billed by DeepSeek are not used as fallbacks.",
    "DeepSeek 正常时": "DeepSeek OK",
    "DeepSeek 故障时": "DeepSeek down",
    超出预算时: "Over budget",
    可见: "Visible",
    隐藏: "Hidden",
    没有读取到工具: "No tools found",
    收起: "Collapse",
    "展开其余 {n} 个搜索工具": "Show {n} more search tools",
    "DeepDeep · 已关闭": "DeepDeep · off",
    "另有 {n} 个其他工具不受影响": "{n} other tools are not affected",
    网络连通性: "Network connectivity",
    检测连通性: "Check connectivity",
    "检测中…": "Checking…",
    连通性检测完成: "Connectivity check done",
    "云端 API 的搜索在服务商的服务器上完成，你的服务器只要连得上它的接口；自建服务（比如 SearXNG）从你的服务器直接访问各搜索引擎，服务器访问不了、或 IP 被拦截的引擎就没有结果。检测只打开几个网站首页和接口地址、查一次 SearXNG，不调用计费接口。":
      "Cloud APIs search on the provider's servers, so your server only needs to reach their API. Self-hosted services (such as SearXNG) reach each search engine from your server, so engines your server can't reach, or that block its IP, return nothing. The check only opens a few home pages and API addresses and queries SearXNG once; no billed API is called.",
    后端: "Backend",
    "运行方式 / 接口": "How it runs / API",
    能搜到: "Coverage",
    实测: "Measured",
    未检测: "Not checked",
    "可连接 · {ms} ms": "Reachable · {ms} ms",
    "连不上（{error}）": "Unreachable ({error})",
    "{n} 个引擎可用": "{n} engines working",
    "查询失败（{error}）": "Query failed ({error})",
    服务器能访问国外: "Server reaches sites abroad",
    服务器访问不了国外: "Server can't reach sites abroad",
    不联网: "Offline",
    "{list} 等 {n} 个": "{list} and {n} in total",
    " 服务商在{region}": " provider in {region}",
    直接访问目标网页: "Opens target pages directly",
    "国内 ": "China ",
    "国外 ": "Abroad ",
    可访问: " reachable",
    不可访问: " unreachable",
    "服务器网络（{time} 检测）": "Server network (checked {time})",
    国内网站: "Sites in China",
    国外网站: "Sites abroad",
    "SearXNG 引擎（{url}，测试词 DeepSeek：{results} 条结果，{relevant} 条包含测试词）":
      "SearXNG engines ({url}, test query DeepSeek: {results} results, {relevant} contain the query)",
    "能用：": "Working: ",
    没有: "none",
    "被拦截或无响应：": "Blocked or not responding: ",
    "只剩很少的引擎能用时，SearXNG 的通用搜索结果往往和问题无关。可以考虑在 SearXNG 里换用其他引擎、给服务器配置代理，或在 AstrBot 里关掉这些工具。":
      "With only a few engines working, SearXNG's general results are often unrelated to the question. Consider switching engines in SearXNG, giving the server a proxy, or turning these tools off in AstrBot.",
    云端: "Cloud",
    自建: "Self-hosted",
    服务器直连: "Direct from server",
    本机: "Local",
    诊断信息: "Diagnostics",
    "反馈问题时附上：包含版本、配置摘要、当前状态和最近的失败记录，不含 API Key":
      "Attach this when reporting a problem: versions, a settings summary, current status and recent failures. No API keys.",
    生成诊断信息: "Generate diagnostics",
    复制: "Copy",
    已复制诊断信息: "Diagnostics copied",
    "浏览器不允许自动复制，已选中，请按 Ctrl+C 复制": "The browser blocked copying; the text is selected, press Ctrl+C",
    "觉得好用的话，欢迎到 GitHub 点个 Star ⭐": "If you find it useful, a Star on GitHub is welcome ⭐",
    "已复制仓库链接，在浏览器打开即可点 Star": "Repository link copied; open it in your browser to Star",
    "插件面板运行在 AstrBot 的安全沙箱里，不能直接打开外部网页。可以在 AstrBot「插件」页打开本插件的详情，点击仓库地址前往 GitHub；问题反馈也在那里提交。":
      "The dashboard runs in AstrBot's sandbox and can't open external pages. Open this plugin's details on the AstrBot \"Plugins\" page and click the repository link to go to GitHub; report problems there too.",
    已解除熔断: "Breaker reset",
    "连接失败：{error}": "Connection failed: {error}",

    // ---------- 设置：搜索偏好、可用的搜索工具 ----------
    "一般只需要看这几项，默认值就能用。其余的都在最下面的「高级设置」里。":
      "These are usually all you need, and the defaults work. Everything else is under \"Advanced settings\" at the bottom.",
    搜索偏好: "Search preference",
    "排在前面的先用，不理想时往下换。选一个预设，或者自己调顺序。": "Tools higher up go first; the next one is used when results aren't good. Pick a preset or set your own order.",
    预设: "Preset",
    场景: "Scenario",
    "已保存 · {time}": "Saved · {time}",
    这一类现在没有可用的工具: "No tools available for this scenario right now",
    上移: "Move up",
    下移: "Move down",
    启用: "Use",
    不用: "Skip",
    "{scene}：（都不用）": "{scene}: (none)",
    "{scene}：{first}": "{scene}: {first}",
    "{scene}：优先 {first}；不理想时再用 {rest}": "{scene}: {first} first; if results aren't good, then {rest}",
    模型会收到的分工说明: "What the model is told",
    "改动后，下一次对话的提示词缓存会失效一次。": "After a change, the next chat's prompt cache is invalidated once.",
    可用的搜索工具: "Available search tools",
    "DeepDeep 认识的搜索工具、各自擅长什么和现在的状态": "Search tools DeepDeep knows, what each is good at, and their status",
    其余: "More",
    "其余 {n} 个：{parts}": "{n} more: {parts}",
    "推荐（按评测）": "Recommended (by evaluation)",
    省钱优先: "Save money",
    中文资料优先: "Chinese sources first",
    自定义: "Custom",
    一般问题: "General questions",
    技术问题: "Technical questions",
    读取网页: "Reading pages",
    "当前路由模式下，一般问题和读取网页固定先用 DeepSeek；想完全按偏好排，把路由模式改成“只引导”":
      "In the current routing mode, DeepSeek always goes first for general questions and reading pages. To follow your order fully, set the routing mode to \"Guide only\".",
    "DeepDeep 自带": "Built into DeepDeep",
    插件: "Plugin",
    知识库: "Knowledge base",
    当前在用: "In use",
    可用: "Available",
    "缺 Key": "Key missing",
    暂停: "Paused",
    "已填 Key，未选用": "Key set, not selected",
    已关闭: "Off",
    已停用: "Disabled",
    "没填 Key": "No key",
    未安装: "Not installed",
    "中文资料覆盖最好，结果带发布日期、相关度和权威度，适合新闻和最新动态":
      "Best Chinese coverage; results carry publish date, relevance and authority. Good for news and latest updates",
    "按次计费，每千次 ¥5（国际站 $0.75）": "Billed per call: ¥5 per 1000 ($0.75 on the international site)",
    "英文和技术资料最好：编程、软件版本、安全漏洞、官方文档": "Best for English and technical material: programming, versions, vulnerabilities, official docs",
    "免费每月 1000 次": "Free: 1000 calls a month",
    "返回 Google 的结果，国内外网站都覆盖，另有图片、学术、财经搜索": "Google results covering sites in and outside China, plus image, scholar and finance search",
    "免费每月 250 次": "Free: 250 calls a month",
    "中文网站为主，接口在国内，国内服务器连接稳定": "Mostly Chinese sites; API in China, stable from servers in China",
    按次计费: "Billed per call",
    "百度的搜索结果，中文网站覆盖广": "Baidu results with broad Chinese coverage",
    "按次计费（有免费额度）": "Billed per call (free tier available)",
    "Tavily（AstrBot 内置）": "Tavily (AstrBot built-in)",
    "和 DeepDeep 的 Tavily 是同一个服务，另带网页读取": "Same service as DeepDeep's Tavily, plus page reading",
    "免费每月 1000 次（和 DeepDeep 的 Tavily 共用 Key）": "Free: 1000 calls a month (shares the key with DeepDeep's Tavily)",
    "独立的搜索索引，英文网站为主，注重隐私": "Independent index, mostly English sites, privacy-focused",
    有免费额度: "Free tier available",
    "语义搜索：按意思找相似的文章、论文和网站，另带网页读取": "Semantic search: finds similar articles, papers and sites by meaning, plus page reading",
    "搜索的同时抓取网页全文，适合需要读整页内容的场景": "Fetches full pages while searching; good when whole pages are needed",
    聚合多家搜索引擎的结果: "Aggregates results from several search engines",
    "DeepSeek（AstrBot 内置）": "DeepSeek (AstrBot built-in)",
    "AstrBot 4.29.1 起自带的基础版 DeepSeek 搜索，和 DeepDeep 用同一个账户计费": "The basic DeepSeek search built into AstrBot 4.29.1+, billed to the same account as DeepDeep",
    "按次计费，和 DeepDeep 共用 DeepSeek 账户": "Billed per call, shares the DeepSeek account with DeepDeep",
    "SearXNG（web_searcher_pro 插件）": "SearXNG (web_searcher_pro plugin)",
    "自建、不花钱，聚合多个搜索引擎，另有图片、视频、新闻、学术等分类搜索": "Self-hosted and free; aggregates several engines, with image, video, news and scholar categories",
    "免费（需要自建 SearXNG）": "Free (needs a self-hosted SearXNG)",
    "在 AstrBot 插件市场搜索 web_searcher_pro 安装，并自建 SearXNG": "Install web_searcher_pro from the AstrBot plugin market and self-host SearXNG",
    "Kimi 联网搜索": "Kimi web search",
    "月之暗面的联网搜索，中文资料好": "Moonshot AI's web search, good for Chinese sources",
    "在 AstrBot 插件市场搜索 kimi_web_search 安装": "Install kimi_web_search from the AstrBot plugin market",
    "Grok 搜索": "Grok search",
    "包含 X（推特）上的实时内容，适合追热点": "Includes real-time content from X (Twitter); good for trending topics",
    "在 AstrBot 插件市场搜索 grok_web_search 安装": "Install grok_web_search from the AstrBot plugin market",
    "Gemini 搜索": "Gemini search",
    "借 Gemini 用 Google 搜索并总结": "Searches Google through Gemini and summarizes",
    "在 AstrBot 插件市场搜索 gemini_search 安装": "Install gemini_search from the AstrBot plugin market",
    "Exa（插件）": "Exa (plugin)",
    "Exa 语义搜索的插件版": "Plugin version of Exa semantic search",
    "在 AstrBot 插件市场搜索 exa_web_search 安装": "Install exa_web_search from the AstrBot plugin market",
    "AnySearch（插件）": "AnySearch (plugin)",
    "AnySearch 聚合搜索的插件版，支持批量搜索": "Plugin version of AnySearch, with batch search",
    "在 AstrBot 插件市场搜索 anysearch 安装": "Install anysearch from the AstrBot plugin market",
    "GitHub 搜索（MCP）": "GitHub search (MCP)",
    "直接搜 GitHub 上的仓库、代码、Issue 和 PR": "Searches GitHub repositories, code, issues and PRs directly",
    免费: "Free",
    "在 AstrBot「MCP」里添加 GitHub 官方 MCP 服务": "Add the official GitHub MCP server under AstrBot \"MCP\"",
    "arXiv 论文（MCP）": "arXiv papers (MCP)",
    "搜索和下载 arXiv 论文": "Searches and downloads arXiv papers",
    "在 AstrBot「MCP」里添加 arXiv MCP 服务": "Add an arXiv MCP server under AstrBot \"MCP\"",
    "只查 AstrBot 里上传的资料，不联网": "Only searches material uploaded to AstrBot, offline",
    "在 AstrBot「知识库」里创建并给人格启用": "Create one under AstrBot \"Knowledge base\" and enable it for the persona",
    "当前选中了它，但没有填写 Key，内置搜索一调用就会报错：在 AstrBot「配置 → 网页搜索」填写":
      "It's selected but has no key, so built-in search fails when called: fill it in under AstrBot \"Config → Web search\"",
    "DeepDeep 的 Tavily 正在用这个 Key": "DeepDeep's Tavily is using this key",
    "内置网页搜索同一时间只能选一家，想用它的话在 AstrBot「配置 → 网页搜索」切换":
      "Built-in web search uses one provider at a time; switch under AstrBot \"Config → Web search\" to use it",
    "在 AstrBot「配置 → 网页搜索」选它并填写 Key": "Select it under AstrBot \"Config → Web search\" and fill in the key",
    "在 AstrBot「配置 → 网页搜索」填写 Tavily 的 Key（不用选它为当前提供商）":
      "Fill in the Tavily key under AstrBot \"Config → Web search\" (no need to select it as the provider)",
    "没有找到 DeepSeek API Key：在插件配置里填写，或添加 DeepSeek 官方提供商": "No DeepSeek API key: fill it in the plugin config, or add an official DeepSeek provider",
    "工具已在 AstrBot 里停用": "The tools are disabled in AstrBot",
    "配置不完整，见概览页的配置检查": "Setup incomplete; see the setup check on the Overview page",
    "DeepSeek 读网页": "DeepSeek page reading",

    // ---------- 后端：配置检查 ----------
    "AstrBot 内置网页搜索": "AstrBot built-in web search",
    "DeepDeep · DeepSeek 搜索和抓取": "DeepDeep · DeepSeek search and fetch",
    "DeepDeep · Tavily 搜索": "DeepDeep · Tavily search",
    "没有找到 DeepSeek API Key，DeepDeep 的搜索不可用（会自动引导模型改用其他搜索）":
      "No DeepSeek API key found, so DeepDeep's search is unavailable (the model is steered to other search tools automatically)",
    "在插件配置里填写 DeepSeek API Key，或在 AstrBot 里添加 api_base 为 https://api.deepseek.com 的 DeepSeek 提供商（会自动复用它的 Key）":
      "Fill in the DeepSeek API key in the plugin config, or add a DeepSeek provider with api_base https://api.deepseek.com in AstrBot (its key is reused automatically)",
    "没有找到插件配置文件，无法检查": "Plugin config file not found, can't check",
    "没有填写 API Key，这些工具一调用就会报错": "No API key filled in; these tools fail as soon as they're called",
    "未开启（可选）": "Off (optional)",
    "需要时在 AstrBot「配置 → 网页搜索」开启，并选一家（Tavily、SerpApi、博查等）":
      "Turn it on under AstrBot \"Config → Web search\" when needed and pick a provider (Tavily, SerpApi, Bocha, …)",
    已在设置中关闭: "Turned off in settings",
    "没有找到 Tavily Key：技术类问题会改由 DeepSeek 等其他工具负责": "No Tavily key found: technical questions go to DeepSeek and other tools instead",
    "在 AstrBot「配置 → 网页搜索」选 Tavily 并填写 Key（填完可以切回原来的提供商，Key 会保留），或在 AstrBot 插件配置页给 DeepDeep 单独填写「Tavily API Key」。Tavily 免费额度每月 1000 次":
      "Pick Tavily under AstrBot \"Config → Web search\" and fill in the key (you can switch back to your provider afterwards; the key is kept), or set \"Tavily API key\" for DeepDeep in its plugin config. The Tavily free tier is 1000 calls a month.",
    未知: "unknown",
    本次不提供: "not offered this time",
    "还有其他 Key 可用，暂时不影响搜索": "Other keys still work, so search is not affected for now",
    "已暂停提供 Tavily，技术问题改由其他搜索负责": "Tavily is paused; technical questions go to other search tools",
    博查: "Bocha",
    "百度 AI 搜索": "Baidu AI Search",
    "Key 无效": "invalid key",
    额度用完: "quota used up",

    // ---------- 后端：Tavily、插件 ----------
    插件配置: "plugin config",
    "复用 AstrBot 网页搜索的 Tavily Key": "reused from AstrBot web search",
    已在插件设置中关闭: "turned off in plugin settings",
    "没有找到 Tavily API Key": "no Tavily API key found",
    "AstrBot 内置网页搜索已经是 Tavily，避免重复": "AstrBot's built-in web search already is Tavily, avoiding duplicates",
    "Tavily Key 无效，换成有效的 Key 后自动恢复": "Tavily key invalid; comes back automatically once a valid key is set",
    "连接正常（本次测试计费 1 次）": "Connection OK (this test was billed once)",
    "未找到 DeepSeek API Key：请在插件配置中填写，或添加 api_base 为 https://api.deepseek.com 的提供商。":
      "No DeepSeek API key: fill it in the plugin config, or add a provider with api_base https://api.deepseek.com.",
    "Tavily API Key 无效": "Tavily API key invalid",
    "Tavily 请求过于频繁或本月额度已用完": "Too many Tavily requests, or this month's quota is used up",
    "Tavily 本月额度已用完": "Tavily quota for this month is used up",
    "Tavily 账户额度不足": "Not enough Tavily credit",
    "Tavily 请求超时": "Tavily request timed out",
    "没有可用的 Tavily API Key": "No usable Tavily API key",

    // ---------- 后端：健康、费用 ----------
    空结果: "empty",
    结果不相关: "irrelevant",

    // ---------- 后端：模型内置搜索 ----------
    "OpenAI 搜索模型（按次额外计费）": "OpenAI search model (extra per-call cost)",
    "Perplexity Sonar（每次回答都会联网搜索）": "Perplexity Sonar (searches the web on every answer)",
    "OpenRouter 联网版（按次额外计费）": "OpenRouter online variant (extra per-call cost)",
    "enable_search（通义千问等）": "enable_search (Qwen and others)",
    "search_parameters（xAI Live Search）": "search_parameters (xAI Live Search)",
    "web_search_options（OpenAI 搜索模型）": "web_search_options (OpenAI search models)",
    "plugins: web（OpenRouter）": "plugins: web (OpenRouter)",
    "tools: $web_search（Kimi）": "tools: $web_search (Kimi)",
    "Gemini 原生搜索（Google 搜索）": "Gemini native search (Google Search)",
    "xAI Live Search（按信息源计费）": "xAI Live Search (billed per source)",
    "Gemini 读取链接（URL Context）": "Gemini URL Context",

    // ---------- 后端：分工、网络 ----------
    通用网页搜索: "General web search",
    "通用网页搜索（新闻、事实、中文资料等）": "General web search (news, facts, Chinese sources, …)",
    "新闻、事实、中文资料等": "News, facts, Chinese sources, …",
    "新闻时事、中文资料、海外新闻、比赛和最新动态": "News, Chinese sources, world news, sports and latest updates",
    读取网页全文: "Read full pages",
    "用户发来链接、摘要不够详细时": "When the user sends a link or snippets aren't detailed enough",
    按问题类型分工: "By question type",
    "编程、软件版本、安全漏洞、官方文档等技术问题": "Technical questions: programming, versions, vulnerabilities, official docs",
    找图片: "Images",
    "找视频（B 站、YouTube 等视频平台）": "Videos (Bilibili, YouTube, …)",
    找音乐: "Music",
    地点和地图: "Places and maps",
    "股票、汇率等财经行情": "Stocks, exchange rates and other market data",
    学术论文和期刊: "Academic papers and journals",
    "学术论文（arXiv 等）": "Academic papers (arXiv, …)",
    "GitHub 上的仓库、代码、Issue 和 PR": "GitHub repositories, code, issues and PRs",
    "Arch Linux AUR 软件包": "Arch Linux AUR packages",
    编程文档和代码示例: "Programming docs and code examples",
    网页截图: "Web page screenshots",
    本地知识库里的资料: "Local knowledge base",
    "云端 API": "Cloud API",
    "自建服务（内网）": "Self-hosted (intranet)",
    国内: "China",
    国外: "abroad",
    "DeepSeek 网页抓取": "DeepSeek page fetch",
    能读: "Readable",
    "由 DeepSeek 的服务器去抓网页，服务器自己打不开的网站也能读": "DeepSeek's servers fetch the page, so sites your server can't open are readable too",
    "DeepSeek 搜索": "DeepSeek search",
    好: "Good",
    "搜索在 DeepSeek 的服务器上完成，中文内容覆盖最好；服务器只需能连上 api.deepseek.com":
      "Search runs on DeepSeek's servers with the best Chinese coverage; your server only needs to reach api.deepseek.com",
    "只搜 GitHub；国内服务器访问 GitHub 可能不稳定": "GitHub only; access from servers in China may be unstable",
    "SearXNG 插件 · 网页抓取": "SearXNG plugin · page fetch",
    取决于服务器: "Depends on server",
    "从你的服务器直接打开网页，服务器访问不了的网站读不到": "Opens pages directly from your server, so sites it can't reach can't be read",
    "SearXNG（自建）": "SearXNG (self-hosted)",
    取决于引擎: "Depends on engines",
    "SearXNG 从你的服务器向各搜索引擎发请求：服务器 IP 被拦截（验证码、限流）的引擎会没有结果，只剩少数引擎时结果可能完全不相关。点“检测”可以看到哪些引擎还能用":
      "SearXNG queries each engine from your server: engines that block its IP (captcha, rate limits) return nothing, and with only a few left results may be unrelated. Run the check to see which engines still work.",
    "SerpApi（Google 结果）": "SerpApi (Google results)",
    较好: "Fairly good",
    "返回 Google 的搜索结果，国内外网站都覆盖；接口在国外": "Returns Google results covering sites in and outside China; API hosted abroad",
    一般: "Fair",
    "以英文网站为主，中文网站覆盖一般；接口在国外。2026-10 评测里技术类问题（编程、版本、漏洞、文档）表现最好，但不带发布日期":
      "Mostly English sites with fair Chinese coverage; API hosted abroad. Best on technical questions (programming, versions, vulnerabilities, docs) in the 2026-10 evaluation, but results have no publish dates.",
    "以英文网站为主；接口在国外": "Mostly English sites; API hosted abroad",
    "语义搜索，以英文网站为主；接口在国外": "Semantic search, mostly English sites; API hosted abroad",
    "由 Firecrawl 抓取网页；接口在国外": "Firecrawl fetches the page; API hosted abroad",
    博查搜索: "Bocha search",
    "以中文网站为主；接口在国内": "Mostly Chinese sites; API hosted in China",
    "Kimi（月之暗面）": "Kimi (Moonshot AI)",
    "包含 X（推特）上的实时内容；接口在国外，插件里可以配置代理或中转地址": "Includes real-time content from X (Twitter); API hosted abroad, a proxy or relay can be set in the plugin",
    "接口在国外，国内服务器通常无法直连": "API hosted abroad; servers in China usually can't reach it directly",
    本地知识库: "Local knowledge base",
    "只查 AstrBot 里的知识库，不联网": "Only searches AstrBot's knowledge base, offline",
    百度: "Baidu",
    哔哩哔哩: "Bilibili",
    超时: "timeout",
    "（检查 SearXNG 是否开启了 JSON 格式）": "(check that SearXNG has the JSON format enabled)",

    // ---------- 后端：操作结果、校验 ----------
    "请求体必须是 JSON 对象": "The request body must be a JSON object",
    记录不存在或已被清理: "The record doesn't exist or was cleaned up",
    "这条提醒已经不存在了，刷新看看": "This reminder no longer exists; try refreshing",
    找不到这个工具: "Tool not found",
    "不能从这里停用（不是已注册的插件工具，或已经停用）": "Can't be disabled here (not a registered plugin tool, or already disabled)",
    "这个工具已经不存在（插件可能被卸载了），已从列表移除": "This tool no longer exists (the plugin may have been uninstalled); removed from the list",
    "无效的记录 ID": "Invalid record ID",
    "无返回值，或已直接发送给用户": "No return value, or sent to the user directly",
    "工具执行时抛出异常或被中断，AstrBot 没有返回结果，详见 AstrBot 日志":
      "The tool raised an exception or was interrupted and AstrBot returned no result; see the AstrBot log",
    "未配置 DeepSeek API Key": "No DeepSeek API key configured",
    "搜索请求参数无效": "Invalid search request",
    "DeepSeek API Key 无效或缺失，请联系管理员检查插件配置": "DeepSeek API key invalid or missing; check the plugin config",
    "DeepSeek 账户余额不足，暂时无法联网搜索，请告知用户并直接根据已有知识回答": "Insufficient DeepSeek balance; web search is unavailable for now",
    "该网页被 DeepSeek 安全策略拒绝抓取，请换一个链接或改用搜索": "DeepSeek's safety policy refused to fetch this page",
    "无法获取该网页的可用内容（页面不存在、被站点屏蔽或没有可提取的正文），请换一个链接": "No usable content could be fetched (page missing, blocked by the site, or no extractable text)",
    "请求超时，请稍后再试": "Request timed out, try again later",
    请求内容过大: "Request too large",
    "联网搜索请求过于频繁，请稍后再试": "Too many search requests, try again later",
    "DeepSeek 搜索服务内部错误，请稍后再试": "DeepSeek search internal error, try again later",
    "DeepSeek 搜索服务上游超时，请稍后再试": "DeepSeek search upstream timeout, try again later",
    "DeepSeek 搜索服务暂时不可用，请稍后再试": "DeepSeek search is temporarily unavailable, try again later",
    "连接 DeepSeek 超时，请稍后再试": "Connecting to DeepSeek timed out, try again later",
    "无法连接 DeepSeek 搜索服务，请稍后再试": "Can't reach DeepSeek search, try again later",
    "未配置 DeepSeek API Key，请联系管理员在插件配置中填写，或添加 DeepSeek 官方提供商": "No DeepSeek API key configured; fill it in the plugin config or add an official DeepSeek provider",
  };

  // 带变量的后端句子。fn(匹配结果, tr) 返回英文
  const PATTERNS = [
    [/^Key 来源：(.+)$/, (m, tr) => `Key source: ${tr(m[1])}`],
    [/^插件 (.+)$/, (m) => `Plugin ${m[1]}`],
    [/^当前提供商是 (.+?)，但没有填写 .+ 的 Key，内置搜索工具一调用就会报错$/, (m, tr) => `The current provider is ${tr(m[1])}, but its key is empty, so the built-in search tools fail as soon as they're called`],
    [/^在 AstrBot「配置 → 网页搜索」填写 (.+) 的 Key$/, (m, tr) => `Fill in the ${tr(m[1])} key under AstrBot "Config → Web search"`],
    [/^当前提供商：(.+)，Key 已填写$/, (m, tr) => `Current provider: ${tr(m[1])}, key filled in`],
    [/^当前提供商：(.+)$/, (m, tr) => `Current provider: ${tr(m[1])}`],
    [/^没有填写服务地址：(.+)$/, (m) => `Service URL not filled in: ${m[1].replace(/、/g, ", ")}`],
    [/^在 AstrBot「插件」页打开 (.+) 的配置填写 Key；暂时不用的话可以在 AstrBot 里关掉这些工具$/, (m) => `Open the settings of ${m[1]} on the AstrBot "Plugins" page and fill in the key; if you don't need it now, turn these tools off in AstrBot`],
    [/^在 AstrBot「插件」页打开 (.+) 的配置填写$/, (m) => `Open the settings of ${m[1]} on the AstrBot "Plugins" page and fill it in`],
    [/^可选的凭据没有填写：(.+)（只影响相关的可选功能）$/, (m) => `Optional credentials not filled in: ${m[1].replace(/、/g, ", ")} (only affects related optional features)`],
    [/^最近一次调用失败：(?:第 (\d+) 个 Key（(.+?)）)?(.+?)（HTTP (\d+)）$/, (m, tr) => `Last call failed: ${m[1] ? `key #${m[1]} (${m[2]}) ` : ""}${tr(m[3])} (HTTP ${m[4]})`],
    [/^到 (\S+) 登录，复制有效的 Key，/, (m) => `Sign in at ${m[1]}, copy a valid key and paste it into the Tavily key under AstrBot "Config → Web search", or "Tavily API key" in the plugin config`],
    [/^等下个月额度重置，或到 (\S+) 升级套餐、换一个 Key。/, (m) => `Wait for the quota to reset, or upgrade your plan or use another key at ${m[1]}. You can set a "Tavily monthly call cap" below your quota under Settings → Advanced so it stops before running out.`],
    [/^对话模型 (.+) · 内置联网搜索$/, (m) => `Chat model ${m[1]} · built-in web search`],
    [/^开启了(.+?)。AstrBot 的说明是开启后函数工具会失效，DeepDeep 和其他搜索工具可能都调用不了$/, (m, tr) => `${tr(m[1])} is on. AstrBot notes that function tools stop working when it's on, so DeepDeep and other search tools may not be callable`],
    [/^开启了(.+?)：模型能自己读取网页，和网页读取工具作用重复$/, (m, tr) => `${tr(m[1])} is on: the model can read pages by itself, overlapping with page-reading tools`],
    [/^开启了(.+?)：模型会自己联网搜索，和搜索工具作用重复(，而且会额外计费)?。这部分搜索 DeepDeep 看不到，也不计入费用和调用日志$/, (m, tr) => `${tr(m[1])} is on: the model searches the web by itself, overlapping with search tools${m[2] ? ", at extra cost" : ""}. DeepDeep can't see this search and it isn't counted in spending or the call log`],
    [/^在 AstrBot「提供商」里编辑 (.+?)，关闭「启用原生搜索功能」，联网搜索交给 DeepDeep；/, (m) => `Edit ${m[1]} under AstrBot "Providers" and turn off "Enable native search" so DeepDeep handles web search. To keep Gemini's own search instead, turn off DeepDeep and other search tools so they don't take up context`],
    [/^一般不用处理；想统一由 DeepDeep 读取网页的话，在 AstrBot「提供商」里编辑 (.+?)，关闭「启用 URL 上下文」$/, (m) => `Usually nothing to do. To have DeepDeep read all pages, edit ${m[1]} under AstrBot "Providers" and turn off "Enable URL context"`],
    [/^想统一由 DeepDeep 管理搜索的话，在 AstrBot「提供商」里编辑 (.+?)，关掉内置搜索；两边都开也能用，只是可能搜两遍$/, (m) => `To have DeepDeep manage all search, edit ${m[1]} under AstrBot "Providers" and turn off the built-in search. Keeping both works too; it may just search twice`],
    [/^自定义请求体：(.+)$/, (m, tr) => `Custom request body: ${tr(m[1])}`],
    [/^tools: (.+)$/, (m) => `tools: ${m[1]}`],
    [/^(.+) 读网页$/, (m, tr) => `${tr(m[1])} page reading`],
    [/^(\d+) 个工具 · (.+)$/, (m, tr) => `${m[1]} tools · ${tr(m[2])}`],
    [
      /^(\d+) 次调用(?:：(.+?)|，均正常)(?:；平均相关度 ([\d.]+))?(?:；最近连续 (\d+) 次有问题)?$/,
      (m, tr) =>
        `${m[1]} call${m[1] === "1" ? "" : "s"}${m[2] ? `: ${m[2].split("、").map((p) => p.replace(/^(\S+) (\d+)$/, (_, k, n) => `${tr(k).toLowerCase()} ${n}`)).join(", ")}` : ", all fine"}` +
        (m[3] ? `; avg relevance ${m[3]}` : "") +
        (m[4] ? `; the last ${m[4]} calls all had problems` : ""),
    ],
    [/^已达到今日费用上限 (.+)$/, (m) => `Daily spending cap of ${m[1]} reached`],
    [/^已达到本月费用上限 (.+)$/, (m) => `Monthly spending cap of ${m[1]} reached`],
    [/^DeepSeek 连续调用失败（(.+)），约 (\d+) 分钟后自动重试$/, (m, tr) => `DeepSeek failed repeatedly (${tr(m[1])}); retrying automatically in about ${m[2]} min`],
    [/^本月 Tavily 已用 (\d+) \/ (\d+) 次$/, (m) => `Tavily this month: ${m[1]} / ${m[2]} calls`],
    [/^本月 Tavily 已用 (\d+) 次$/, (m) => `Tavily this month: ${m[1]} calls`],
    [/^本月 Tavily 调用已达上限（(.+)）$/, (m, tr) => `Monthly Tavily cap reached (${tr(m[1])})`],
    [/^Tavily 额度用完，约 (\d+) 小时后自动重试$/, (m) => `Tavily quota used up; retrying automatically in about ${m[1]} h`],
    [/^Tavily 返回 HTTP (\d+)$/, (m) => `Tavily returned HTTP ${m[1]}`],
    [/^无法连接 Tavily（(.+)）$/, (m) => `Can't reach Tavily (${m[1]})`],
    [/^复用提供商 (.+)$/, (m) => `reused from provider ${m[1]}`],
    [/^DeepSeek 搜索请求失败（HTTP (\d+)）$/, (m) => `DeepSeek search request failed (HTTP ${m[1]})`],
    [/^链接过长（超过 (\d+) 字符）$/, (m) => `URL too long (over ${m[1]} chars)`],
    [/^(\w+) 必须是布尔值$/, (m) => `${m[1]} must be a boolean`],
    [/^(\w+) 只能是 (.+)$/, (m) => `${m[1]} must be one of ${m[2]}`],
    [/^(\w+) 必须是字符串列表$/, (m) => `${m[1]} must be a list of strings`],
    [/^(\w+) 条目过多或过长$/, (m) => `${m[1]} has too many or too long entries`],
    [/^(\w+) 必须是数字$/, (m) => `${m[1]} must be a number`],
    [/^(\w+) 需在 (\S+) 到 (\S+) 之间$/, (m) => `${m[1]} must be between ${m[2]} and ${m[3]}`],
  ];

  const ZH = /[一-鿿]/;
  let lang = "zh";

  function fill(s, vars) {
    return vars ? s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : s;
  }

  function t(zh, vars) {
    const s = lang === "en" && EN[zh] !== undefined ? EN[zh] : zh;
    return fill(s, vars);
  }

  function translate(s) {
    const text = s.trim();
    if (!text || !ZH.test(text)) return s;
    if (EN[text] !== undefined) return EN[text];
    for (const [re, fn] of PATTERNS) {
      const m = text.match(re);
      if (m) return fn(m, translate);
    }
    // 按句号、分号拆开逐句翻译
    for (const [sep, join] of [["。", ". "], ["；", "; "]]) {
      const parts = text.split(sep).filter((p) => p.trim());
      if (parts.length > 1 || (parts.length === 1 && text.endsWith(sep))) {
        const out = parts.map((p) => translate(p)).join(join);
        return text.endsWith(sep) ? `${out}.` : out;
      }
    }
    return s;
  }

  function tr(text) {
    if (text === null || text === undefined) return "";
    return lang === "en" ? translate(String(text)) : String(text);
  }

  // index.html 里写死的文字：记住中文原文，切换语言时按原文重新翻译
  const origText = new WeakMap();
  const origAttr = new WeakMap();
  const ATTRS = ["placeholder", "aria-label", "title"];

  function applyStatic(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.parentElement?.closest("script, style, code, pre, textarea")) continue;
      if (!origText.has(node)) {
        if (!ZH.test(node.nodeValue)) continue;
        origText.set(node, node.nodeValue);
      }
      const src = origText.get(node);
      const key = src.trim();
      node.nodeValue = lang === "en" && EN[key] !== undefined ? src.replace(key, EN[key]) : src;
    }
    for (const el of root.querySelectorAll(ATTRS.map((a) => `[${a}]`).join(","))) {
      let saved = origAttr.get(el);
      if (!saved) {
        saved = {};
        for (const a of ATTRS) {
          const v = el.getAttribute(a);
          if (v && ZH.test(v)) saved[a] = v;
        }
        origAttr.set(el, saved);
      }
      for (const [a, v] of Object.entries(saved)) el.setAttribute(a, lang === "en" && EN[v] !== undefined ? EN[v] : v);
    }
  }

  window.DeepDeepI18n = {
    EN,
    t,
    tr,
    applyStatic,
    setLang(next) {
      lang = next === "en" ? "en" : "zh";
    },
    getLang() {
      return lang;
    },
  };
})();
