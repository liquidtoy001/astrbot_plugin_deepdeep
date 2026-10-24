// DeepDeep · 插件 WebUI
// 所有用户相关内容（搜索词、链接、错误信息）都用 textContent 渲染，避免 XSS。
// 界面文字以中文为准，t() / tr() 在英文模式下查 i18n.js 的词典；后端返回的说明也经过 tr()。

// AstrBot 4.29 起叫 AstrBotPluginView，4.26–4.28 叫 AstrBotPluginPage（4.29 仍保留这个别名）
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const I18N = window.DeepDeepI18n;
const { t, tr } = I18N;
const $ = (id) => document.getElementById(id);
const form = $("settings");

const UNAVAILABLE_TITLES = {
  no_key: "未配置 API Key",
  budget_daily: "已达今日费用上限",
  budget_monthly: "已达本月费用上限",
  circuit_open: "连续失败，已暂停",
};
const MODE_TEXT = {
  guide_only: "只引导",
  deepseek_first: "DeepSeek 优先",
  deepseek_only: "只用 DeepSeek",
  fallback_only: "暂停 DeepSeek",
  coexist: "不干预",
};
const MODE_HINTS = {
  guide_only: "不隐藏任何工具，只在分工说明里写清先用谁、什么情况再换；工具列表不变，不影响提示词缓存",
  deepseek_first: "DeepSeek 可用时隐藏同类工具，选择更集中；每次切换会让提示词缓存失效一次",
  deepseek_only: "始终隐藏其他通用搜索工具，DeepSeek 不可用时也不降级",
  fallback_only: "隐藏 DeepDeep 的工具，只用其他搜索（与关闭 DeepSeek 搜索开关效果相同）",
  coexist: "所有搜索工具并存，由模型自己选",
};
const VERDICT = {
  broken: ["基本失效", "bad"],
  degraded: ["不稳定", "warn"],
  ok: ["正常", "ok"],
  unknown: ["数据不足", ""],
};
const OUTCOME = {
  billable: ["成功", "ok"],
  empty: ["无结果", "warn"],
  cache_hit: ["缓存", "accent"],
  failed: ["失败", "bad"],
  blocked: ["已拦截", "warn"],
};

let lastStatus = null;
let formDirty = false;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// 列表连接符：中文用顿号，英文用逗号
const joinList = (items) => items.join(I18N.getLang() === "en" ? ", " : "、");

// 金额：主币种为计费币种，括号里是按汇率换算的另一种币种（汇率为 0 时不显示）
const SYMBOLS = { cny: "¥", usd: "$" };
const currency = () => lastStatus?.budget?.currency || "cny";
const rate = () => Number(lastStatus?.settings?.exchange_rate || 0);
function amount(n, code = currency()) {
  const v = n ?? 0;
  return `${SYMBOLS[code]}${v.toFixed(v >= 1 ? 2 : code === "cny" ? 3 : 4)}`;
}
function converted(n, code = currency(), r = rate()) {
  if (!(r > 0)) return "";
  return code === "cny" ? amount((n ?? 0) / r, "usd") : amount((n ?? 0) * r, "cny");
}
const money = (n) => {
  const other = converted(n);
  return other ? `${amount(n)}（${other}）` : amount(n);
};

function toast(message, bad = false) {
  const box = $("toast");
  box.textContent = message;
  box.className = bad ? "toast bad" : "toast";
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (box.hidden = true), 6000);
}

async function withBusy(button, fn) {
  button.disabled = true;
  try {
    return await fn();
  } catch (err) {
    toast(tr(err?.message || String(err)), true);
    return undefined;
  } finally {
    button.disabled = false;
  }
}

// 沙箱里剪贴板接口可能不可用，失败时选中文字让用户手动复制
async function copyText(text, node, okText) {
  try {
    await navigator.clipboard.writeText(text);
    toast(okText);
  } catch {
    if (node) {
      const range = document.createRange();
      range.selectNodeContents(node);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }
    toast(t("浏览器不允许自动复制，已选中，请按 Ctrl+C 复制"));
  }
}

// ---------- 语言 ----------

let langPref = "auto";

function astrbotLocale() {
  try {
    return bridge.getLocale?.() || bridge.getContext?.()?.locale || "zh-CN";
  } catch {
    return "zh-CN";
  }
}

function resolveLang(pref = langPref) {
  if (pref === "zh" || pref === "en") return pref;
  return /^en/i.test(astrbotLocale()) ? "en" : "zh";
}

function applyLang() {
  const next = resolveLang();
  const changed = next !== I18N.getLang();
  I18N.setLang(next);
  document.documentElement.lang = next === "en" ? "en" : "zh-CN";
  I18N.applyStatic(document.body);
  $("lang-select").value = langPref;
  if (changed) rerenderAll();
}

function rerenderAll() {
  if (lastStatus) renderStatus(lastStatus);
  if (lastOverview) renderLanes(lastOverview);
  if (lastSearchStats) renderSearchStats(lastSearchStats);
  if (lastCalls) renderCallsPage(lastCalls);
  if (lastFacets) renderFacets(lastFacets);
  if (lastTools.length) renderTools(lastTools);
  if (lastNetwork) renderNetwork(lastNetwork);
  if (lastCatalog) renderCatalog(lastCatalog);
  renderPref();
  if (usageDays.length) renderChart(usageDays);
  updateModeHint();
  updateBudgetHints();
}

// ---------- 状态 ----------

function setMeter(id, ratio) {
  const bar = $(id);
  if (ratio === null || ratio === undefined) {
    bar.style.width = "0";
    bar.className = "";
    return;
  }
  bar.style.width = `${Math.min(100, ratio * 100).toFixed(1)}%`;
  bar.className = ratio >= 1 ? "bad" : ratio >= 0.8 ? "warn" : "";
}

function renderStatus(s) {
  lastStatus = s;
  $("version").textContent = s.version;
  $("about-version").textContent = s.version;

  const banner = $("banner");
  const mode = s.settings.routing_mode;
  let level = "ok";
  let title = t("DeepSeek 搜索正常");
  let detail = t("路由模式：{mode}", { mode: t(MODE_TEXT[mode] || mode) });
  if (!s.settings.own_tools_enabled) {
    level = "warn";
    title = t("DeepSeek 搜索已关闭，由其他搜索工具负责");
    detail = t("分工说明、调用日志和健康检测照常工作。可在“设置”里重新开启。");
  } else if (mode === "fallback_only") {
    level = "warn";
    title = t("已手动暂停 DeepSeek，正在使用其他搜索工具");
  } else if (!s.available) {
    level = s.unavailable_code === "no_key" ? "bad" : "warn";
    title = t(UNAVAILABLE_TITLES[s.unavailable_code] || "DeepSeek 暂不可用");
    const next =
      mode === "deepseek_only"
        ? t("当前为“只用 DeepSeek”模式，不会降级。")
        : mode === "guide_only"
          ? t("模型调用 DeepDeep 时会收到改用其他搜索工具的建议。")
          : t("已自动降级到其他搜索工具。");
    detail = `${tr(s.unavailable_message)}${I18N.getLang() === "en" ? ". " : "。"}${next}`;
  } else if (s.budget.warning) {
    level = "warn";
    title = t("DeepSeek 搜索正常，费用接近上限");
  }
  banner.className = `banner ${level}`;
  $("banner-title").textContent = title;
  $("banner-detail").textContent = detail;
  $("btn-reset").hidden = !s.circuit.open;

  const b = s.budget;
  $("today-cost").textContent = amount(b.today_cost);
  $("month-cost").textContent = amount(b.month_cost);
  $("today-cost-cny").textContent = converted(b.today_cost) ? `≈ ${converted(b.today_cost)}` : "";
  $("month-cost-cny").textContent = converted(b.month_cost) ? `≈ ${converted(b.month_cost)}` : "";
  $("today-limit").textContent = b.daily_limit ? t("上限 {amount}", { amount: money(b.daily_limit) }) : t("不限制");
  $("month-limit").textContent = b.monthly_limit ? t("上限 {amount}", { amount: money(b.monthly_limit) }) : t("不限制");
  setMeter("today-meter", b.daily_ratio);
  setMeter("month-meter", b.monthly_ratio);

  for (const kind of ["search", "fetch"]) {
    const c = s.today[kind];
    $(`today-${kind}`).textContent = t("{n} 次", { n: c.billable });
    $(`today-${kind}-detail`).textContent = t("缓存 {cache} · 无结果 {empty} · 失败 {failed}", { cache: c.cache_hit, empty: c.empty, failed: c.failed });
  }

  const k = s.key;
  $("key-info").textContent = k.configured ? t("DeepSeek Key：{masked}（{source}）", { masked: k.masked, source: tr(k.source) }) : t("未找到 DeepSeek API Key");
  renderTavilyInfo(s.tavily);
  renderCaps(s);

  if (!formDirty) fillForm(s.settings);
  // 汇率可能刚加载或刚修改，图表里的人民币金额跟着刷新
  if (usageDays.length) renderChart(usageDays);
}

function renderTavilyInfo(tv) {
  if (!tv) return;
  const state = !tv.configured
    ? t("状态：没有找到 Tavily Key（在 AstrBot「配置 → 网页搜索」里填写，或在插件配置里单独填写）")
    : tv.usable
      ? t("状态：可用 · Key {masked}（{source}）", { masked: tv.masked, source: tr(tv.source) })
      : t("状态：不提供 · {reason}", { reason: tr(tv.reason) });
  const f = tv.last_failure;
  const failure = f && tv.usable ? t(" · 最近一次失败：{label}（HTTP {status}）", { label: tr(f.label), status: f.status }) : "";
  // 达到上限时 reason 里已经带了用量
  const usage = tv.configured && tv.usage_text && !tv.limit_reached ? ` · ${tr(tv.usage_text)}` : "";
  $("tavily-info").textContent = state + failure + usage;
}

function fillForm(st) {
  form.own_tools_enabled.checked = st.own_tools_enabled;
  form.tavily_enabled.checked = st.tavily_enabled;
  form.tavily_monthly_limit.value = st.tavily_monthly_limit ?? 0;
  form.tool_monthly_limits.value = (st.tool_monthly_limits || []).join("\n");
  form.search_lane_rules.value = (st.search_lane_rules || []).join("\n");
  form.routing_mode.value = st.routing_mode;
  updateModeHint();
  form.billing_currency.value = st.billing_currency;
  form.daily_budget.value = st.daily_budget;
  form.monthly_budget.value = st.monthly_budget;
  form.exchange_rate.value = st.exchange_rate;
  updateBudgetHints();
  form.circuit_threshold.value = st.circuit_threshold;
  form.circuit_cooldown_seconds.value = st.circuit_cooldown_seconds;
  form.fallback_tool_patterns.value = st.fallback_tool_patterns.join("\n");
  form.specialist_rules.value = st.specialist_rules.join("\n");
  form.inject_search_guide.checked = st.inject_search_guide;
  form.record_queries.checked = st.record_queries;
  form.call_log_enabled.checked = st.call_log_enabled;
  form.call_log_record_other_content.checked = st.call_log_record_other_content;
  form.call_log_retention_days.value = st.call_log_retention_days;
}

function updateModeHint() {
  $("mode-hint").textContent = t(MODE_HINTS[form.routing_mode.value] || "");
}

// 输入费用上限时实时显示对应的另一种币种金额（按表单里的汇率）
function updateBudgetHints() {
  // 跟随表单里尚未保存的币种和汇率
  const code = form.billing_currency.value || "cny";
  const r = Number(form.exchange_rate.value || 0);
  for (const node of document.querySelectorAll(".cur-symbol")) node.textContent = SYMBOLS[code];
  const hint = (value, base) => {
    const v = Number(value || 0);
    const other = v > 0 ? converted(v, code, r) : "";
    return other ? t("约 {other}，{base}", { other, base: t(base) }) : t(base);
  };
  $("daily-hint").textContent = hint(form.daily_budget.value, "0 表示不限制");
  $("monthly-hint").textContent = hint(form.monthly_budget.value, "按自然月统计，0 表示不限制");
}

const lines = (text) => text.split("\n").map((s) => s.trim()).filter(Boolean);

function readForm() {
  return {
    own_tools_enabled: form.own_tools_enabled.checked,
    tavily_enabled: form.tavily_enabled.checked,
    tavily_monthly_limit: Number(form.tavily_monthly_limit.value || 0),
    tool_monthly_limits: lines(form.tool_monthly_limits.value),
    search_lane_rules: lines(form.search_lane_rules.value),
    routing_mode: form.routing_mode.value,
    billing_currency: form.billing_currency.value,
    daily_budget: Number(form.daily_budget.value || 0),
    monthly_budget: Number(form.monthly_budget.value || 0),
    exchange_rate: Number(form.exchange_rate.value || 0),
    circuit_threshold: Number(form.circuit_threshold.value || 3),
    circuit_cooldown_seconds: Number(form.circuit_cooldown_seconds.value || 300),
    fallback_tool_patterns: lines(form.fallback_tool_patterns.value),
    specialist_rules: lines(form.specialist_rules.value),
    inject_search_guide: form.inject_search_guide.checked,
    record_queries: form.record_queries.checked,
    call_log_enabled: form.call_log_enabled.checked,
    call_log_record_other_content: form.call_log_record_other_content.checked,
    call_log_retention_days: Number(form.call_log_retention_days.value || 14),
  };
}

// ---------- 本月调用上限 ----------

function renderCaps(s) {
  const rows = (s.tool_caps || []).map((c) => ({
    name: c.pattern,
    tools: Object.keys(c.tools || {}),
    used: c.used,
    limit: c.limit,
  }));
  const tv = s.tavily;
  if (tv?.monthly_limit > 0) rows.unshift({ name: "deepdeep_tavily_search", tools: [], used: tv.month_used, limit: tv.monthly_limit });
  $("sec-caps").hidden = !rows.length;
  const body = $("caps-body");
  body.replaceChildren();
  for (const r of rows) {
    const full = r.used >= r.limit;
    body.append(
      el(
        "tr",
        {},
        el("td", {}, el("code", {}, r.name), r.tools.length && !(r.tools.length === 1 && r.tools[0] === r.name) ? el("div", { class: "muted small tools-line", title: r.tools.join("\n") }, joinList(r.tools)) : null),
        el("td", { class: "num" }, r.used),
        el("td", { class: "num" }, r.limit),
        el("td", {}, el("span", { class: `badge ${full ? "bad" : r.used >= r.limit * 0.8 ? "warn" : "ok"}` }, full ? t("已达上限，本月不再提供") : t("正常"))),
      ),
    );
  }
}

// ---------- 图表 ----------

function renderChart(days) {
  const box = $("chart");
  // 页面隐藏时量不到宽度，等切换到“用量”页再画
  if (!box.clientWidth) return;
  box.replaceChildren();
  const NS = "http://www.w3.org/2000/svg";
  // 按容器实际宽度绘制，避免拉伸导致文字变形
  const W = Math.max(280, Math.round(box.clientWidth));
  const H = 180;
  const pad = { l: 28, r: 6, t: 8, b: 20 };
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");

  const totals = days.map((d) => d.search.billable + d.fetch.billable);
  const max = Math.max(...totals, 0);
  const sum = totals.reduce((a, b) => a + b, 0);
  const cost = days.reduce((a, d) => a + d.cost, 0);
  $("chart-summary").textContent = t("30 天合计 {sum} 次，预估 {cost}", { sum, cost: money(cost) });
  svg.setAttribute("aria-label", t("近 30 天计费 {sum} 次", { sum }));

  const text = (x, y, content, cls, anchor = "start") => {
    const node = document.createElementNS(NS, "text");
    node.setAttribute("x", x);
    node.setAttribute("y", y);
    node.setAttribute("class", cls);
    node.setAttribute("text-anchor", anchor);
    node.textContent = content;
    svg.append(node);
  };

  if (max === 0) {
    text(W / 2, H / 2, t("还没有计费调用"), "empty", "middle");
    box.append(svg);
    return;
  }

  const plotH = H - pad.t - pad.b;
  const step = (W - pad.l - pad.r) / days.length;
  const barW = Math.max(2, step * 0.68);
  const top = Math.ceil(max / 4) * 4 || 4;

  for (let i = 0; i <= 2; i++) {
    const v = (top / 2) * i;
    const y = pad.t + plotH - (v / top) * plotH;
    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", pad.l);
    line.setAttribute("x2", W - pad.r);
    line.setAttribute("y1", y);
    line.setAttribute("y2", y);
    line.setAttribute("class", "grid");
    svg.append(line);
    text(pad.l - 4, y + 3, String(v), "axis", "end");
  }

  days.forEach((d, i) => {
    const x = pad.l + i * step + (step - barW) / 2;
    let y = pad.t + plotH;
    const g = document.createElementNS(NS, "g");
    const tip = document.createElementNS(NS, "title");
    tip.textContent = t("{date}  搜索 {search} · 抓取 {fetch} · {cost}", { date: d.date, search: d.search.billable, fetch: d.fetch.billable, cost: money(d.cost) });
    g.append(tip);
    for (const kind of ["search", "fetch"]) {
      const v = d[kind].billable;
      if (!v) continue;
      const h = (v / top) * plotH;
      y -= h;
      const r = document.createElementNS(NS, "rect");
      r.setAttribute("x", x);
      r.setAttribute("y", y);
      r.setAttribute("width", barW);
      r.setAttribute("height", Math.max(1, h));
      r.setAttribute("class", `bar-${kind}`);
      g.append(r);
    }
    // 透明的整列热区，方便悬停看提示
    const hit = document.createElementNS(NS, "rect");
    hit.setAttribute("x", pad.l + i * step);
    hit.setAttribute("y", pad.t);
    hit.setAttribute("width", step);
    hit.setAttribute("height", plotH);
    hit.setAttribute("fill", "transparent");
    g.append(hit);
    svg.append(g);
    const every = W < 480 ? 10 : 5;
    if (i % every === 0 || i === days.length - 1) {
      text(x + barW / 2, H - 5, d.date.slice(5), "axis", "middle");
    }
  });
  box.append(svg);
}

// ---------- 表格 ----------

function emptyRow(cols, message) {
  return el("tr", { class: "empty-row" }, el("td", { colspan: cols }, message));
}

function visibility(visible) {
  return visible ? el("span", { class: "badge ok" }, t("可见")) : el("span", { class: "badge" }, t("隐藏"));
}

const TOOLS_COLLAPSED = 6;
let toolsExpanded = false;
let lastTools = [];

function renderTools(tools) {
  lastTools = tools;
  const body = $("tools-body");
  body.replaceChildren();
  if (!tools.length) {
    body.append(emptyRow(5, t("没有读取到工具")));
    return;
  }
  const roles = {
    deepseek: ["DeepSeek", "accent"],
    fallback: ["备用搜索", "warn"],
    specialist: ["专用搜索", ""],
    other: ["其他", ""],
  };
  // 只列出和搜索路由有关的，其他工具折叠成一行统计；搜索工具多时先显示前几个
  const relevant = tools.filter((x) => x.role !== "other");
  const others = tools.length - relevant.length;
  const shown = toolsExpanded ? relevant : relevant.slice(0, TOOLS_COLLAPSED);
  const more = $("btn-tools-more");
  more.hidden = relevant.length <= TOOLS_COLLAPSED;
  more.textContent = toolsExpanded ? t("收起") : t("展开其余 {n} 个搜索工具", { n: relevant.length - shown.length });
  for (const x of shown) {
    let [label, cls] = roles[x.role];
    label = t(label);
    if (x.role === "deepseek" && lastStatus && !lastStatus.settings.own_tools_enabled) [label, cls] = [t("DeepDeep · 已关闭"), ""];
    body.append(
      el(
        "tr",
        {},
        el("td", {}, el("code", {}, x.name)),
        el("td", {}, el("span", { class: `badge ${cls}` }, x.specialty ? `${label} · ${tr(x.specialty)}` : x.same_backend ? `${label} · ${t("DeepSeek 计费")}` : label)),
        el("td", {}, visibility(x.visible_when_ok)),
        el("td", {}, visibility(x.visible_when_down)),
        el("td", {}, visibility(x.visible_when_budget)),
      ),
    );
  }
  if (others) body.append(emptyRow(5, t("另有 {n} 个其他工具不受影响", { n: others })));
}

// 展开状态按记录 ID 记住，30 秒自动刷新时不会收起
const openRows = new Set();

function kv(pairs) {
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of pairs) {
    if (v === undefined || v === null || v === "") continue;
    dl.append(el("dt", {}, t(k)), el("dd", {}, typeof v === "object" ? JSON.stringify(v) : String(v)));
  }
  return dl;
}

function section(title, ...children) {
  return el("div", {}, el("h3", {}, title), ...children);
}

function safeLink(url) {
  // 只把 http(s) 链接渲染成可点击，其他一律当文本
  return /^https?:\/\//i.test(url || "")
    ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, url)
    : document.createTextNode(url || "");
}

const yesNo = (v) => (v ? t("是") : t("否"));

function renderDetail(it) {
  const d = it.detail || {};
  const grid = el("div", { class: "detail-grid" });

  grid.append(
    section(
      t("概要"),
      kv([
        ["请求 ID", it.request_id ? t("#{id}（与工具结果第一行、日志一致）", { id: it.request_id }) : ""],
        ["时间", new Date(it.ts * 1000).toLocaleString()],
        ["结果", t((OUTCOME[it.outcome] || CALL_STATUS[it.outcome] || [it.outcome])[0])],
        ["HTTP 状态", it.status || ""],
        ["总耗时", it.latency_ms ? `${it.latency_ms} ms` : ""],
        ["返回给模型的字数", d.output_chars],
      ]),
    ),
  );

  if (d.request) {
    const r = d.request;
    grid.append(
      section(
        t("请求参数"),
        kv([
          ["搜索词", r.query],
          ["链接", r.url],
          ["结果数", r.count],
          ["附带正文", r.need_content === undefined ? "" : yesNo(r.need_content)],
          ["用户原始问题", r.ori_question],
        ]),
      ),
    );
  }

  if (d.blocked) {
    grid.append(section(t("未调用的原因"), kv([["代码", d.blocked.code], ["说明", tr(d.blocked.message)]])));
  }

  if (d.attempts?.length) {
    const rows = d.attempts.map((a) =>
      el(
        "tr",
        {},
        el("td", {}, a.attempt),
        el("td", {}, a.status || t("网络错误")),
        el("td", { class: "num" }, `${a.latency_ms} ms`),
        el("td", {}, tr(a.error || "")),
        el("td", {}, a.retry_in_s !== undefined ? t("{s}s 后重试", { s: a.retry_in_s }) : ""),
      ),
    );
    grid.append(
      section(
        t("请求过程（{n} 次）", { n: d.attempts.length }),
        el(
          "table",
          { class: "mini" },
          el("thead", {}, el("tr", {}, ...["次", "状态", "耗时", "错误", "重试"].map((h) => el("th", {}, t(h))))),
          el("tbody", {}, ...rows),
        ),
      ),
    );
  }

  if (d.error) {
    grid.append(
      section(
        t("错误"),
        kv([
          ["HTTP 状态", d.error.status || t("网络错误")],
          ["说明", tr(d.error.message)],
          ["DeepSeek 返回", d.error.detail],
          ["触发熔断", d.error.circuit_opened ? t("是") : ""],
        ]),
      ),
    );
  }

  if (d.fallback) {
    const f = d.fallback;
    grid.append(
      section(
        t("降级"),
        kv([
          ["路由模式", t(MODE_TEXT[f.mode] || f.mode)],
          ["放出的备用工具", f.restored_tools?.length ? joinList(f.restored_tools) : t("无可用备用工具")],
          ["撤下 DeepSeek 工具", yesNo(f.deepseek_tools_removed)],
        ]),
      ),
    );
  }

  if (d.results?.length) {
    const rows = d.results.map((r, i) =>
      el(
        "tr",
        {},
        el("td", {}, i + 1),
        el("td", {}, el("div", {}, r.title || t("(无标题)")), el("div", {}, safeLink(r.url))),
        el("td", { class: "num" }, (r.date || "").slice(0, 10) || "–"),
        el("td", { class: "num" }, Number(r.score ?? 0).toFixed(2)),
        el("td", { class: "num" }, r.authority >= 0 ? r.authority : "–"),
        el("td", {}, r.site || ""),
      ),
    );
    grid.append(
      section(
        t("返回结果（相关度低于阈值的不会给模型；查询带 after:/before: 或问“最新”时不过滤）"),
        el(
          "div",
          { class: "table-wrap" },
          el(
            "table",
            { class: "mini" },
            el("thead", {}, el("tr", {}, ...["#", "标题 / 链接", "日期", "相关度", "权威度", "来源"].map((h) => el("th", {}, t(h))))),
            el("tbody", {}, ...rows),
          ),
        ),
      ),
    );
  }

  if (d.page) {
    grid.append(
      section(
        t("网页"),
        kv([
          ["标题", d.page.title],
          ["正文字数", d.page.content_chars],
          ["是否截断", d.page.truncated ? t("是（超过抓取字数上限）") : t("否")],
        ]),
      ),
    );
  }

  const raw = el("details", {}, el("summary", { class: "muted small" }, t("原始记录（JSON）")));
  raw.append(el("pre", { class: "raw" }, JSON.stringify(it, null, 2)));
  grid.append(raw);
  return grid;
}

// ---------- 调用日志（持久化，所有工具） ----------

const CALL_STATUS = {
  ok: ["成功", "ok"],
  error: ["失败", "bad"],
  blocked: ["已拦截", "warn"],
  empty: ["无结果", "warn"],
  cache_hit: ["缓存", "accent"],
  interrupted: ["中断", "bad"],
  running: ["进行中", ""],
};
const PAGE_SIZE = 50;
let callPage = 0;
let callTotal = 0;
let lastCalls = null;
let lastFacets = null;
const callFilters = $("call-filters");

function sourceLabel(src) {
  if (!src) return "";
  if (src === "deepseek") return "DeepSeek";
  if (src === "builtin") return t("AstrBot 内置");
  if (src.startsWith("plugin:")) return src.slice(7).replace(/^astrbot_plugin_/, "");
  if (src.startsWith("mcp:")) return `MCP · ${src.slice(4)}`;
  return src;
}

function currentCallFilters() {
  const f = Object.fromEntries(new FormData(callFilters).entries());
  const params = {};
  for (const [k, v] of Object.entries(f)) if (v) params[k] = String(v).trim();
  // 6 位十六进制视为请求 ID，精确匹配
  if (/^#?[0-9a-f]{6}$/i.test(params.q || "")) {
    params.request_id = params.q.replace("#", "").toLowerCase();
    delete params.q;
  }
  return params;
}

function pre(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return el("pre", { class: "raw" }, text);
}

function renderCallDetail(call) {
  const grid = el("div", { class: "detail-grid" });
  const [label] = CALL_STATUS[call.status] || [call.status];
  grid.append(
    section(
      t("概要"),
      kv([
        ["记录 ID", call.id],
        ["请求 ID", call.request_id ? t("#{id}（与工具结果第一行、日志一致）", { id: call.request_id }) : ""],
        ["时间", new Date(call.ts * 1000).toLocaleString()],
        ["工具", call.tool],
        ["来源", `${sourceLabel(call.source)}（${call.source}）`],
        ["会话", call.session],
        ["结果", t(label)],
        ["HTTP 状态", call.http_status || ""],
        ["耗时", call.duration_ms !== null && call.duration_ms !== undefined ? `${call.duration_ms} ms` : ""],
        ["返回内容长度", call.result_chars !== null && call.result_chars !== undefined ? t("{n} 字", { n: call.result_chars }) : ""],
        ["说明", tr(call.note)],
        ["错误", tr(call.error)],
      ]),
    ),
  );

  // DeepSeek 的调用有结构化详情，复用原来的展示
  if (call.detail && typeof call.detail === "object") {
    const it = {
      ts: call.ts,
      outcome: call.status,
      request_id: call.request_id,
      status: call.http_status,
      latency_ms: call.duration_ms,
      detail: call.detail,
    };
    const detailGrid = renderDetail(it);
    detailGrid.firstChild.remove(); // 去掉重复的“概要”
    grid.append(...detailGrid.childNodes);
    return grid;
  }

  if (call.content_recorded) {
    if (call.args !== null && call.args !== undefined) grid.append(section(t("调用参数"), pre(call.args)));
    if (call.result) grid.append(section(t("返回结果"), pre(call.result)));
  } else {
    grid.append(
      el(
        "p",
        { class: "muted small" },
        call.source === "deepseek"
          ? t("未记录搜索词和结果：设置里关闭了「记录 DeepSeek 的搜索词、链接和返回结果」。")
          : t("未记录参数和结果：默认只记录其他工具的名称、耗时和状态。需要时在「设置 → 高级设置」中开启「记录其他工具的完整参数和结果」，之后的调用会保存完整内容。"),
      ),
    );
  }
  const raw = el("details", {}, el("summary", { class: "muted small" }, t("原始记录（JSON）")));
  raw.append(pre(call));
  grid.append(raw);
  return grid;
}

function renderCalls(items) {
  const body = $("calls-body");
  body.replaceChildren();
  if (!items.length) {
    body.append(emptyRow(6, t("没有符合条件的调用记录")));
    return;
  }
  for (const it of items) {
    const [label, cls] = CALL_STATUS[it.status] || [it.status, ""];
    const key = `call-${it.id}`;
    const status = it.http_status && it.http_status !== 200 ? ` ${it.http_status}` : "";
    const main = el(
      "tr",
      { class: "row-main", tabindex: "0", "aria-expanded": "false" },
      el("td", { class: "num" }, new Date(it.ts * 1000).toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })),
      el("td", {}, el("code", {}, it.tool)),
      el("td", {}, el("span", { class: "src" }, sourceLabel(it.source))),
      el("td", {}, el("span", { class: `badge ${cls}` }, t(label) + status)),
      el("td", { class: "num" }, it.duration_ms !== null && it.duration_ms !== undefined ? `${it.duration_ms} ms` : "–"),
      el("td", { class: "target" }, tr(it.error || it.note) || (it.request_id ? `#${it.request_id}` : "")),
    );
    const detail = el("tr", { class: "row-detail" }, el("td", { colspan: 6 }, el("span", { class: "muted small" }, t("加载中…"))));
    detail.hidden = true;
    let loaded = false;
    const toggle = async (open) => {
      detail.hidden = !open;
      main.classList.toggle("open", open);
      main.setAttribute("aria-expanded", String(open));
      open ? openRows.add(key) : openRows.delete(key);
      if (open && !loaded) {
        loaded = true;
        try {
          const full = await bridge.apiGet(`call/${it.id}`);
          detail.firstChild.replaceChildren(renderCallDetail(full));
        } catch (err) {
          loaded = false;
          detail.firstChild.replaceChildren(el("span", { class: "muted small" }, t("加载失败：{error}", { error: tr(err.message || String(err)) })));
        }
      }
    };
    main.addEventListener("click", () => toggle(detail.hidden));
    main.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle(detail.hidden);
      }
    });
    body.append(main, detail);
    if (openRows.has(key)) toggle(true);
  }
}

function renderCallsPage(data) {
  lastCalls = data;
  callTotal = data.total;
  renderCalls(data.items);
  const pages = Math.max(1, Math.ceil(callTotal / PAGE_SIZE));
  $("page-info").textContent = t("第 {page} / {pages} 页 · 共 {total} 条", { page: callPage + 1, pages, total: callTotal });
  $("btn-prev").disabled = callPage === 0;
  $("btn-next").disabled = callPage + 1 >= pages;
  const s = data.summary;
  const oldest = s.oldest_ts ? new Date(s.oldest_ts * 1000).toLocaleDateString() : "–";
  $("calls-summary").textContent = t("所有工具的调用记录 · 已保存 {total} 条（最早 {oldest}），保留 {days} 天 · 点击一行查看详情", { total: s.total, oldest, days: s.retention_days });
}

async function loadCalls() {
  renderCallsPage(await bridge.apiGet("calls", { ...currentCallFilters(), limit: PAGE_SIZE, offset: callPage * PAGE_SIZE }));
}

function renderFacets(f) {
  lastFacets = f;
  const fill = (name, values, label) => {
    const select = callFilters.elements[name];
    const current = select.value;
    select.replaceChildren(el("option", { value: "" }, t(label)), ...values.map((v) => el("option", { value: v }, name === "source" ? sourceLabel(v) : v)));
    select.value = values.includes(current) ? current : "";
  };
  fill("source", f.sources, "全部来源");
  fill("tool", f.tools, "全部工具");
}

async function loadFacets() {
  renderFacets(await bridge.apiGet("call-facets"));
}

const reloadCalls = () => loadCalls().catch((err) => toast(tr(err.message), true));
callFilters.addEventListener("submit", (e) => {
  e.preventDefault();
  callPage = 0;
  reloadCalls();
});
for (const name of ["range", "source", "tool", "status"]) {
  callFilters.elements[name].addEventListener("change", () => {
    callPage = 0;
    reloadCalls();
  });
}
$("btn-prev").addEventListener("click", () => {
  callPage = Math.max(0, callPage - 1);
  reloadCalls();
});
$("btn-next").addEventListener("click", () => {
  callPage += 1;
  reloadCalls();
});
$("btn-export").addEventListener("click", (e) =>
  withBusy(e.currentTarget, async () => {
    await bridge.download("call-export", currentCallFilters());
    toast(t("已导出当前筛选条件下的调用日志（JSON Lines）"));
  }),
);
// 面板运行在受限 iframe 里，不能用 confirm()，改为两次点击确认
const clearBtn = $("btn-clear");
let clearTimer;
clearBtn.addEventListener("click", (e) => {
  if (!clearBtn.classList.contains("armed")) {
    clearBtn.classList.add("armed");
    clearBtn.textContent = t("再点一次确认清空");
    clearTimer = setTimeout(() => {
      clearBtn.classList.remove("armed");
      clearBtn.textContent = t("清空");
    }, 4000);
    return;
  }
  clearTimeout(clearTimer);
  clearBtn.classList.remove("armed");
  clearBtn.textContent = t("清空");
  withBusy(e.currentTarget, async () => {
    const r = await bridge.apiPost("call-clear", {});
    callPage = 0;
    await Promise.allSettled([loadCalls(), loadFacets()]);
    toast(t("已清空 {n} 条调用日志", { n: r.removed }));
  });
});

// ---------- 各搜索工具用量 ----------

const CATEGORY = {
  deepdeep: ["DeepDeep", "accent"],
  fallback: ["备用搜索", "warn"],
  special: ["专用搜索", ""],
};
let lastSearchStats = null;

function healthBadge(h) {
  if (!h) return el("span", { class: "muted small" }, "–");
  const [label, cls] = VERDICT[h.verdict] || [h.verdict, ""];
  return el("span", { class: `badge ${cls}`, title: tr(h.reason) }, t(label));
}

function renderSearchStats(data, health = lastOverview?.health || {}) {
  lastSearchStats = data;
  const body = $("stats-body");
  body.replaceChildren();
  if (!data.rows.length) {
    body.append(emptyRow(10, data.logging_enabled ? t("这段时间没有搜索类工具的调用") : t("调用日志已关闭，无法统计")));
  }
  for (const r of data.rows) {
    const [label, cls] = CATEGORY[r.category] || [r.category, ""];
    const answered = r.total - r.blocked;
    const success = answered > 0 ? `${Math.round(((r.ok + r.empty + r.cache_hit) / answered) * 100)}%` : "–";
    body.append(
      el(
        "tr",
        {},
        el("td", {}, el("code", {}, r.tool)),
        el("td", {}, el("span", { class: "src" }, sourceLabel(r.source))),
        el("td", {}, el("span", { class: `badge ${cls}` }, r.same_backend ? `${t(label)} · ${t("DeepSeek 计费")}` : t(label))),
        el("td", {}, healthBadge(health[r.tool])),
        el("td", { class: "num" }, r.total),
        el("td", { class: "num" }, success),
        el("td", { class: "num" }, r.error || "–"),
        el("td", { class: "num" }, r.blocked || "–"),
        el("td", { class: "num" }, r.avg_ms !== null && r.avg_ms !== undefined ? `${r.avg_ms} ms` : "–"),
        el("td", { class: "num" }, r.last_ts ? new Date(r.last_ts * 1000).toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "–"),
      ),
    );
  }
  $("stats-note").textContent = t("合计 {n} 次 · 数据来自调用日志；成功率不含被拦截的调用。“健康”按近 24 小时判断：失败、空结果、结果与搜索词不相关都算有问题，鼠标悬停看原因", { n: data.total });
}

async function loadSearchStats() {
  renderSearchStats(await bridge.apiGet("search-stats", { range: $("stats-range").value }));
}
$("stats-range").addEventListener("change", () => loadSearchStats().catch((err) => toast(tr(err.message), true)));

// ---------- 主力搜索工具 ----------

let lastOverview = null;
const KIND_SHORT = { cloud: "云端", self_hosted: "自建", server: "服务器直连", local: "本机" };

const SETUP = {
  missing: ["缺少配置", "bad"],
  warn: ["建议处理", "warn"],
  info: ["说明", ""],
  ok: ["已配置", "ok"],
};

function toolChip(name, data) {
  const h = data.health[name];
  let [, cls] = h ? VERDICT[h.verdict] || ["", ""] : ["", ""];
  let title = h ? `${t(VERDICT[h.verdict][0])}：${tr(h.reason)}` : t("近 24 小时没有调用");
  if (h?.ignored) {
    cls = "";
    title = `${t("（已忽略）")}${title}`;
  }
  const setup = data.setup?.tool_status?.[name];
  if (setup) {
    cls = SETUP[setup][1];
    title = `${t(SETUP[setup][0])}${t("（见上方“配置检查”）")} · ${title}`;
  }
  const chip = el("span", { class: `chip ${cls}`, title });
  chip.append(el("i", { class: `hdot ${cls}` }), el("code", {}, name));
  return chip;
}

function backendText(name, data) {
  const b = data.backends[name];
  return b ? `${tr(b.label)} · ${t(KIND_SHORT[b.kind] || b.kind)}` : "";
}

// 面板在受限 iframe 里不能用 confirm()：需要确认的操作点两次
function actionBtn(label, onClick, { confirmText = "", danger = false } = {}) {
  const b = el("button", { type: "button", class: `btn ghost small-btn${danger ? " danger" : ""}` }, label);
  let timer;
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    if (confirmText && !b.classList.contains("armed")) {
      b.classList.add("armed");
      b.textContent = confirmText;
      timer = setTimeout(() => {
        b.classList.remove("armed");
        b.textContent = label;
      }, 5000);
      return;
    }
    clearTimeout(timer);
    withBusy(b, onClick);
  });
  return b;
}

async function overviewAction(endpoint, body, okText) {
  const data = await bridge.apiPost(endpoint, body);
  renderLanes(data);
  const failed = Object.entries(data.failed || {});
  if (failed.length) toast(failed.map(([name, why]) => `${name}：${tr(why)}`).join("；"), true);
  else toast(okText);
  if (loaded.has("tools")) loadTools().catch(() => {});
  if (loaded.has("usage")) loadSearchStats().catch(() => {});
}

function problemActions(key, tools, what) {
  const box = el("span", { class: "row-actions" });
  if (tools?.length) {
    box.append(
      actionBtn(
        tools.length > 1 ? t("停用这 {n} 个工具", { n: tools.length }) : t("停用这个工具"),
        () => overviewAction("disable-tools", { tools, reason: what }, t("已停用 {tools}，可以在下方“已处理”里重新启用", { tools: joinList(tools) })),
        { confirmText: t("再点一次确认停用（下一次对话的提示词缓存会失效一次）"), danger: true },
      ),
    );
  }
  box.append(actionBtn(t("忽略"), () => overviewAction("ignore", { key }, t("已忽略，问题变化时会重新提醒"))));
  return box;
}

// 面板不能打开外部网页：链接显示成可复制的地址
function urlLine(url) {
  const code = el("code", {}, url);
  const btn = el("button", { type: "button", class: "btn ghost small-btn" }, t("复制链接"));
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    copyText(url, code, t("已复制链接，在浏览器里打开"));
  });
  return el("div", { class: "small url-line" }, code, " ", btn);
}

function setupRow(it) {
  const [label, cls] = SETUP[it.status] || [it.status, ""];
  return el(
    "li",
    { class: `setup-item ${it.status}` },
    el("span", { class: `badge ${cls}` }, t(label)),
    el(
      "div",
      {},
      el("strong", {}, tr(it.title)),
      el("span", { class: "muted small" }, ` ${tr(it.detail || "")}`),
      it.fix && it.status !== "ok" ? el("div", { class: "small fix" }, `${t("怎么办：")}${tr(it.fix)}`) : null,
      it.url && it.status !== "ok" ? urlLine(it.url) : null,
      it.tools?.length ? el("div", { class: "muted small tools-line", title: it.tools.join("\n") }, it.tools.length > 4 ? t("{list} 等 {n} 个工具", { list: joinList(it.tools.slice(0, 3)), n: it.tools.length }) : joinList(it.tools)) : null,
      (it.status === "missing" || it.status === "warn") && !it.ignored ? problemActions(it.ignore_key, it.fixable_tools, `${it.title}：${it.detail}`) : null,
    ),
  );
}

function renderSetup(setup) {
  const box = $("setup-box");
  box.replaceChildren();
  if (!setup?.items?.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const problems = setup.items.filter((it) => (it.status === "missing" || it.status === "warn") && !it.ignored);
  const rest = setup.items.filter((it) => !problems.includes(it) && !it.ignored);
  box.className = `setup-box ${setup.missing ? "bad" : problems.length ? "warn" : "ok"}`;
  if (problems.length) {
    const parts = [];
    if (setup.missing) parts.push(t("{n} 项缺少配置", { n: setup.missing }));
    if (setup.warn) parts.push(t("{n} 项建议处理", { n: setup.warn }));
    box.append(el("strong", {}, t("配置检查：{parts}", { parts: parts.join(I18N.getLang() === "en" ? ", " : "，") })), el("ul", {}, ...problems.map(setupRow)));
  }
  if (rest.length) {
    const details = el(
      "details",
      {},
      el("summary", { class: problems.length ? "muted small" : "" }, problems.length ? t("其余 {n} 项", { n: rest.length }) : t("✓ 配置检查：搜索工具都已配置好（{n} 项）", { n: rest.length })),
      el("ul", {}, ...rest.map(setupRow)),
    );
    box.append(details);
  }
}

function renderModels(models) {
  const box = $("models-body");
  box.replaceChildren();
  if (!models?.length) {
    box.append(el("p", { class: "muted small" }, t("没有对话模型开启内置联网搜索，联网搜索都由搜索工具负责。")));
    return;
  }
  const list = el("ul", { class: "models-list" });
  for (const m of models) {
    const [label, cls] = !m.enabled ? ["未启用", ""] : m.tools_broken ? ["可能冲突", "warn"] : m.is_default ? ["默认在用", "accent"] : ["已启用", ""];
    const notes = [];
    if (m.tools_broken) notes.push(t("AstrBot 的说明是开启后函数工具会失效，DeepDeep 和其他搜索工具可能都调用不了"));
    if (m.extra_cost) notes.push(t("会额外计费"));
    if (m.method !== "gemini_url") notes.push(t("和搜索工具作用重复，可能搜两遍"));
    list.append(
      el(
        "li",
        {},
        el("span", { class: `badge ${cls}` }, t(label)),
        el(
          "div",
          {},
          el("strong", {}, m.provider_id),
          m.model ? el("span", { class: "muted small" }, ` · ${m.model}`) : null,
          el("div", { class: "small" }, tr(m.label)),
          el("div", { class: "muted small" }, notes.join(I18N.getLang() === "en" ? "; " : "；")),
          m.enabled ? el("div", { class: "small fix" }, t("想关掉的话：在 AstrBot「提供商」里编辑 {id}，关闭{what}", { id: m.provider_id, what: m.method === "extra_body" ? t("「自定义请求体」里的搜索参数") : m.method === "model" ? t("（换一个不带搜索的模型）") : t("「启用原生搜索功能」") })) : null,
        ),
      ),
    );
  }
  box.append(list);
}

function renderLanes(data) {
  lastOverview = data;
  renderSetup(data.setup);
  renderHandled(data);
  renderModels(data.model_search);
  const box = $("lanes");
  box.replaceChildren();
  if (!data.lanes.length) {
    box.append(el("p", { class: "muted small" }, t("没有读取到搜索类工具")));
  }
  // 通用搜索、读取网页：首选和备用分开写；专用搜索合成一张表
  const main = data.lanes.filter((l) => !l.key.startsWith("special:"));
  const special = data.lanes.filter((l) => l.key.startsWith("special:"));
  for (const lane of main) {
    const card = el("article", { class: "lane" });
    card.append(el("div", { class: "lane-title" }, el("strong", {}, tr(lane.title)), el("span", { class: "muted small" }, tr(lane.desc))));
    card.append(
      el("div", { class: "lane-row" }, el("span", { class: "lane-label" }, t("首选")), el("div", {}, toolChip(lane.primary, data), el("div", { class: "muted small" }, backendText(lane.primary, data)))),
    );
    if (lane.backups.length) {
      const list = el("div", { class: "chips" });
      for (const name of lane.backups.slice(0, 5)) list.append(toolChip(name, data));
      if (lane.backups.length > 5) list.append(el("span", { class: "muted small" }, t("等 {n} 个", { n: lane.backups.length })));
      card.append(el("div", { class: "lane-row" }, el("span", { class: "lane-label" }, t("备用")), list));
    }
    box.append(card);
  }
  if (special.length) {
    const card = el("article", { class: "lane lane-special" }, el("div", { class: "lane-title" }, el("strong", {}, t("专用搜索")), el("span", { class: "muted small" }, t("按需求选对口的工具，始终保留"))));
    const grid = el("div", { class: "spec-grid" });
    for (const lane of special) {
      const chips = el("div", { class: "chips" });
      for (const name of [lane.primary, ...lane.backups].slice(0, 3)) chips.append(toolChip(name, data));
      const extra = lane.backups.length + 1 - 3;
      if (extra > 0) chips.append(el("span", { class: "muted small" }, t("等 {n} 个", { n: lane.backups.length + 1 })));
      grid.append(el("div", { class: "spec-title" }, tr(lane.title)), chips);
    }
    card.append(grid);
    box.append(card);
  }
  const capped = Object.entries(data.capped || {});
  if (capped.length) {
    box.append(el("p", { class: "muted small" }, t("本月已达调用上限、暂不提供：{list}", { list: joinList(capped.map(([name, c]) => `${name}（${c.used} / ${c.limit}）`)) })));
  }
  const flagged = Object.entries(data.health).filter(([, h]) => (h.verdict === "broken" || h.verdict === "degraded") && !h.ignored);
  const alert = $("health-alert");
  alert.hidden = !flagged.length;
  alert.replaceChildren();
  if (flagged.length) {
    alert.append(el("strong", {}, t("⚠ {n} 个搜索工具近 24 小时表现异常（只提示，不会改变路由）", { n: flagged.length })));
    const ul = el("ul");
    for (const [name, h] of flagged.sort((a, b) => (a[1].verdict === "broken" ? 0 : 1) - (b[1].verdict === "broken" ? 0 : 1))) {
      ul.append(
        el(
          "li",
          {},
          healthBadge(h),
          " ",
          el("code", {}, name),
          el("span", { class: "muted small" }, ` ${tr(h.reason)}`),
          problemActions(h.ignore_key, h.fixable ? [name] : [], `${VERDICT[h.verdict][0]}：${h.reason}`),
        ),
      );
    }
    alert.append(ul);
  }
  if (lastSearchStats) renderSearchStats(lastSearchStats, data.health);
}

function renderHandled(data) {
  const box = $("handled");
  const body = $("handled-body");
  body.replaceChildren();
  const ignored = [
    ...(data.setup?.items || []).filter((it) => it.ignored).map((it) => ({ key: it.ignore_key, title: tr(it.title), detail: tr(it.detail) })),
    ...Object.entries(data.health || {})
      .filter(([, h]) => h.ignored)
      .map(([tool, h]) => ({ key: h.ignore_key, title: tool, detail: `${t(VERDICT[h.verdict][0])}：${tr(h.reason)}` })),
  ];
  const disabled = data.handled?.disabled || [];
  box.hidden = !ignored.length && !disabled.length;
  const parts = [];
  if (disabled.length) parts.push(t("已停用 {n} 个工具", { n: disabled.length }));
  if (ignored.length) parts.push(t("已忽略 {n} 条提醒", { n: ignored.length }));
  $("handled-summary").textContent = t("已处理：{parts}", { parts: parts.join(I18N.getLang() === "en" ? ", " : "，") });
  if (disabled.length) {
    body.append(el("h3", {}, t("由 DeepDeep 停用的工具（也可以在 AstrBot 的工具管理里启用）")));
    const ul = el("ul");
    for (const d of disabled) {
      ul.append(
        el(
          "li",
          {},
          el("code", {}, d.tool),
          el("span", { class: "muted small" }, ` ${d.ts ? new Date(d.ts * 1000).toLocaleString() : ""} · ${tr(d.reason || "")}${d.still_disabled ? "" : t(" · 已在别处重新启用")}`),
          el(
            "span",
            { class: "row-actions" },
            actionBtn(d.still_disabled ? t("重新启用") : t("从列表移除"), () => overviewAction("enable-tools", { tools: [d.tool] }, t("已重新启用 {tool}", { tool: d.tool }))),
          ),
        ),
      );
    }
    body.append(ul);
  }
  if (ignored.length) {
    body.append(el("h3", {}, t("已忽略的提醒（问题变化、或恢复后再出问题时会重新提醒）")));
    const ul = el("ul");
    for (const it of ignored) {
      ul.append(
        el(
          "li",
          {},
          el("strong", {}, it.title),
          el("span", { class: "muted small" }, ` ${it.detail}`),
          el("span", { class: "row-actions" }, actionBtn(t("恢复提醒"), () => overviewAction("unignore", { key: it.key }, t("已恢复提醒")))),
        ),
      );
    }
    body.append(ul);
  }
}

async function loadOverview() {
  renderLanes(await bridge.apiGet("overview", { range: "24h" }));
}

// ---------- 网络连通性 ----------

let lastNetwork = null;

function probeCell(res) {
  if (!res) return el("span", { class: "muted small" }, t("未检测"));
  return res.ok
    ? el("span", { class: "badge ok", title: `HTTP ${res.status}` }, t("可连接 · {ms} ms", { ms: res.ms }))
    : el("span", { class: "badge bad" }, t("连不上（{error}）", { error: tr(res.error) }));
}

function coverage(text) {
  const cls = { 好: "ok", 能读: "ok", 较好: "ok", 一般: "warn", 取决于引擎: "warn", 取决于服务器: "warn" }[text] || "";
  return el("span", { class: `badge ${cls}` }, tr(text));
}

function renderNetwork(data) {
  lastNetwork = data;
  const check = data.last_check;
  const body = $("net-body");
  body.replaceChildren();
  if (!data.groups.length) body.append(emptyRow(5, t("没有读取到搜索类工具")));
  for (const g of data.groups) {
    const ep = g.endpoint ? check?.endpoints?.[g.endpoint] : null;
    let probe;
    if (g.key === "searxng" && check?.searxng) {
      const sx = check.searxng;
      probe = sx.ok
        ? el("span", { class: `badge ${sx.working.length <= 1 || sx.relevant === 0 ? "bad" : sx.failed.length ? "warn" : "ok"}` }, t("{n} 个引擎可用", { n: sx.working.length }))
        : el("span", { class: "badge bad" }, t("查询失败（{error}）", { error: tr(sx.error) }));
    } else if (g.kind === "server") {
      probe = check ? el("span", { class: `badge ${check.server_foreign_ok ? "ok" : "warn"}` }, check.server_foreign_ok ? t("服务器能访问国外") : t("服务器访问不了国外")) : el("span", { class: "muted small" }, t("未检测"));
    } else if (g.kind === "local") {
      probe = el("span", { class: "muted small" }, t("不联网"));
    } else {
      probe = check ? probeCell(ep) : el("span", { class: "muted small" }, t("未检测"));
    }
    body.append(
      el(
        "tr",
        {},
        el("td", {}, el("strong", {}, tr(g.label)), el("div", { class: "muted small tools-line", title: g.tools.join("\n") }, g.tools.length > 3 ? t("{list} 等 {n} 个", { list: joinList(g.tools.slice(0, 2)), n: g.tools.length }) : joinList(g.tools))),
        el(
          "td",
          {},
          el("span", { class: `badge ${g.intranet ? "accent" : ""}` }, tr(g.kind_label)),
          g.region !== "none" && g.region !== "self" ? el("span", { class: "muted small" }, t(" 服务商在{region}", { region: tr(g.region_label) })) : null,
          el("div", { class: "endpoint" }, g.endpoint ? el("code", {}, g.endpoint) : el("span", { class: "muted small" }, g.kind === "server" ? t("直接访问目标网页") : "")),
        ),
        el("td", { class: "cover" }, el("div", {}, el("span", { class: "muted small" }, t("国内 ")), coverage(g.domestic)), el("div", {}, el("span", { class: "muted small" }, t("国外 ")), coverage(g.foreign))),
        el("td", {}, probe),
        el("td", { class: "small note" }, tr(g.note)),
      ),
    );
  }

  const sum = $("net-summary");
  sum.hidden = !check;
  sum.replaceChildren();
  if (check) {
    const line = (region, title) =>
      el(
        "div",
        {},
        el("span", { class: `badge ${check[`server_${region}_ok`] ? "ok" : "bad"}` }, `${t(title)}${check[`server_${region}_ok`] ? t("可访问") : t("不可访问")}`),
        " ",
        ...check.sites[region].map((x) => el("span", { class: "muted small site" }, `${tr(x.label)} ${x.ok ? `${x.ms} ms` : tr(x.error)}`)),
      );
    sum.append(
      el("div", { class: "muted small" }, t("服务器网络（{time} 检测）", { time: new Date(check.checked_at * 1000).toLocaleString() })),
      line("domestic", "国内网站"),
      line("foreign", "国外网站"),
    );
  }

  const box = $("searxng-box");
  const sx = check?.searxng;
  box.hidden = !sx;
  box.replaceChildren();
  if (sx?.ok) {
    box.append(
      el("h3", {}, t("SearXNG 引擎（{url}，测试词 DeepSeek：{results} 条结果，{relevant} 条包含测试词）", { url: data.searxng_url, results: sx.results, relevant: sx.relevant })),
      el("div", {}, el("span", { class: "muted small" }, t("能用：")), ...(sx.working.length ? sx.working.map((e) => el("span", { class: "badge ok eng" }, e)) : [el("span", { class: "badge bad" }, t("没有"))])),
    );
    if (sx.failed.length) {
      box.append(el("div", {}, el("span", { class: "muted small" }, t("被拦截或无响应：")), ...sx.failed.map((f) => el("span", { class: "badge bad eng", title: tr(f.reason) }, `${f.engine}（${tr(f.reason)}）`))));
    }
    if (sx.working.length <= 1 || sx.relevant === 0) {
      box.append(el("p", { class: "small warn-text" }, t("只剩很少的引擎能用时，SearXNG 的通用搜索结果往往和问题无关。可以考虑在 SearXNG 里换用其他引擎、给服务器配置代理，或在 AstrBot 里关掉这些工具。")));
    }
  }
}

async function loadNetwork() {
  renderNetwork(await bridge.apiGet("network"));
}

// ---------- 搜索偏好 ----------
// 预设之间切换不用请求后端：后端一次给出每个预设下各场景的顺序。改动后立即保存

let prefData = null;
let prefPreset = "rec";
let prefScene = "general";
let prefOrders = {};
let prefTimer;

const clone = (x) => JSON.parse(JSON.stringify(x));

function prefLines() {
  return Object.keys(prefData.scenes)
    .map((scene) => {
      const items = prefOrders.custom[scene] || [];
      const names = [...items.filter((x) => !x.disabled).map((x) => x.name), ...items.filter((x) => x.disabled).map((x) => `!${x.name}`)];
      return names.length ? `${scene} = ${names.join(", ")}` : "";
    })
    .filter(Boolean);
}

function setPrefState(text, cls = "") {
  $("pref-state").textContent = text;
  $("pref-state").className = `save-state small ${cls}`;
}

async function savePref() {
  setPrefState(t("保存中…"), "muted");
  try {
    const body = { search_preference: prefPreset };
    if (prefPreset === "custom") body.search_order = prefLines();
    renderStatus(await bridge.apiPost("settings", body));
    setPrefState(t("已保存 · {time}", { time: new Date().toLocaleTimeString() }), "ok-text");
    // 分工变了：概览马上刷新，工具页打开时再刷新
    loaded.delete("tools");
    loadOverview().catch(() => {});
  } catch (err) {
    setPrefState(t("保存失败：{error}", { error: tr(err?.message || String(err)) }), "bad-text");
  }
}

function schedulePrefSave(delay) {
  setPrefState(t("有未保存的修改…"), "muted");
  clearTimeout(prefTimer);
  prefTimer = setTimeout(savePref, delay);
}

// 改顺序或“不用”时切到自定义（从当前看到的顺序开始改）
function editablePref() {
  if (prefPreset !== "custom") {
    prefOrders.custom = clone(prefOrders[prefPreset]);
    prefPreset = "custom";
  }
  return prefOrders.custom[prefScene];
}

function prefChips(box, entries, current, onPick) {
  box.replaceChildren(
    ...entries.map(([key, label]) => {
      const b = el("button", { type: "button", class: key === current ? "on" : "", "aria-pressed": String(key === current) }, tr(label));
      b.addEventListener("click", () => onPick(key));
      return b;
    }),
  );
}

function renderPref() {
  if (!prefData) return;
  prefChips($("pref-presets"), Object.entries(prefData.presets), prefPreset, (key) => {
    prefPreset = key;
    if (key === "custom" && !prefOrders.custom) prefOrders.custom = clone(prefOrders.rec);
    renderPref();
    schedulePrefSave(0);
  });
  prefChips($("pref-scenes"), Object.entries(prefData.scenes), prefScene, (key) => {
    prefScene = key;
    renderPref();
  });
  const list = $("pref-list");
  const items = prefOrders[prefPreset]?.[prefScene] || [];
  list.replaceChildren();
  if (!items.length) list.append(el("li", { class: "off" }, el("span"), el("span", { class: "muted small" }, t("这一类现在没有可用的工具")), el("span")));
  let n = 0;
  items.forEach((it, i) => {
    const up = el("button", { type: "button", class: "btn ghost small-btn", "aria-label": t("上移") }, "↑");
    const down = el("button", { type: "button", class: "btn ghost small-btn", "aria-label": t("下移") }, "↓");
    const toggle = el("button", { type: "button", class: "btn ghost small-btn" }, it.disabled ? t("启用") : t("不用"));
    const move = (to) => {
      const arr = editablePref();
      if (to < 0 || to >= arr.length || arr[to].disabled !== arr[i].disabled) return;
      [arr[i], arr[to]] = [arr[to], arr[i]];
      renderPref();
      schedulePrefSave(800);
    };
    up.addEventListener("click", () => move(i - 1));
    down.addEventListener("click", () => move(i + 1));
    toggle.addEventListener("click", () => {
      const arr = editablePref();
      const [item] = arr.splice(i, 1);
      item.disabled = !item.disabled;
      // 不用的排到最后；重新启用的接在启用的后面
      const at = item.disabled ? arr.length : arr.filter((x) => !x.disabled).length;
      arr.splice(at, 0, item);
      renderPref();
      schedulePrefSave(800);
    });
    list.append(
      el(
        "li",
        { class: it.disabled ? "off" : "" },
        el("span", { class: "num" }, it.disabled ? "–" : String(++n)),
        el("span", { class: "name", title: (it.tools || [it.name]).join("\n") }, tr(it.label), el("small", {}, [it.name !== it.label ? it.name : "", tr(it.note)].filter(Boolean).join(" · "))),
        el("span", { class: "ctl" }, up, down, toggle),
      ),
    );
  });
  const sep = I18N.getLang() === "en" ? ", " : "、";
  const preview = Object.entries(prefData.scenes).map(([scene, label]) => {
    const on = (prefOrders[prefPreset]?.[scene] || []).filter((x) => !x.disabled).map((x) => tr(x.label));
    if (!on.length) return t("{scene}：（都不用）", { scene: tr(label) });
    if (on.length === 1) return t("{scene}：{first}", { scene: tr(label), first: on[0] });
    return t("{scene}：优先 {first}；不理想时再用 {rest}", { scene: tr(label), first: on[0], rest: on.slice(1, 4).join(sep) });
  });
  $("pref-preview").textContent = `${t("模型会收到的分工说明")}\n${preview.join("\n")}`;
  $("pref-note").textContent = [prefData.mode_note ? tr(prefData.mode_note) : "", t("改动后，下一次对话的提示词缓存会失效一次。")].filter(Boolean).join(" ");
}

// ---------- 可用的搜索工具 ----------

const CATALOG_SHOWN = 5;
const CATALOG_CLS = { active: "accent", ok: "ok", unstable: "warn", missing: "bad", paused: "warn" };
let lastCatalog = null;

function catalogRow(e) {
  const meta = [tr(e.cost), tr(e.detail)].filter(Boolean).join(" · ");
  return el(
    "li",
    {},
    el(
      "div",
      {},
      el("strong", {}, tr(e.name)),
      el("span", { class: "src" }, tr(e.source)),
      e.strengths ? el("div", { class: "why" }, tr(e.strengths)) : null,
      meta ? el("div", { class: "meta" }, meta) : null,
    ),
    el("span", { class: `badge ${CATALOG_CLS[e.status] || ""}` }, tr(e.status_label)),
  );
}

function renderCatalog(list) {
  lastCatalog = list;
  $("catalog-top").replaceChildren(...list.slice(0, CATALOG_SHOWN).map(catalogRow));
  const rest = list.slice(CATALOG_SHOWN);
  $("catalog-more").hidden = !rest.length;
  $("catalog-rest").replaceChildren(...rest.map(catalogRow));
  const counts = {};
  for (const e of rest) counts[e.status_label] = (counts[e.status_label] || 0) + 1;
  const parts = Object.entries(counts).map(([label, n]) => `${tr(label)} ${n}`);
  $("catalog-more-summary").textContent = t("其余 {n} 个：{parts}", { n: rest.length, parts: parts.join(" · ") });
}

async function loadSearchTools() {
  const data = await bridge.apiGet("search-tools");
  renderCatalog(data.catalog);
  prefData = data.preferences;
  prefOrders = clone(prefData.orders);
  prefPreset = prefData.preset;
  renderPref();
}

// ---------- 数据加载 ----------

async function loadStatus() {
  renderStatus(await bridge.apiGet("status"));
}
let usageDays = [];
async function loadUsage() {
  usageDays = (await bridge.apiGet("usage", { days: 30 })).days;
  renderChart(usageDays);
}
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => usageDays.length && renderChart(usageDays), 150);
});

async function loadTools(preview = false) {
  const params = preview
    ? {
        fallback: lines(form.fallback_tool_patterns.value).join("\n"),
        specialists: lines(form.specialist_rules.value).join("\n"),
      }
    : {};
  renderTools((await bridge.apiGet("tools", params)).tools);
}

// ---------- 页面 ----------
// 所有页面在同一个文档里，切换只是显示 / 隐藏：不重新加载 iframe、不重新连接 bridge。
// 每页的数据第一次打开时才加载；状态和概览一开始就加载（顶部横幅、配置检查、健康状况要用）。

const PAGE_LOADERS = {
  overview: () => [loadOverview()],
  usage: () => [loadUsage(), loadSearchStats()],
  calls: () => [loadCalls(), loadFacets()],
  settings: () => [loadSearchTools()],
  tools: () => [loadTools(formDirty), loadNetwork()],
};
const loaded = new Set();
const tabs = [...document.querySelectorAll("#navbar [data-page]")];
const views = [...document.querySelectorAll(".view[data-page]")];
let currentPage = "overview";

function reportFailures(results) {
  const failed = results.find((r) => r.status === "rejected");
  if (failed) toast(t("加载失败：{error}", { error: tr(failed.reason?.message || String(failed.reason)) }), true);
}

async function showPage(name) {
  if (!PAGE_LOADERS[name]) name = "overview";
  currentPage = name;
  for (const v of views) v.hidden = v.dataset.page !== name;
  for (const b of tabs) {
    const on = b.dataset.page === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", String(on));
  }
  // 手机上导航栏可横向滚动：让当前项留在可见范围内（不能用 scrollIntoView，会带动整页滚动）
  const nav = $("navbar");
  const cur = tabs.find((b) => b.dataset.page === name);
  const left = cur.getBoundingClientRect().left - nav.getBoundingClientRect().left + nav.scrollLeft;
  if (left < nav.scrollLeft || left + cur.offsetWidth > nav.scrollLeft + nav.clientWidth) nav.scrollLeft = left - 16;
  if (name === "usage" && usageDays.length) renderChart(usageDays);
  if (!loaded.has(name)) {
    loaded.add(name);
    reportFailures(await Promise.allSettled(PAGE_LOADERS[name]()));
  }
}

for (const b of tabs) b.addEventListener("click", () => showPage(b.dataset.page));

async function refreshAll() {
  // 其他页面下次打开时再重新加载
  loaded.clear();
  loaded.add(currentPage);
  const results = await Promise.allSettled([loadStatus(), loadOverview()]);
  results.push(...(await Promise.allSettled(currentPage === "overview" ? [] : PAGE_LOADERS[currentPage]())));
  reportFailures(results);
}

// ---------- 事件 ----------

// 修改后自动保存：勾选、下拉立即保存；输入框停顿一会儿再保存
const saveState = $("save-state");
let saveTimer;
let saving = Promise.resolve();

function setSaveState(text, cls = "") {
  saveState.textContent = text;
  saveState.className = `save-state small ${cls}`;
}

let editSeq = 0;

function scheduleSave(delay) {
  editSeq += 1;
  formDirty = true;
  setSaveState(t("有未保存的修改…"), "muted");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saving = saving.then(saveNow);
  }, delay);
}

async function saveNow() {
  // 数字框正在输入一半（比如空着）时先不保存
  if (!form.checkValidity()) {
    setSaveState(t("有内容不符合要求，修改后会自动保存"), "bad-text");
    return;
  }
  setSaveState(t("保存中…"), "muted");
  const seq = editSeq;
  try {
    const before = lastStatus?.settings || {};
    const status = await bridge.apiPost("settings", readForm());
    // 保存期间又改了内容：别用服务器返回的值覆盖正在编辑的表单
    formDirty = seq !== editSeq;
    renderStatus(status);
    setSaveState(t("已自动保存 · {time}", { time: new Date().toLocaleTimeString() }), "ok-text");
    const st = status.settings;
    const routingChanged =
      before.own_tools_enabled !== st.own_tools_enabled ||
      before.routing_mode !== st.routing_mode ||
      String(before.fallback_tool_patterns) !== String(st.fallback_tool_patterns) ||
      String(before.specialist_rules) !== String(st.specialist_rules) ||
      before.tavily_enabled !== st.tavily_enabled ||
      before.tavily_monthly_limit !== st.tavily_monthly_limit ||
      String(before.tool_monthly_limits) !== String(st.tool_monthly_limits) ||
      String(before.search_lane_rules) !== String(st.search_lane_rules);
    if (routingChanged) loadSearchTools().catch(() => {});
    if (routingChanged) {
      // 概览马上刷新；工具和网络页打开时再刷新
      loaded.delete("tools");
      const tasks = [loadOverview()];
      if (currentPage === "tools") {
        loaded.add("tools");
        tasks.push(...PAGE_LOADERS.tools());
      }
      await Promise.allSettled(tasks);
    }
  } catch (err) {
    setSaveState(t("保存失败：{error}", { error: tr(err?.message || String(err)) }), "bad-text");
    toast(t("保存失败：{error}", { error: tr(err?.message || String(err)) }), true);
  }
}

form.addEventListener("submit", (e) => e.preventDefault());
form.addEventListener("change", (e) => {
  updateBudgetHints();
  updateModeHint();
  const target = e.target;
  if (target.matches("select, input[type=checkbox]")) scheduleSave(0);
  else scheduleSave(600);
});
form.addEventListener("input", (e) => {
  updateBudgetHints();
  if (e.target.matches("textarea, input[type=number]")) scheduleSave(1200);
});

$("lang-select").addEventListener("change", (e) =>
  withBusy(e.currentTarget, async () => {
    langPref = e.currentTarget.value;
    applyLang();
    // 只保存语言，不带上表单里可能还没保存的修改
    renderStatus(await bridge.apiPost("settings", { panel_language: langPref }));
  }),
);

$("btn-tools-more").addEventListener("click", () => {
  toolsExpanded = !toolsExpanded;
  renderTools(lastTools);
});
$("btn-net").addEventListener("click", (e) =>
  withBusy(e.currentTarget, async () => {
    $("btn-net").textContent = t("检测中…");
    try {
      renderNetwork(await bridge.apiPost("network-check", {}));
      toast(t("连通性检测完成"));
    } finally {
      $("btn-net").textContent = t("检测连通性");
    }
  }),
);

$("btn-copy-repo").addEventListener("click", () => copyText($("repo-url").textContent, $("repo-url"), t("已复制仓库链接，在浏览器打开即可点 Star")));
$("btn-diag").addEventListener("click", (e) =>
  withBusy(e.currentTarget, async () => {
    const data = await bridge.apiGet("diagnostics");
    const box = $("diag");
    box.value = JSON.stringify(data, null, 2);
    box.hidden = false;
    $("btn-diag-copy").hidden = false;
  }),
);
$("btn-diag-copy").addEventListener("click", async () => {
  const box = $("diag");
  box.select();
  try {
    // 面板运行在受限 iframe 中，剪贴板接口可能不可用，失败时保留选中让用户手动复制
    await navigator.clipboard.writeText(box.value);
    toast(t("已复制诊断信息"));
  } catch {
    toast(t("浏览器不允许自动复制，已选中，请按 Ctrl+C 复制"));
  }
});
$("btn-refresh").addEventListener("click", (e) => withBusy(e.currentTarget, refreshAll));
$("btn-reset").addEventListener("click", (e) =>
  withBusy(e.currentTarget, async () => {
    renderStatus(await bridge.apiPost("reset-circuit", {}));
    toast(t("已解除熔断"));
  }),
);
$("btn-test").addEventListener("click", (e) =>
  withBusy(e.currentTarget, async () => {
    const r = await bridge.apiPost("test", {});
    toast(r.ok ? `${tr(r.message)}${r.first_result ? `：${r.first_result}` : ""}` : t("连接失败：{error}", { error: tr(r.message) }), !r.ok);
    const tasks = [loadStatus()];
    if (loaded.has("calls")) tasks.push(loadCalls());
    if (loaded.has("usage")) tasks.push(loadUsage());
    await Promise.allSettled(tasks);
  }),
);

// ---------- 启动 ----------

await bridge.ready();
// 先按 AstrBot 的语言显示，读到插件设置后再按设置调整
applyLang();
// AstrBot 切换语言时跟着切换（只在“跟随 AstrBot”时）
bridge.onContext?.(() => {
  if (langPref === "auto") applyLang();
});
loaded.add("overview");
const first = await Promise.allSettled([loadStatus(), loadOverview()]);
langPref = lastStatus?.settings?.panel_language || "auto";
applyLang();
reportFailures(first);
showPage("overview");
setInterval(() => {
  if (document.visibilityState !== "visible") return;
  // 顶部横幅随时要用状态；其他数据只刷新当前页
  const tasks = [loadStatus()];
  if (currentPage === "overview") tasks.push(loadOverview());
  // 调用日志只在第一页且没有展开详情时自动刷新，免得打断查看
  if (currentPage === "calls" && callPage === 0 && !document.querySelector("tr.row-main.open")) tasks.push(loadCalls());
  Promise.allSettled(tasks);
}, 30000);
