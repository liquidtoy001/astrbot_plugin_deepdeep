"""DeepDeep 认识的所有搜索来源：一张表，一家一条记录。

工具名、AstrBot 内置网页搜索的提供商和 Key 配置项、接口地址、能覆盖哪些网站、价格、怎么安装，
都只写在这里，其他模块从这里读。用户的搜索来源各不相同（博查、SearXNG、Brave、Exa……），
DeepSeek 和 Tavily 只是其中两家，只是 DeepDeep 恰好有它们自己的客户端。

加一家新的搜索来源：在 PROVIDERS 里加一条记录就够了。
- networks：工具走哪条网络。按表里的顺序匹配，第一个命中的生效，所以通配范围大的放后面
- listings：设置页「可用的搜索工具」里的一行；是 AstrBot 内置的，带上 builtin
- known_tools：工具名不符合 router.py 的通用规则（*web_search* 等）时，用它让路由认出来。
  这是写在代码里的识别，升级后老用户也生效；不要用 *_search 这种宽泛的规则，
  否则 memory_search、msg_search 也会被当成网页搜索。没有现成插件的服务用 _service_tools
- api：只要发一个 HTTP 请求就能搜的服务，写上 SearchApi 就有了客户端和工具
  （同时在 _conf_schema.json 里加 Key 和开关两项，AstrBot 会清掉配置里 schema 没有的项）
- 不用管 _CATALOG_ORDER、_BUILTIN_ORDER：没写到的排在最后

本模块不依赖 core 里的其他模块。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

_T = TypeVar("_T")

SEARCH_TOOL = "deepdeep_web_search"
FETCH_TOOL = "deepdeep_web_fetch"
OWN_TOOLS = (SEARCH_TOOL, FETCH_TOOL)
# DeepDeep 自带的 Tavily 搜索：不受“启用自带搜索”开关影响，作为技术类问题的首选和第一备用
TAVILY_TOOL = "deepdeep_tavily_search"
# AstrBot 内置的 Tavily 搜索工具（和 DeepDeep 的 Tavily 是同一个服务）
TAVILY_BUILTIN_SEARCH = "web_search_tavily"
TAVILY_KEY_FIELD = "websearch_tavily_key"
KB_TOOL = "astr_kb_search"

DEEPSEEK_ENDPOINT = "https://api.deepseek.com"
DEEPSEEK_HOST = DEEPSEEK_ENDPOINT.removeprefix("https://")
TAVILY_ENDPOINT = "https://api.tavily.com"

# 和本插件用同一个 DeepSeek 账户计费的工具（AstrBot 4.29.1 起内置的 DeepSeek 搜索、
# 其他 DeepSeek 搜索插件）
DEEPSEEK_BILLED_PATTERNS = ("*deepseek*",)


@dataclass(frozen=True, slots=True)
class Network:
    """工具走哪条网络、能搜到国内还是国外网站。"""

    key: str
    label: str
    patterns: tuple[str, ...]
    # cloud：云端 API；self_hosted：自建服务（请求从服务器发出）；local：不联网；server：从服务器直接访问网页
    kind: str
    endpoint: str | None
    # 服务商所在地：domestic / foreign / self / none
    region: str
    domestic: str
    foreign: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class Builtin:
    """AstrBot 内置网页搜索里的一家。"""

    # AstrBot 设置页的提供商 ID
    id: str
    label: str
    # 读取 Key 的配置项。每家各读各的，不会互相复用
    key_field: str
    # 选中这一家时会加入请求的工具
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Pricing:
    """估算花费用的公开价格。不知道价格的服务不写，面板上列为“未计入”，不当成 0。"""

    usd_per_1000: float
    # 每月免费额度（按月重置的才写；一次性的额度算不了，见 note）
    free_monthly: int = 0
    # 显示在用量页的说明
    note: str = ""


@dataclass(frozen=True, slots=True)
class SearchApi:
    """core/extra_search.py 按它发请求、取结果。请求和返回格式来自各家的官方文档。"""

    tool: str
    description: str
    url: str
    method: str
    auth_header: str
    auth_prefix: str
    query_field: str
    results_field: str
    # 不写表示这家不收条数参数（请求后截断）
    count_field: str = ""
    max_count: int = 10
    title_fields: tuple[str, ...] = ("title",)
    url_fields: tuple[str, ...] = ("url",)
    snippet_fields: tuple[str, ...] = ("snippet",)
    date_fields: tuple[str, ...] = ("date",)
    # 还没有在真实接口上跑过的显示“未实测”，跑通后改成 True
    tested: bool = False


@dataclass(frozen=True, slots=True)
class Listing:
    """设置页「可用的搜索工具」里的一行。"""

    key: str
    name: str
    # own：DeepDeep 自带；builtin：AstrBot 内置网页搜索；plugin：插件；mcp：MCP 服务；kb：知识库
    source: str
    strengths: str
    cost: str
    # 属于这一项的工具名规则；内置网页搜索默认就是它的 tools
    patterns: tuple[str, ...] = ()
    builtin: Builtin | None = None
    # 没装时怎么装
    install: str = ""
    # 还没有现成插件、装不了的服务：工具出现了才显示这一行
    show_when_missing: bool = True

    def __post_init__(self) -> None:
        if not self.patterns and self.builtin is not None:
            object.__setattr__(self, "patterns", self.builtin.tools)


@dataclass(frozen=True, slots=True)
class Provider:
    key: str
    listings: tuple[Listing, ...] = ()
    networks: tuple[Network, ...] = ()
    # 写进 fallback_tool_patterns 默认值的规则。AstrBot 会把默认值存进用户配置，
    # 新增的规则到不了老用户那里，所以新来源不要用它，用 known_tools
    fallback_patterns: tuple[str, ...] = ()
    # 代码里就认识的可替代通用搜索工具。用户的分工规则优先
    known_tools: tuple[str, ...] = ()
    # 备用工具排序时算作“其他综合搜索”的关键词（见 router._alt_rank）
    alt_keywords: tuple[str, ...] = ()
    # 这家是专用搜索时：擅长什么、哪些工具算（写进分工提示）
    specialist_desc: str = ""
    specialist_patterns: tuple[str, ...] = ()
    signup_url: str = ""
    # DeepDeep 自己的客户端才有：存 Key 的插件配置项，以及面板上开关对应的配置项。
    # AstrBot 内置的几家没有这两项：它们的 Key 归 AstrBot 管，DeepDeep 不写 AstrBot 的配置
    key_field: str = ""
    enabled_field: str = ""
    # 有它就由 core/extra_search.py 提供工具，只有填了 Key 且开着才注册
    api: SearchApi | None = None
    # 免费、用的人可能没想要的服务：填了 Key 还要手动打开
    opt_in: bool = False
    pricing: Pricing | None = None
    # 不花钱的服务：超出每月花费上限时也保留
    free: bool = False

    @property
    def specialist_rules(self) -> list[str]:
        return [f"{p} = {self.specialist_desc}" for p in self.specialist_patterns]


def _service_tools(*names: str) -> tuple[str, ...]:
    """还没有现成插件的服务：工具名里同时有服务名和 search / fetch 才算，不猜具体的工具名。"""
    return tuple(
        p
        for name in names
        for p in (
            f"{name}*search*",
            f"{name}*fetch*",
            f"*search*{name}*",
            f"*fetch*{name}*",
        )
    )


_GITHUB_TOOLS = (
    "*github*",
    "search_repositories",
    "search_code",
    "search_issues",
    "search_pull_requests",
)

# 2026-10 对照插件市场整理，见 README「兼容性」。表的顺序就是 Network 的匹配顺序
PROVIDERS: tuple[Provider, ...] = (
    Provider(
        # 同类的 DeepSeek 搜索插件也算这一家：deepseek_web_search（SkyL3gend）/
        # web_search_deepseek、deepseek_fetch_web_page（Fantasality）
        "deepseek",
        signup_url="https://platform.deepseek.com",
        key_field="api_key",
        enabled_field="own_tools_enabled",
        listings=(
            Listing(
                "deepseek",
                "DeepSeek 搜索",
                "own",
                "中文资料覆盖最好，结果带发布日期、相关度和权威度，适合新闻和最新动态",
                "按次计费，每千次 ¥5（国际站 $0.75）",
                OWN_TOOLS,
            ),
            Listing(
                "deepseek_builtin",
                "DeepSeek（AstrBot 内置）",
                "builtin",
                "AstrBot 4.29.1 起自带的基础版 DeepSeek 搜索，和 DeepDeep 用同一个账户计费",
                "按次计费，和 DeepDeep 共用 DeepSeek 账户",
                builtin=Builtin(
                    "deepseek",
                    "DeepSeek",
                    "websearch_deepseek_key",
                    ("web_search_deepseek", "deepseek_fetch_web_page"),
                ),
            ),
        ),
        networks=(
            Network(
                "deepdeep_fetch",
                "DeepSeek 网页抓取",
                (FETCH_TOOL, "deepseek_fetch_web_page"),
                "cloud",
                DEEPSEEK_ENDPOINT,
                "domestic",
                "能读",
                "能读",
                "由 DeepSeek 的服务器去抓网页，服务器自己打不开的网站也能读",
            ),
            Network(
                "deepseek",
                "DeepSeek 搜索",
                (SEARCH_TOOL, "web_search_deepseek", "*deepseek*"),
                "cloud",
                DEEPSEEK_ENDPOINT,
                "domestic",
                "好",
                "好",
                "搜索在 DeepSeek 的服务器上完成，中文内容覆盖最好；服务器只需能连上 api.deepseek.com",
            ),
        ),
    ),
    Provider(
        "github",
        free=True,
        listings=(
            Listing(
                "github",
                "GitHub 搜索（MCP）",
                "mcp",
                "直接搜 GitHub 上的仓库、代码、Issue 和 PR",
                "免费",
                _GITHUB_TOOLS,
                install="在 AstrBot「MCP」里添加 GitHub 官方 MCP 服务",
            ),
        ),
        networks=(
            Network(
                "github",
                "GitHub API",
                _GITHUB_TOOLS,
                "cloud",
                "https://api.github.com",
                "foreign",
                "—",
                "好",
                "只搜 GitHub；国内服务器访问 GitHub 可能不稳定",
            ),
        ),
        specialist_desc="GitHub 上的仓库、代码、Issue 和 PR",
        specialist_patterns=_GITHUB_TOOLS,
    ),
    Provider(
        "arxiv",
        free=True,
        listings=(
            Listing(
                "arxiv",
                "arXiv 论文（MCP）",
                "mcp",
                "搜索和下载 arXiv 论文",
                "免费",
                ("*paper*", "*arxiv*"),
                install="在 AstrBot「MCP」里添加 arXiv MCP 服务",
            ),
        ),
    ),
    Provider(
        # web_searcher_pro 插件：searxng_web_search_* / searxng_web_fetch_url
        "searxng",
        free=True,
        signup_url="https://docs.searxng.org",
        listings=(
            Listing(
                "searxng",
                "SearXNG（web_searcher_pro 插件）",
                "plugin",
                "自建、不花钱，聚合多个搜索引擎，另有图片、视频、新闻、学术等分类搜索",
                "免费（需要自建 SearXNG）",
                ("searxng_*",),
                install="在 AstrBot 插件市场搜索 web_searcher_pro 安装，并自建 SearXNG",
            ),
        ),
        networks=(
            Network(
                "searxng_fetch",
                "SearXNG 插件 · 网页抓取",
                ("searxng_web_fetch_url",),
                "server",
                None,
                "self",
                "取决于服务器",
                "取决于服务器",
                "从你的服务器直接打开网页，服务器访问不了的网站读不到",
            ),
            Network(
                "searxng",
                "SearXNG（自建）",
                ("searxng_*",),
                "self_hosted",
                None,
                "self",
                "取决于引擎",
                "取决于引擎",
                "SearXNG 从你的服务器向各搜索引擎发请求：服务器 IP 被拦截（验证码、限流）的引擎会没有结果，"
                "只剩少数引擎时结果可能完全不相关。点“检测”可以看到哪些引擎还能用",
            ),
        ),
    ),
    Provider(
        "serpapi",
        pricing=Pricing(
            25,
            free_monthly=250,
            note="按最小的 Starter 套餐折算（每月 $25 含 1000 次）",
        ),
        signup_url="https://serpapi.com",
        listings=(
            Listing(
                "serpapi",
                "SerpApi（Google 结果）",
                "builtin",
                "返回 Google 的结果，国内外网站都覆盖，另有图片、学术、财经搜索",
                "免费每月 250 次",
                ("*serpapi*",),
                builtin=Builtin(
                    "serpapi",
                    "SerpApi",
                    "websearch_serpapi_key",
                    (
                        "web_search_serpapi",
                        "image_search_serpapi",
                        "finance_search_serpapi",
                        "scholar_search_serpapi",
                    ),
                ),
            ),
        ),
        # AstrBot 主分支新增的新闻搜索（v4.29.3 还没有，所以不写进 Builtin.tools，免得面板
        # 预览列出不存在的工具）。算可替代的通用搜索，不算专用：见 tests/test_providers.py
        known_tools=("news_search_serpapi",),
        networks=(
            Network(
                "serpapi",
                "SerpApi（Google 结果）",
                ("*serpapi*",),
                "cloud",
                "https://serpapi.com",
                "foreign",
                "较好",
                "好",
                "返回 Google 的搜索结果，国内外网站都覆盖；接口在国外",
            ),
        ),
    ),
    Provider(
        "tavily",
        pricing=Pricing(
            8, free_monthly=1000, note="每次基础搜索 1 个 credit，每个 $0.008"
        ),
        listings=(
            Listing(
                "tavily",
                "Tavily",
                "own",
                "英文和技术资料最好：编程、软件版本、安全漏洞、官方文档",
                "免费每月 1000 次",
                (TAVILY_TOOL,),
            ),
            Listing(
                "tavily_builtin",
                "Tavily（AstrBot 内置）",
                "builtin",
                "和 DeepDeep 的 Tavily 是同一个服务，另带网页读取",
                "免费每月 1000 次（和 DeepDeep 的 Tavily 共用 Key）",
                builtin=Builtin(
                    "tavily",
                    "Tavily",
                    TAVILY_KEY_FIELD,
                    (TAVILY_BUILTIN_SEARCH, "tavily_extract_web_page"),
                ),
            ),
        ),
        networks=(
            Network(
                "tavily",
                "Tavily",
                ("*tavily*",),
                "cloud",
                TAVILY_ENDPOINT,
                "foreign",
                "一般",
                "好",
                "以英文网站为主，中文网站覆盖一般；接口在国外。2026-10 评测里技术类问题（编程、版本、漏洞、文档）表现最好，但不带发布日期",
            ),
        ),
        signup_url="https://app.tavily.com",
        key_field="tavily_api_key",
        enabled_field="tavily_enabled",
    ),
    Provider(
        "brave",
        pricing=Pricing(
            5, note="列表价（来自 Openbenchmarks 2026-10-21），有免费额度的没有扣除"
        ),
        signup_url="https://api-dashboard.search.brave.com",
        listings=(
            Listing(
                "brave",
                "Brave Search",
                "builtin",
                "独立的搜索索引，英文网站为主，注重隐私",
                "有免费额度",
                builtin=Builtin(
                    "brave", "Brave", "websearch_brave_key", ("web_search_brave",)
                ),
            ),
        ),
        networks=(
            Network(
                "brave",
                "Brave Search",
                ("*brave*",),
                "cloud",
                "https://api.search.brave.com",
                "foreign",
                "一般",
                "好",
                "以英文网站为主；接口在国外",
            ),
        ),
    ),
    Provider(
        # exa_web_search 插件：exa-search / web_fetch_exa
        "exa",
        pricing=Pricing(
            7, note="列表价（来自 Openbenchmarks 2026-10-21），有免费额度的没有扣除"
        ),
        signup_url="https://dashboard.exa.ai",
        listings=(
            Listing(
                "exa",
                "Exa",
                "builtin",
                "语义搜索：按意思找相似的文章、论文和网站，另带网页读取",
                "有免费额度",
                builtin=Builtin(
                    "exa",
                    "Exa",
                    "websearch_exa_key",
                    ("web_search_exa", "exa_get_contents"),
                ),
            ),
            Listing(
                "exa_plugin",
                "Exa（插件）",
                "plugin",
                "Exa 语义搜索的插件版",
                "有免费额度",
                ("exa-search", "web_fetch_exa"),
                install="在 AstrBot 插件市场搜索 exa_web_search 安装",
            ),
        ),
        networks=(
            Network(
                "exa",
                "Exa",
                ("*_exa", "exa_*", "exa-*", "web_fetch_exa"),
                "cloud",
                "https://api.exa.ai",
                "foreign",
                "一般",
                "好",
                "语义搜索，以英文网站为主；接口在国外",
            ),
        ),
        fallback_patterns=("exa_get_contents", "exa-search"),
        alt_keywords=("exa",),
    ),
    Provider(
        "firecrawl",
        pricing=Pricing(
            5, note="列表价（来自 Openbenchmarks 2026-10-21），有免费额度的没有扣除"
        ),
        signup_url="https://www.firecrawl.dev",
        listings=(
            Listing(
                "firecrawl",
                "Firecrawl",
                "builtin",
                "搜索的同时抓取网页全文，适合需要读整页内容的场景",
                "有免费额度",
                builtin=Builtin(
                    "firecrawl",
                    "Firecrawl",
                    "websearch_firecrawl_key",
                    ("web_search_firecrawl", "firecrawl_extract_web_page"),
                ),
            ),
        ),
        networks=(
            Network(
                "firecrawl",
                "Firecrawl",
                ("*firecrawl*",),
                "cloud",
                "https://api.firecrawl.dev",
                "foreign",
                "一般",
                "好",
                "由 Firecrawl 抓取网页；接口在国外",
            ),
        ),
    ),
    Provider(
        # anysearch 插件：anysearch_search / anysearch_batch_search / anysearch_extract
        "anysearch",
        signup_url="https://anysearch.com",
        listings=(
            Listing(
                "anysearch",
                "AnySearch",
                "builtin",
                "聚合多家搜索引擎的结果",
                "按次计费",
                builtin=Builtin(
                    "anysearch",
                    "AnySearch",
                    "websearch_anysearch_key",
                    ("web_search_anysearch",),
                ),
            ),
            Listing(
                "anysearch_plugin",
                "AnySearch（插件）",
                "plugin",
                "AnySearch 聚合搜索的插件版，支持批量搜索",
                "按次计费",
                ("anysearch_*",),
                install="在 AstrBot 插件市场搜索 anysearch 安装",
            ),
        ),
        networks=(
            Network(
                "anysearch",
                "AnySearch",
                ("*anysearch*",),
                "cloud",
                "https://api.anysearch.com",
                "foreign",
                "一般",
                "好",
            ),
        ),
        fallback_patterns=("anysearch_*",),
        alt_keywords=("anysearch",),
    ),
    Provider(
        "bocha",
        signup_url="https://open.bochaai.com",
        listings=(
            Listing(
                "bocha",
                "博查搜索",
                "builtin",
                "中文网站为主，接口在国内，国内服务器连接稳定",
                "按次计费",
                builtin=Builtin(
                    "bocha", "博查", "websearch_bocha_key", ("web_search_bocha",)
                ),
            ),
        ),
        networks=(
            Network(
                "bocha",
                "博查搜索",
                ("*bocha*",),
                "cloud",
                "https://api.bochaai.com",
                "domestic",
                "好",
                "一般",
                "以中文网站为主；接口在国内",
            ),
        ),
    ),
    Provider(
        "baidu",
        signup_url="https://console.bce.baidu.com/qianfan",
        listings=(
            Listing(
                "baidu_ai_search",
                "百度 AI 搜索",
                "builtin",
                "百度的搜索结果，中文网站覆盖广",
                "按次计费（有免费额度）",
                builtin=Builtin(
                    "baidu_ai_search",
                    "百度 AI 搜索",
                    "websearch_baidu_app_builder_key",
                    ("web_search_baidu",),
                ),
            ),
        ),
        networks=(
            Network(
                "baidu",
                "百度 AI 搜索",
                ("*baidu*",),
                "cloud",
                "https://qianfan.baidubce.com",
                "domestic",
                "好",
                "一般",
                "以中文网站为主；接口在国内",
            ),
        ),
    ),
    Provider(
        # kimi_web_search 插件
        "kimi",
        signup_url="https://platform.moonshot.cn",
        listings=(
            Listing(
                "kimi",
                "Kimi 联网搜索",
                "plugin",
                "月之暗面的联网搜索，中文资料好",
                "按次计费",
                ("kimi_web_search", "kimi_web_fetch"),
                install="在 AstrBot 插件市场搜索 kimi_web_search 安装",
            ),
        ),
        networks=(
            Network(
                "kimi",
                "Kimi（月之暗面）",
                ("*kimi*",),
                "cloud",
                "https://api.moonshot.cn",
                "domestic",
                "好",
                "较好",
            ),
        ),
        alt_keywords=("kimi",),
    ),
    Provider(
        # grok_web_search 插件（grok_suite 同名）
        "grok",
        signup_url="https://console.x.ai",
        listings=(
            Listing(
                "grok",
                "Grok 搜索",
                "plugin",
                "包含 X（推特）上的实时内容，适合追热点",
                "按次计费",
                ("grok_web_search", "grok_web_fetch"),
                install="在 AstrBot 插件市场搜索 grok_web_search 安装",
            ),
        ),
        networks=(
            Network(
                "grok",
                "Grok（xAI）",
                ("*grok*",),
                "cloud",
                "https://api.x.ai",
                "foreign",
                "一般",
                "好",
                "包含 X（推特）上的实时内容；接口在国外，插件里可以配置代理或中转地址",
            ),
        ),
        alt_keywords=("grok",),
    ),
    Provider(
        # gemini_search 插件：gemini_search / web_fetch
        "gemini",
        signup_url="https://aistudio.google.com",
        listings=(
            Listing(
                "gemini",
                "Gemini 搜索",
                "plugin",
                "借 Gemini 用 Google 搜索并总结",
                "有免费额度",
                ("gemini_search",),
                install="在 AstrBot 插件市场搜索 gemini_search 安装",
            ),
        ),
        networks=(
            Network(
                "gemini",
                "Gemini（Google）",
                ("*gemini*",),
                "cloud",
                "https://generativelanguage.googleapis.com",
                "foreign",
                "较好",
                "好",
                "接口在国外，国内服务器通常无法直连",
            ),
        ),
        fallback_patterns=("gemini_search",),
        alt_keywords=("gemini",),
    ),
    Provider(
        # mmx_cli_tool 插件：mmx_web_search
        "minimax",
        networks=(
            Network(
                "minimax",
                "MiniMax",
                ("mmx_*",),
                "cloud",
                "https://api.minimaxi.com",
                "domestic",
                "好",
                "一般",
            ),
        ),
    ),
    Provider(
        # astrbot_plugin_keenable 插件：keenable_web_search / keenable_fetch_page
        "keenable",
        free=True,
        signup_url="https://keenable.ai/console",
        listings=(
            Listing(
                "keenable",
                "Keenable",
                "plugin",
                "面向 AI 的搜索引擎，自带网页读取，不用注册也能用",
                "免费（不填 Key 按 IP 限流）",
                ("keenable_*",),
                install="在 AstrBot 插件市场搜索 astrbot_plugin_keenable 安装",
            ),
        ),
        networks=(
            Network(
                "keenable",
                "Keenable",
                ("keenable_*",),
                "cloud",
                "https://api.keenable.ai",
                "foreign",
                "一般",
                "好",
                "接口在国外",
            ),
        ),
        known_tools=("keenable_*search*", "keenable_*fetch*"),
        alt_keywords=("keenable",),
    ),
    Provider(
        "linkup",
        pricing=Pricing(
            5, note="列表价（来自 Openbenchmarks 2026-10-21），有免费额度的没有扣除"
        ),
        listings=(
            Listing(
                "linkup",
                "Linkup",
                "plugin",
                "Linkup 的搜索服务",
                "见服务商官网",
                _service_tools("linkup"),
                show_when_missing=False,
            ),
        ),
        known_tools=_service_tools("linkup"),
        alt_keywords=("linkup",),
    ),
    Provider(
        # “you” 太短，your_memory_search 这样的工具也会命中，所以只认带 you_ 前缀的和全称
        "youcom",
        pricing=Pricing(
            5, note="列表价（来自 Openbenchmarks 2026-10-21），有免费额度的没有扣除"
        ),
        listings=(
            Listing(
                "youcom",
                "You.com",
                "plugin",
                "You.com 的搜索服务",
                "见服务商官网",
                ("you_search", "you_fetch", *_service_tools("youcom", "you_com")),
                show_when_missing=False,
            ),
        ),
        known_tools=("you_search", "you_fetch", *_service_tools("youcom", "you_com")),
        alt_keywords=("youcom",),
    ),
    Provider(
        "nimble",
        pricing=Pricing(
            5, note="列表价（来自 Openbenchmarks 2026-10-21），有免费额度的没有扣除"
        ),
        listings=(
            Listing(
                "nimble",
                "Nimble",
                "plugin",
                "Nimble 的搜索服务",
                "见服务商官网",
                _service_tools("nimble"),
                show_when_missing=False,
            ),
        ),
        known_tools=_service_tools("nimble"),
        alt_keywords=("nimble",),
    ),
    Provider(
        "perplexity",
        pricing=Pricing(5),
        signup_url="https://www.perplexity.ai/account/api",
        key_field="perplexity_api_key",
        enabled_field="perplexity_enabled",
        listings=(
            Listing(
                "perplexity",
                "Perplexity",
                "own",
                "英文网页搜索准确率高，结果带发布日期",
                "按次计费（每千次约 $5）",
                ("deepdeep_perplexity_search", *_service_tools("perplexity", "pplx")),
            ),
        ),
        networks=(
            Network(
                "perplexity",
                "Perplexity",
                ("*perplexity*", "*pplx*"),
                "cloud",
                "https://api.perplexity.ai",
                "foreign",
                "未测",
                "好",
                "接口在国外",
            ),
        ),
        known_tools=(
            "deepdeep_perplexity_search",
            *_service_tools("perplexity", "pplx"),
        ),
        alt_keywords=("pplx", "perplexity"),
        # https://docs.perplexity.ai/api-reference/search-post
        api=SearchApi(
            "deepdeep_perplexity_search",
            "用 Perplexity 联网搜索网页。结果带摘要、链接和发布日期。用简洁的关键词。",
            "https://api.perplexity.ai/search",
            "POST",
            "Authorization",
            "Bearer ",
            "query",
            "results",
            count_field="max_results",
            max_count=20,
        ),
    ),
    Provider(
        "tinyfish",
        free=True,
        signup_url="https://agent.tinyfish.ai/api-keys",
        key_field="tinyfish_api_key",
        enabled_field="tinyfish_enabled",
        opt_in=True,
        listings=(
            Listing(
                "tinyfish",
                "TinyFish",
                "own",
                "免费的英文网页搜索",
                "免费（每天 12000 次以内，也要先申请 Key）",
                ("deepdeep_tinyfish_search", *_service_tools("tinyfish")),
            ),
        ),
        networks=(
            Network(
                "tinyfish",
                "TinyFish",
                ("*tinyfish*",),
                "cloud",
                "https://api.search.tinyfish.ai",
                "foreign",
                "未测",
                "好",
                "接口在国外",
            ),
        ),
        known_tools=("deepdeep_tinyfish_search", *_service_tools("tinyfish")),
        alt_keywords=("tinyfish",),
        # https://docs.tinyfish.ai/search-api
        api=SearchApi(
            "deepdeep_tinyfish_search",
            "用 TinyFish 联网搜索网页。结果带摘要和链接。用简洁的关键词。",
            "https://api.search.tinyfish.ai",
            "GET",
            "X-API-Key",
            "",
            "query",
            "results",
        ),
    ),
    Provider(
        "jina",
        signup_url="https://jina.ai/?sui=apikey",
        key_field="jina_api_key",
        enabled_field="jina_enabled",
        listings=(
            Listing(
                "jina",
                "Jina",
                "own",
                "搜索后直接返回网页内容",
                "新 Key 送 1000 万 token，每次搜索至少 1 万 token",
                ("deepdeep_jina_search", *_service_tools("jina")),
            ),
        ),
        networks=(
            Network(
                "jina",
                "Jina",
                ("*jina*",),
                "cloud",
                "https://s.jina.ai",
                "foreign",
                "未测",
                "好",
                "接口在国外",
            ),
        ),
        known_tools=("deepdeep_jina_search", *_service_tools("jina")),
        alt_keywords=("jina",),
        # https://docs.jina.ai/api-reference/search 。文档只写了每条有 url、title、content，
        # 外层的 data 是按 Jina 的 JSON 格式写的，需要实测确认
        api=SearchApi(
            "deepdeep_jina_search",
            "用 Jina 联网搜索网页。结果带网页内容和链接。用简洁的关键词。",
            "https://s.jina.ai/",
            "POST",
            "Authorization",
            "Bearer ",
            "q",
            "data",
            snippet_fields=("description", "content"),
            date_fields=("publishedTime", "date"),
        ),
    ),
    Provider(
        "serper",
        pricing=Pricing(
            1, note="注册送的 2500 次是一次性的，没法按月扣除，这里全部按付费算"
        ),
        signup_url="https://serper.dev",
        key_field="serper_api_key",
        enabled_field="serper_enabled",
        listings=(
            Listing(
                "serper",
                "Serper",
                "own",
                "返回 Google 的搜索结果",
                "注册送 2500 次，之后按次计费",
                ("deepdeep_serper_search", *_service_tools("serper")),
            ),
        ),
        networks=(
            Network(
                "serper",
                "Serper（Google 结果）",
                ("*serper*",),
                "cloud",
                "https://google.serper.dev",
                "foreign",
                "较好",
                "好",
                "返回 Google 的搜索结果；接口在国外",
            ),
        ),
        known_tools=("deepdeep_serper_search", *_service_tools("serper")),
        alt_keywords=("serper",),
        # 返回字段（organic、link、snippet、date）来自 https://serper.dev 官网示例；
        # 官网没有公开的接口文档页，地址和请求头来自社区代码，需要实测确认
        api=SearchApi(
            "deepdeep_serper_search",
            "用 Serper 搜索 Google，返回 Google 的结果。用简洁的关键词。",
            "https://google.serper.dev/search",
            "POST",
            "X-API-KEY",
            "",
            "q",
            "organic",
            count_field="num",
            max_count=20,
            url_fields=("link",),
        ),
    ),
    Provider(
        "kb",
        free=True,
        listings=(
            Listing(
                "kb",
                "本地知识库",
                "kb",
                "只查 AstrBot 里上传的资料，不联网",
                "免费",
                (KB_TOOL,),
                install="在 AstrBot「知识库」里创建并给人格启用",
            ),
        ),
        networks=(
            Network(
                "kb",
                "本地知识库",
                (KB_TOOL,),
                "local",
                None,
                "none",
                "—",
                "—",
                "只查 AstrBot 里的知识库，不联网",
            ),
        ),
        specialist_desc="本地知识库里的资料",
        specialist_patterns=(KB_TOOL,),
    ),
)

# 设置页的展示顺序（也是工具名匹配的顺序）和内置工具列表的顺序。没写到的排在最后
_CATALOG_ORDER = (
    "deepseek",
    "tavily",
    "serpapi",
    "bocha",
    "baidu_ai_search",
    "tavily_builtin",
    "brave",
    "exa",
    "firecrawl",
    "anysearch",
    "deepseek_builtin",
    "searxng",
    "kimi",
    "grok",
    "gemini",
    "exa_plugin",
    "anysearch_plugin",
    "github",
    "arxiv",
    "kb",
)
_BUILTIN_ORDER = (
    "tavily",
    "bocha",
    "brave",
    "firecrawl",
    "baidu_ai_search",
    "exa",
    "anysearch",
    "serpapi",
    "deepseek",
)


def _pinned(
    items: Iterable[_T], order: tuple[str, ...], key: Callable[[_T], str]
) -> tuple[_T, ...]:
    rank = {k: i for i, k in enumerate(order)}
    return tuple(sorted(items, key=lambda x: rank.get(key(x), len(rank))))


_BY_KEY = {p.key: p for p in PROVIDERS}

CATALOG: tuple[Listing, ...] = _pinned(
    (x for p in PROVIDERS for x in p.listings), _CATALOG_ORDER, lambda x: x.key
)
BACKENDS: tuple[Network, ...] = tuple(n for p in PROVIDERS for n in p.networks)
BUILTINS: tuple[Builtin, ...] = _pinned(
    (x.builtin for x in CATALOG if x.builtin is not None),
    _BUILTIN_ORDER,
    lambda b: b.id,
)
# AstrBot 设置页的网页搜索提供商 -> 会加入请求的内置工具名
BUILTIN_PROVIDER_TOOLS = {b.id: list(b.tools) for b in BUILTINS}
# 内置网页搜索工具名，用于 WebUI 预览（它们在每次请求时才加入工具集）
BUILTIN_WEB_SEARCH_TOOLS = [t for b in BUILTINS for t in b.tools]
EXTRA_FALLBACK_PATTERNS = tuple(q for p in PROVIDERS for q in p.fallback_patterns)
KNOWN_SEARCH_TOOLS = tuple(q for p in PROVIDERS for q in p.known_tools)
ALT_KEYWORDS = tuple(k for p in PROVIDERS for k in p.alt_keywords)


# 能在面板里填 Key 的服务：{服务: 插件配置项}
KEY_FIELDS = {p.key: p.key_field for p in PROVIDERS if p.key_field}
# 由 core/extra_search.py 提供工具的服务
EXTRA_PROVIDERS = tuple(p for p in PROVIDERS if p.api is not None)
EXTRA_TOOLS = tuple(p.api.tool for p in EXTRA_PROVIDERS)
_BY_LISTING = {x.key: p for p in PROVIDERS for x in p.listings}


def get(key: str) -> Provider:
    return _BY_KEY[key]


def of_listing(listing_key: str) -> Provider:
    return _BY_LISTING[listing_key]
