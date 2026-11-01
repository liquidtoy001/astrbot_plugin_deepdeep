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

// 面板里所有的折叠都用它：每次多显示 5 个，可以收起。显示到第几个按 key 记住，刷新时不会收起。
// groups 的每一项是一个节点，或者要一起显示的几个节点（比如调用日志的一行和它的详情）
const foldState = new Map();
const FOLD_STEP = 5;

function foldInto(box, key, groups, { first = FOLD_STEP, colspan = 1 } = {}) {
  const paint = () => {
    const shown = Math.min(groups.length, Math.max(first, foldState.get(key) ?? first));
    const rest = groups.length - shown;
    box.replaceChildren(...groups.slice(0, shown).flat());
    if (rest <= 0 && shown <= first) return;
    const ctl = el("div", { class: "fold-ctl" });
    if (rest > 0) {
      const more = el("button", { type: "button", class: "btn ghost small-btn" }, t("再显示 {n} 个（还有 {rest} 个）", { n: Math.min(FOLD_STEP, rest), rest }));
      more.addEventListener("click", () => {
        foldState.set(key, shown + FOLD_STEP);
        paint();
      });
      ctl.append(more);
    }
    if (shown > first) {
      const less = el("button", { type: "button", class: "btn ghost small-btn" }, t("收起"));
      less.addEventListener("click", () => {
        foldState.delete(key);
        paint();
      });
      ctl.append(less);
    }
    if (box.tagName === "TBODY") box.append(el("tr", { class: "fold-row" }, el("td", { colspan }, ctl)));
    else if (box.tagName === "UL" || box.tagName === "OL") box.append(el("li", { class: "fold-row" }, ctl));
    else box.append(ctl);
  };
  paint();
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
  button.classList.add("busy");
  try {
    return await fn();
  } catch (err) {
    toast(tr(err?.message || String(err)), true);
    return undefined;
  } finally {
    button.disabled = false;
    button.classList.remove("busy");
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
  if (lastRouting) renderRouting(lastRouting);
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

// 概览顶部的状态：说的是“搜索能不能用”，不只是 DeepSeek（本插件管理所有搜索工具）
function renderBanner() {
  const s = lastStatus;
  const o = lastOverview;
  if (!s || !o) return;
  const mode = s.settings.routing_mode;
  const modeText = t("路由模式：{mode}", { mode: t(MODE_TEXT[mode] || mode) });
  const OWN = ["deepdeep_web_search", "deepdeep_web_fetch"];
  const lanes = o.lanes || [];
  const general = lanes.find((l) => l.key === "search");
  const noKey = s.unavailable_code === "no_key";
  // 没有 Key 时自带工具不算可用
  const usable = new Set(lanes.flatMap((l) => [l.primary, ...l.backups]).filter((n) => !(noKey && OWN.includes(n))));
  const hasGeneral = general && !(noKey && general.primary === OWN[0]);
  const flagged =
    (o.setup?.missing || 0) +
    (o.setup?.warn || 0) +
    Object.values(o.health || {}).filter((h) => (h.verdict === "broken" || h.verdict === "degraded") && !h.ignored).length;
  const ownOn = s.settings.own_tools_enabled && mode !== "fallback_only";
  const ownDown = ownOn && !s.available && !noKey;

  let level = "ok";
  let title = t("搜索正常");
  const parts = [];
  if (hasGeneral) parts.push(t("{n} 个搜索工具可用", { n: usable.size }), t("通用搜索首选 {tool}", { tool: general.primary }));
  if (!hasGeneral) {
    level = "bad";
    title = t("没有可用的通用搜索工具");
    // 快速开始就在下面，不用再说一遍
    if (o.search_ready !== false) parts.push(t("模型现在无法联网搜索，下方“配置检查”里有配置方法"));
  } else if (s.spending?.over) {
    level = "warn";
    title = t("本月估算花费已达上限，付费搜索工具暂停提供");
  } else if (ownDown) {
    const others = general.backups.length > 0;
    level = others ? "warn" : "bad";
    title = others ? t("DeepSeek 暂不可用，其他搜索工具正常") : t(UNAVAILABLE_TITLES[s.unavailable_code] || "DeepSeek 暂不可用");
    parts.unshift(tr(s.unavailable_message));
  } else if (flagged) {
    level = "warn";
    title = t("搜索可用，有 {n} 项需要处理", { n: flagged });
  } else if (s.spending?.warning) {
    level = "warn";
    title = t("搜索正常，本月估算花费接近上限");
  }
  parts.push(modeText);
  $("banner").className = `banner ${level}`;
  $("banner-title").textContent = title;
  $("banner-detail").textContent = parts.join(" · ");
}

function renderStatus(s) {
  lastStatus = s;
  $("version").textContent = s.version;

  renderBanner();
  $("btn-reset").hidden = !s.circuit.open;
  // 没有 Key 或关闭了自带搜索时，没有东西可测
  $("btn-test").hidden = !s.key.configured || !s.settings.own_tools_enabled;

  renderSpending(s.spending);

  const all = s.today_tools;
  for (const kind of ["search", "fetch"]) {
    // 调用日志关闭时只有 DeepSeek 自己的统计，这时要说明一下，否则数字的含义变了
    const detail = $(`today-${kind}-detail`);
    detail.hidden = all.logging_enabled;
    detail.textContent = all.logging_enabled ? "" : t("调用日志已关闭，只统计 DeepSeek");
    $(`today-${kind}`).textContent = t("{n} 次", { n: all.logging_enabled ? all[kind].total : s.today[kind].billable });
  }

  renderCaps(s);

  if (!formDirty) fillForm(s.settings);
  // 汇率可能刚加载或刚修改，图表里的人民币金额跟着刷新
  if (usageDays.length) renderChart(usageDays);
}

// ---------- 花费 ----------

// 第 9 个开始合并成“其他”：分类色只有 8 个，不生成新颜色
const SERIES_SLOTS = 8;

// 圆环图：每一段是一家的份额，中间是合计。段之间留 2px 的缝，图例和下面的表写着具体数字
function renderSpendDonut(segs, total) {
  const box = $("spend-donut");
  const tip = $("spend-tip");
  tip.hidden = true;
  box.replaceChildren();
  box.hidden = !segs.length;
  if (!segs.length) return;
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 160 160");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", segs.map((x) => `${x.name} ${Math.round(x.share * 100)}%`).join(", "));
  const R = 62;
  const C = 2 * Math.PI * R;
  const gap = segs.length > 1 ? 2 : 0;
  let start = 0;
  for (const x of segs) {
    const len = x.share * C;
    const arc = document.createElementNS(NS, "circle");
    arc.setAttribute("cx", 80);
    arc.setAttribute("cy", 80);
    arc.setAttribute("r", R);
    arc.setAttribute("fill", "none");
    arc.setAttribute("stroke-width", 24);
    arc.setAttribute("class", "spend-arc");
    arc.setAttribute("tabindex", "0");
    arc.style.stroke = x.color;
    arc.setAttribute("stroke-dasharray", `${Math.max(1, len - gap)} ${C - Math.max(1, len - gap)}`);
    arc.setAttribute("stroke-dashoffset", String(-start));
    arc.setAttribute("transform", "rotate(-90 80 80)");
    const show = (e) => {
      tip.textContent = `${x.name} · ${x.costText} · ${Math.round(x.share * 100)}%`;
      tip.hidden = false;
      const parent = $("sec-spend").getBoundingClientRect();
      const r = box.getBoundingClientRect();
      const px = e.clientX || r.left + r.width / 2;
      const py = e.clientY || r.top + r.height / 2;
      tip.style.left = `${Math.max(0, Math.min(parent.width - tip.offsetWidth, px - parent.left + 10))}px`;
      tip.style.top = `${py - parent.top + 10}px`;
    };
    arc.addEventListener("mousemove", show);
    arc.addEventListener("focus", show);
    arc.addEventListener("click", show);
    arc.addEventListener("mouseleave", () => (tip.hidden = true));
    arc.addEventListener("blur", () => (tip.hidden = true));
    svg.append(arc);
    start += len;
  }
  const label = (y, cls, text) => {
    const node = document.createElementNS(NS, "text");
    node.setAttribute("x", 80);
    node.setAttribute("y", y);
    node.setAttribute("text-anchor", "middle");
    node.setAttribute("class", cls);
    node.textContent = text;
    svg.append(node);
  };
  label(76, "donut-caption", t("合计"));
  label(96, "donut-total", total);
  box.append(svg);
}

function renderSpending(sp) {
  if (!sp) return;
  const money2 = (n) => amount(n, sp.currency);
  // 概览：一行
  const has = sp.services.length > 0 || sp.total > 0;
  $("spend-card").hidden = !has;
  $("spend-line").textContent = sp.limit
    ? t("本月约 {total} / 上限 {limit}", { total: money2(sp.total), limit: money2(sp.limit) })
    : t("本月约 {total}", { total: money2(sp.total) });
  setMeter("month-meter", sp.ratio);
  $("spend-note").textContent = sp.excluded.length ? t("另有 {n} 个未计入", { n: sp.excluded.length }) : "";

  // 用量页：各服务的份额。颜色跟着服务走（按表里的固定顺序），不跟排名
  const slots = new Map(sp.services.map((x, i) => [x.key, i]));
  const paid = sp.services.filter((x) => x.cost > 0).sort((a, b) => b.cost - a.cost);
  const main = paid.filter((x) => slots.get(x.key) < SERIES_SLOTS);
  const rest = paid.filter((x) => slots.get(x.key) >= SERIES_SLOTS);
  const segs = main.map((x) => ({ name: tr(x.name), cost: x.cost, costText: money2(x.cost), share: x.share, color: `var(--series-${slots.get(x.key) + 1})` }));
  if (rest.length) {
    segs.push({
      name: t("其他"),
      cost: rest.reduce((a, x) => a + x.cost, 0),
      costText: money2(rest.reduce((a, x) => a + x.cost, 0)),
      share: rest.reduce((a, x) => a + x.share, 0),
      color: "var(--muted)",
    });
  }
  $("spend-summary").textContent = sp.limit
    ? t("合计约 {total}，上限 {limit}", { total: money2(sp.total), limit: money2(sp.limit) })
    : t("合计约 {total}，没有设上限", { total: money2(sp.total) });
  renderSpendDonut(segs, money2(sp.total));
  $("spend-legend").replaceChildren(
    ...segs.map((x) =>
      el("li", {}, (() => {
        const s = el("i", { class: "spend-swatch" });
        s.style.background = x.color;
        return s;
      })(), el("span", {}, x.name), el("span", { class: "muted" }, `${money2(x.cost)} · ${Math.round(x.share * 100)}%`)),
    ),
  );
  const rows = [...sp.services]
    .sort((a, b) => b.cost - a.cost || b.calls - a.calls)
    .map((x) =>
      el(
        "tr",
        {},
        el("td", {}, el("strong", {}, tr(x.name)), x.note ? el("div", { class: "muted small" }, tr(x.note)) : null),
        el("td", { class: "num" }, x.calls),
        el("td", { class: "num" }, x.free_quota ? `${x.free_used} / ${x.free_quota}` : "–"),
        el("td", { class: "num" }, money2(x.cost)),
        el("td", { class: "num" }, `${Math.round(x.share * 100)}%`),
      ),
    );
  if (rows.length) foldInto($("spend-body"), "spend", rows, { colspan: 5 });
  else $("spend-body").replaceChildren(emptyRow(5, t("本月还没有付费服务的调用")));
  const over = $("spend-over");
  over.hidden = !sp.over;
  over.textContent = sp.blocked.length
    ? t("已达到上限，本月暂停提供：{list}。DeepSeek 按费用上限的方式处理，下个月自动恢复", { list: joinList(sp.blocked) })
    : t("已达到上限。DeepSeek 按费用上限的方式处理，下个月自动恢复");
  const ex = $("spend-excluded");
  ex.hidden = !sp.excluded.length;
  ex.textContent = t("未计入（不知道价格，或汇率是 0）：{list}。这些服务不算进合计，超出上限时也不会被拿掉。在设置页的搜索服务里填上单价就会计入", {
    list: joinList(sp.excluded.map((x) => (x.calls ? `${tr(x.name)}（${x.calls}）` : tr(x.name)))),
  });
}

// 搜索结果和请求相关的设置
const NUMBER_SETTINGS = ["default_count", "max_count", "min_rerank_score", "content_chars_per_result", "max_search_chars", "max_fetch_chars", "cache_ttl_seconds", "request_timeout", "max_retries", "qps_limit"];
const CHECK_SETTINGS = ["include_content", "send_original_question"];

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
  for (const name of NUMBER_SETTINGS) form[name].value = st[name];
  for (const name of CHECK_SETTINGS) form[name].checked = st[name];
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
    ...Object.fromEntries(NUMBER_SETTINGS.map((name) => [name, Number(form[name].value)])),
    ...Object.fromEntries(CHECK_SETTINGS.map((name) => [name, form[name].checked])),
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
  const capRows = [];
  for (const r of rows) {
    const full = r.used >= r.limit;
    capRows.push(
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
  if (capRows.length) foldInto(body, "caps", capRows, { colspan: 4 });
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

  // DeepSeek 的计费次数加上其他工具（调用日志）
  const count = (d, kind) => d[kind].billable + (d[`other_${kind}`] || 0);
  const totals = days.map((d) => count(d, "search") + count(d, "fetch"));
  const max = Math.max(...totals, 0);
  const sum = totals.reduce((a, b) => a + b, 0);
  const st = lastStatus?.settings;
  const scope = st && !st.call_log_enabled ? t("调用日志已关闭，只统计 DeepSeek") : st ? t("其他工具来自调用日志，只保留 {days} 天", { days: st.call_log_retention_days }) : "";
  $("chart-summary").textContent = [t("30 天合计 {sum} 次", { sum }), scope].filter(Boolean).join(" · ");
  svg.setAttribute("aria-label", t("近 30 天 {sum} 次", { sum }));

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
    text(W / 2, H / 2, t("还没有调用"), "empty", "middle");
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
    tip.textContent = t("{date}  搜索 {search} · 抓取 {fetch}", { date: d.date, search: count(d, "search"), fetch: count(d, "fetch") });
    g.append(tip);
    for (const kind of ["search", "fetch"]) {
      const v = count(d, kind);
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

// 这类问题模型用什么，出问题时会怎样。内容都来自真实路由（web_api.route_preview）
let lastRouting = null;
let routeState = "ok";
const ROUTE_STATES = [
  ["ok", "正常"],
  ["down", "DeepSeek 故障"],
  ["budget", "超出上限"],
];

function routeChip(item, cls = "") {
  return serviceChip(item, { cls, names: $("route-names").checked });
}

function routeRow(row) {
  const flow = el("div", { class: "route-flow" });
  for (const item of row.first) flow.append(routeChip(item, row.failed ? "failed" : ""));
  if (row.failed) flow.append(el("span", { class: "small bad-text" }, t("调用失败")));
  if (row.advice.length) {
    flow.append(el("span", { class: "muted" }, "→"));
    for (const item of row.advice) flow.append(routeChip(item));
  }
  const out = el("div", { class: "route-row" }, el("div", { class: "route-title" }, tr(row.plain)), flow);
  if (row.rest.length) {
    const rest = el("div", { class: "chips route-rest" });
    foldInto(rest, `route:${routeState}:${row.key}`, row.rest.map((x) => routeChip(x)));
    out.append(el("div", { class: "route-backups" }, el("span", { class: "muted small" }, t("备用")), rest));
  }
  return out;
}

function routeNote(st, data) {
  if (routeState === "ok") return t("模型按下面的顺序用，不理想时往后换。");
  const name = t(routeState === "down" ? "DeepSeek 故障" : "超出上限");
  const list = joinList(st.advice.map((x) => tr(x.label)));
  const parts = [];
  if (st.effect === "no_own") parts.push(t("现在没有用到 DeepSeek，{state}对它没有影响。", { state: name }));
  if (st.effect === "advice") parts.push(t("{mode}模式不隐藏工具；{state}时模型会收到改用 {list} 的建议。", { mode: t(MODE_TEXT[data.mode] || data.mode), state: name, list }));
  if (st.effect === "no_backup") parts.push(t("{state}时没有可用的备用工具，模型会告诉用户联网搜索暂时不可用。", { state: name }));
  if (st.effect === "swap") parts.push(list ? t("{state}时把 DeepSeek 从请求里拿掉，改用 {list}。", { state: name, list }) : t("{state}时把 DeepSeek 从请求里拿掉。", { state: name }));
  if (st.removed.length) parts.push(t("这些付费工具不再提供：{list}。", { list: joinList(st.removed.map((x) => tr(x.label))) }));
  if (st.gone?.length) parts.push(t("这几类没有专门的工具了，交给一般问题的工具：{list}。", { list: joinList(st.gone.map((x) => tr(x))) }));
  if (!st.rows.length && !st.removed.length && !st.gone?.length && st.effect !== "advice") parts.push(t("其余都不变。"));
  else if (st.unchanged) parts.push(t("下面只列有变化的，其余 {n} 类不变。", { n: st.unchanged }));
  return parts.join(" ");
}

function renderRouting(data) {
  lastRouting = data;
  const st = data.states[routeState];
  prefChips($("route-states"), ROUTE_STATES, routeState, (key) => {
    routeState = key;
    renderRouting(lastRouting);
  });
  $("route-note").textContent = routeNote(st, data);
  const box = $("route-rows");
  if (st.rows.length) foldInto(box, `route:${routeState}`, st.rows.map(routeRow));
  else if (routeState === "ok") box.replaceChildren(el("p", { class: "muted small" }, t("没有读取到搜索类工具")));
  else box.replaceChildren();
  const missing = routeState === "ok" ? missingLine(st.missing) : "";
  $("route-missing").hidden = !missing;
  $("route-missing").textContent = missing;
  $("route-others").textContent = data.other_tools ? t("另有 {n} 个非搜索工具不受影响", { n: data.other_tools }) : "";
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
          ["连续失败后暂停", d.error.circuit_opened ? t("是") : ""],
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
        ["第一条结果", call.top_hit],
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
          ? t("未记录搜索词和结果：设置里关闭了「记录搜索词、链接、DeepSeek 的返回结果，以及各搜索工具第一条结果的简述」。")
          : t("未记录参数和结果：默认只记录其他工具的名称、耗时和状态。需要时在「设置 → 高级设置」中开启「记录其他工具的完整参数和结果」，之后的调用会保存完整内容。"),
      ),
    );
  }
  const raw = el("details", {}, el("summary", { class: "muted small" }, t("原始记录（JSON）")));
  raw.append(pre(call));
  grid.append(raw);
  return grid;
}

// 说明列：出错时显示原因，否则显示第一条结果的简述。请求 ID 在展开后的详情里
function callNoteCell(it) {
  const text = it.error ? tr(it.error) : it.top_hit || tr(it.note) || "";
  return el("td", { class: "target" }, el("span", { class: "call-note", title: text }, text));
}

function renderCalls(items) {
  const body = $("calls-body");
  body.replaceChildren();
  if (!items.length) {
    body.append(emptyRow(6, t("没有符合条件的调用记录")));
    return;
  }
  const rows = [];
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
      callNoteCell(it),
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
    rows.push([main, detail]);
    if (openRows.has(key)) toggle(true);
  }
  foldInto(body, `calls:${callPage}`, rows, { first: 10, colspan: 6 });
}

// 换了筛选条件就回到只显示前 10 行
function resetCallsFold() {
  for (const key of [...foldState.keys()]) if (key.startsWith("calls:")) foldState.delete(key);
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
  resetCallsFold();
  reloadCalls();
});
for (const name of ["range", "source", "tool", "status"]) {
  callFilters.elements[name].addEventListener("change", () => {
    callPage = 0;
    resetCallsFold();
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
  const rows = [];
  for (const r of data.rows) {
    const [label, cls] = CATEGORY[r.category] || [r.category, ""];
    const answered = r.total - r.blocked;
    const success = answered > 0 ? `${Math.round(((r.ok + r.empty + r.cache_hit) / answered) * 100)}%` : "–";
    rows.push(
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
  if (rows.length) foldInto(body, "stats", rows, { colspan: 10 });
  $("stats-note").textContent = t("合计 {n} 次 · 数据来自调用日志；成功率不含被拦截的调用。“健康”按近 24 小时判断：失败、空结果、结果与搜索词不相关都算有问题，鼠标悬停看原因", { n: data.total });
  const burst = data.consecutive_search;
  if (burst?.searches) {
    $("stats-note").textContent += t(" 连续搜索率 {rate}：{n} 次搜索里 {followed} 次之后 3 分钟内又搜索，其中 {same} 次是同一条回复里一起发出的，不多花 LLM 调用。", {
      rate: `${Math.round(burst.rate * 100)}%`,
      n: burst.searches,
      followed: burst.followed,
      same: burst.same_round,
    });
  }
}

async function loadSearchStats() {
  renderSearchStats(await bridge.apiGet("search-stats", { range: $("stats-range").value }));
}
$("stats-range").addEventListener("change", () => loadSearchStats().catch((err) => toast(tr(err.message), true)));

// ---------- 主力搜索工具 ----------

let lastOverview = null;

const SETUP = {
  missing: ["缺少配置", "bad"],
  warn: ["建议处理", "warn"],
  info: ["说明", ""],
  ok: ["已配置", "ok"],
};

// 一个服务：灯、名字。工具名和近 24 小时的调用次数在悬停提示里
const LAMP_TEXT = { ok: "可用", warn: "要留意", bad: "有问题" };

function callsText(calls) {
  return calls ? t("近 24 小时调用 {n} 次", { n: calls }) : t("近 24 小时没有调用");
}

// 服务商图标，见 icons.js；没有图标的服务显示名字的首字母
let logoSeq = 0;

function serviceLogo(item, lamp) {
  const logo = (window.SERVICE_LOGOS || {})[item.service];
  const box = el("span", { class: `svc-logo${logo ? "" : " mono"}`, "data-service": item.service || "", "aria-hidden": "true" });
  // 同一个图标会出现多次，渐变的 id 要各用各的：引用到隐藏页面里的那一个时渐变会失效
  if (logo) box.innerHTML = logo.replace(/\bid="([^"]+)"/g, `id="$1-${++logoSeq}"`).replace(/url\(#([^)]+)\)/g, `url(#$1-${logoSeq})`);
  else box.textContent = (tr(item.label) || "?").slice(0, 1).toUpperCase();
  if (lamp) box.append(el("i", { class: `hdot ${lamp}` }));
  return box;
}

function serviceChip(item, { cls = "", names = false } = {}) {
  const title = [t(LAMP_TEXT[item.lamp]), callsText(item.calls), ...item.tools].join("\n");
  const chip = el("span", { class: `route-chip ${cls}`, title }, serviceLogo(item, item.lamp), el("span", {}, tr(item.label)));
  if (names) chip.append(el("code", {}, item.tools.length > 2 ? t("{list} 等 {n} 个", { list: joinList(item.tools.slice(0, 2)), n: item.tools.length }) : joinList(item.tools)));
  return chip;
}

function toolNames(lanes) {
  const box = el("div", { class: "tool-names" });
  for (const lane of lanes) {
    const tools = [...lane.first, ...lane.rest].flatMap((x) => x.tools);
    box.append(el("div", {}, lanes.length > 1 ? el("span", { class: "muted small" }, `${tr(lane.plain)}：`) : null, ...tools.map((n) => el("code", {}, n))));
  }
  return el("details", { class: "lane-fold" }, el("summary", { class: "muted small" }, t("工具名")), box);
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
  // 快速开始显示着的时候，“没有 DeepSeek Key、不能联网搜索”就不用再说一遍
  const quick = lastOverview?.search_ready === false;
  const items = setup.items.filter((it) => !(quick && it.key === "deepseek" && it.status === "missing"));
  const problems = items.filter((it) => (it.status === "missing" || it.status === "warn") && !it.ignored);
  const rest = items.filter((it) => !problems.includes(it) && !it.ignored);
  const missing = problems.filter((it) => it.status === "missing").length;
  const warn = problems.length - missing;
  box.hidden = !problems.length && !rest.length;
  box.className = `setup-box ${missing ? "bad" : problems.length ? "warn" : "ok"}`;
  if (problems.length) {
    const parts = [];
    if (missing) parts.push(t("{n} 项缺少配置", { n: missing }));
    if (warn) parts.push(t("{n} 项建议处理", { n: warn }));
    const ul = el("ul");
    foldInto(ul, "setup:problems", problems.map(setupRow));
    box.append(el("strong", {}, t("配置检查：{parts}", { parts: parts.join(I18N.getLang() === "en" ? ", " : "，") })), ul);
  }
  if (rest.length) {
    const ul = el("ul");
    foldInto(ul, "setup:rest", rest.map(setupRow));
    box.append(
      el(
        "details",
        {},
        el("summary", { class: problems.length || quick ? "muted small" : "" }, problems.length || quick ? t("其余 {n} 项", { n: rest.length }) : t("✓ 配置检查：搜索工具都已配置好（{n} 项）", { n: rest.length })),
        ul,
      ),
    );
  }
}

// 没有可用的通用搜索时才出现：选服务、填 Key、测试。配好后自己收起来
let qsServices = [];

function renderQuickstart(data) {
  const box = $("quickstart");
  box.hidden = data.search_ready !== false;
  if (box.hidden) return;
  qsServices = data.key_services || [];
  const sel = $("qs-service");
  const current = sel.value;
  sel.replaceChildren(...qsServices.map((x) => el("option", { value: x.service }, tr(x.name))));
  if (qsServices.some((x) => x.service === current)) sel.value = current;
  renderQuickstartInfo();
}

function renderQuickstartInfo() {
  const x = qsServices.find((s) => s.service === $("qs-service").value);
  if (!x) return;
  $("qs-info").replaceChildren(
    el("div", {}, `${tr(x.strengths)} · ${tr(x.cost)}`),
    x.signup_url ? signupLine(x.signup_url) : "",
  );
}

// 面板的沙箱打不开新窗口，也不一定能用剪贴板，所以只显示地址，点一下全选好手动复制
function signupLine(url) {
  return el("div", { class: "signup" }, t("注册地址："), el("code", { class: "selectable" }, url));
}

async function reloadAfterKey() {
  loaded.delete("tools");
  const tasks = [loadStatus(), loadOverview()];
  if (currentPage === "settings" || lastCatalog) tasks.push(loadSearchTools());
  await Promise.allSettled(tasks);
}

async function saveKey(service, input, enable = false) {
  const r = await bridge.apiPost("keys", { service, key: input.value, enable });
  input.value = "";
  if (r.error) throw new Error(r.error);
  toast(r.changed === false ? t("没有填写，已保存的 Key 不变") : t("Key 已保存"));
  await reloadAfterKey();
}

async function testService(service, show) {
  const r = await bridge.apiPost("test", { service });
  const text = r.ok ? tr(r.message) : t("连接失败：{error}", { error: tr(r.message) });
  show(text, !r.ok);
  await reloadAfterKey();
}

function renderModels(models) {
  const box = $("models-body");
  box.replaceChildren();
  // 只有真的检测到时才显示这一节
  $("sec-models").hidden = !models?.length;
  if (!models?.length) return;
  const list = el("ul", { class: "models-list" });
  for (const m of models) {
    const [label, cls] = !m.enabled ? ["未启用", ""] : m.tools_broken ? ["可能冲突", "warn"] : m.is_default ? ["默认在用", "accent"] : ["已启用", ""];
    const notes = [];
    if (m.tools_broken) notes.push(t("按 AstrBot 的说明，原生工具和函数工具同时使用只支持 Gemini 3 及以上模型，其他模型上 DeepDeep 和其他搜索工具可能都调用不了"));
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
  renderBanner();
  renderQuickstart(data);
  renderSetup(data.setup);
  renderHandled(data);
  renderModels(data.model_search);
  const box = $("lanes");
  box.replaceChildren();
  if (!data.lanes.length) {
    box.append(el("p", { class: "muted small" }, t("没有读取到搜索类工具")));
  }
  // 每一类自己有灯，备用服务也直接看得到；工具名才折叠
  const main = data.lanes.filter((l) => l.group !== "special");
  const special = data.lanes.filter((l) => l.group === "special");
  for (const lane of main) {
    const item = lane.first[0];
    const card = el(
      "article",
      { class: "lane" },
      el(
        "div",
        { class: "lane-head" },
        el("i", { class: `hdot ${item.lamp}` }),
        el("div", { class: "lane-plain" }, el("strong", {}, tr(lane.plain)), el("span", { class: "muted" }, " → "), serviceLogo(item), el("strong", {}, tr(item.label))),
        el("span", { class: `lane-state small lamp-${item.lamp}` }, t(LAMP_TEXT[item.lamp])),
      ),
      el("div", { class: "muted small" }, callsText(item.calls)),
    );
    if (lane.rest.length) {
      const list = el("div", { class: "chips" });
      foldInto(list, `lane:${lane.key}`, lane.rest.map((x) => serviceChip(x)));
      card.append(el("div", { class: "route-backups" }, el("span", { class: "muted small" }, t("备用")), list));
    }
    card.append(toolNames([lane]));
    box.append(card);
  }
  if (special.length || data.missing_categories?.length) {
    const items = special.flatMap((l) => [...l.first, ...l.rest]);
    const worst = items.some((x) => x.lamp === "bad") ? "bad" : items.some((x) => x.lamp === "warn") ? "warn" : "ok";
    const card = el("article", { class: "lane lane-special" });
    card.append(
      el(
        "div",
        { class: "lane-head" },
        special.length ? el("i", { class: `hdot ${worst}` }) : null,
        el("div", { class: "lane-plain" }, el("strong", {}, t("专用搜索"))),
        special.length ? el("span", { class: `lane-state small lamp-${worst}` }, t(LAMP_TEXT[worst])) : null,
      ),
    );
    const rows = el("div", { class: "spec-rows" });
    foldInto(
      rows,
      "lane:special",
      special.map((lane) => {
        const chips = el("div", { class: "chips" });
        foldInto(chips, `lane:${lane.key}`, [...lane.first, ...lane.rest].map((x) => serviceChip(x)));
        return el("div", { class: "spec-row" }, el("div", { class: "spec-title" }, tr(lane.plain)), chips);
      }),
    );
    card.append(rows);
    const missing = missingLine(data.missing_categories);
    if (missing) card.append(el("p", { class: "muted small" }, missing));
    if (special.length) card.append(toolNames(special));
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
    const rows = [];
    for (const [name, h] of flagged.sort((a, b) => (a[1].verdict === "broken" ? 0 : 1) - (b[1].verdict === "broken" ? 0 : 1))) {
      rows.push(
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
    foldInto(ul, "health-alert", rows);
    alert.append(ul);
  }
  if (lastSearchStats) renderSearchStats(lastSearchStats, data.health);
}

function missingLine(titles) {
  if (!titles?.length) return "";
  return t("{list} 没有专门的工具，由一般问题的工具负责。有这类工具的话，在高级设置的搜索分工规则里加一行就能认出来", { list: joinList(titles.map((x) => tr(x))) });
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
    const rows = [];
    for (const d of disabled) {
      rows.push(
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
    foldInto(ul, "handled:disabled", rows);
    body.append(ul);
  }
  if (ignored.length) {
    body.append(el("h3", {}, t("已忽略的提醒（问题变化、或恢复后再出问题时会重新提醒）")));
    const ul = el("ul");
    const rows = [];
    for (const it of ignored) {
      rows.push(
        el(
          "li",
          {},
          el("strong", {}, it.title),
          el("span", { class: "muted small" }, ` ${it.detail}`),
          el("span", { class: "row-actions" }, actionBtn(t("恢复提醒"), () => overviewAction("unignore", { key: it.key }, t("已恢复提醒")))),
        ),
      );
    }
    foldInto(ul, "handled:ignored", rows);
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
  const netRows = [];
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
    netRows.push(
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
  if (netRows.length) foldInto(body, "network", netRows, { colspan: 5 });

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
      el("div", { class: "muted small" }, t(check.proxy ? "服务器网络（{time} 检测）· 请求走 AstrBot 设置的代理" : "服务器网络（{time} 检测）", { time: new Date(check.checked_at * 1000).toLocaleString() })),
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

const CATALOG_CLS = { active: "accent", ok: "ok", unstable: "warn", missing: "bad", paused: "warn" };
let lastCatalog = null;

function serviceSwitch(e) {
  const box = el("input", { type: "checkbox" });
  const shared = form[e.enabled_field];
  box.checked = shared ? shared.checked : e.enabled;
  box.addEventListener("change", (ev) => {
    if (shared) {
      // 冒泡到表单后按原来的方式自动保存
      shared.checked = box.checked;
      return;
    }
    // 表单里没有的开关（Perplexity 等）：单独保存，只发这一项
    ev.stopPropagation();
    bridge
      .apiPost("settings", { [e.enabled_field]: box.checked })
      .then(reloadAfterKey)
      .catch((err) => toast(tr(err?.message || String(err)), true));
  });
  return el("label", { class: "service-switch" }, box, el("span", {}, t("启用")));
}

function keyBox(e) {
  const k = e.key_input;
  const input = el("input", { type: "password", autocomplete: "new-password", spellcheck: "false", placeholder: k.masked ? t("已保存，填新的会替换") : t("粘贴 Key"), "aria-label": `${tr(e.name)} Key` });
  // 不走表单的自动保存
  for (const type of ["input", "change"]) input.addEventListener(type, (ev) => ev.stopPropagation());
  const result = el("span", { class: "small", role: "status" });
  const save = el("button", { type: "button", class: "btn" }, t("保存"));
  const onError = (err) => toast(tr(err?.message || String(err)), true);
  save.addEventListener("click", (ev) => withBusy(ev.currentTarget, () => saveKey(k.service, input).catch(onError)));
  input.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    save.click();
  });
  const test = el("button", { type: "button", class: "btn ghost" }, t("测试"));
  test.addEventListener("click", (ev) =>
    withBusy(ev.currentTarget, () =>
      testService(k.service, (text, bad) => {
        toast(text, bad);
      }).catch(onError),
    ),
  );
  const clear = k.own
    ? actionBtn(t("清除"), () => bridge.apiPost("keys", { service: k.service, clear: true }).then(() => {
        toast(t("Key 已清除"));
        return reloadAfterKey();
      }), { confirmText: t("确定清除这个 Key？"), danger: true })
    : null;
  return el(
    "div",
    { class: "key-box" },
    el("div", { class: "key-row" }, input, save, k.masked ? test : null, clear),
    k.masked ? el("div", { class: "muted small" }, t("当前 Key：{masked}（{source}）", { masked: k.masked, source: tr(k.source) })) : null,
    result,
  );
}

// 表里没有价格的服务：自己填每千次价格，才计入每月花费、超出上限时才会被拿掉
function priceBox(e) {
  const p = e.price;
  const notCounted = lastStatus?.spending?.excluded?.some((x) => x.key === p.key);
  if (p.amount === null && !e.tools?.length && !notCounted) return null;
  const input = el("input", { type: "number", min: "0", step: "0.01", "aria-label": t("每千次价格") });
  if (p.amount !== null) input.value = p.amount;
  const hint = el("div", { class: "muted small" }, p.amount === null ? t("还没填单价：不计入每月花费，超出上限时也不会被拿掉") : t("已计入每月花费，超出上限时会被拿掉；填 0 表示不花钱，清空就是不计入"));
  const save = () => {
    const rules = (lastStatus?.settings?.custom_prices || []).filter((line) => line.split("=")[0].trim() !== p.key);
    const raw = input.value.trim();
    if (raw !== "") rules.push(`${p.key} = ${Number(raw)} ${p.currency}`);
    bridge
      .apiPost("settings", { custom_prices: rules })
      .then((status) => {
        renderStatus(status);
        toast(t("单价已保存"));
        return reloadAfterKey();
      })
      .catch((err) => toast(tr(err?.message || String(err)), true));
  };
  // 不走表单的自动保存
  input.addEventListener("input", (ev) => ev.stopPropagation());
  input.addEventListener("change", (ev) => {
    ev.stopPropagation();
    if (input.checkValidity()) save();
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      input.blur();
    }
  });
  return el("label", { class: "price-box" }, el("span", { class: "small" }, I18N.getLang() === "en" ? `${t("每千次价格")} (${SYMBOLS[p.currency]})` : `${t("每千次价格")}（${SYMBOLS[p.currency]}）`), input, hint);
}

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
      e.untested ? el("span", { class: "badge warn untested" }, t("未实测")) : null,
      e.strengths ? el("div", { class: "why" }, tr(e.strengths)) : null,
      meta ? el("div", { class: "meta" }, meta) : null,
      e.signup_url ? signupLine(e.signup_url) : null,
      e.enabled_field ? serviceSwitch(e) : null,
      e.key_input ? keyBox(e) : null,
      e.price ? priceBox(e) : null,
    ),
    el("span", { class: `badge ${CATALOG_CLS[e.status] || ""}` }, tr(e.status_label)),
  );
}

// 现在模型能用、或者正在用的
const CATALOG_IN_USE = new Set(["active", "ok", "unstable", "paused"]);
const CATALOG_FILTERS = [
  ["all", "全部", () => true],
  ["enabled", "已启用", (e) => CATALOG_IN_USE.has(e.status)],
  ["free", "免费", (e) => e.free || e.free_quota],
  ["paid", "付费", (e) => e.free === false],
  ["domestic", "国内", (e) => e.region === "domestic"],
  ["foreign", "国外", (e) => e.region === "foreign"],
];
let catalogFilter = "all";

function renderCatalog(list) {
  lastCatalog = list;
  prefChips(
    $("catalog-filters"),
    CATALOG_FILTERS.map(([key, label, pass]) => [key, `${t(label)} ${list.filter(pass).length}`]),
    catalogFilter,
    (key) => {
      catalogFilter = key;
      renderCatalog(lastCatalog);
    },
  );
  const pass = CATALOG_FILTERS.find(([key]) => key === catalogFilter)[2];
  // 启用的排在前面，其余保持原来的顺序
  const rows = list.filter(pass);
  const sorted = [...rows.filter((e) => CATALOG_IN_USE.has(e.status)), ...rows.filter((e) => !CATALOG_IN_USE.has(e.status))];
  if (sorted.length) foldInto($("catalog-list"), `catalog:${catalogFilter}`, sorted.map(catalogRow));
  else $("catalog-list").replaceChildren(el("li", { class: "muted small" }, t("这一类现在没有服务")));
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
  renderRouting((await bridge.apiGet("tools", params)).scenarios);
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

$("route-names").addEventListener("change", () => lastRouting && renderRouting(lastRouting));
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

// 顶部的 Star / Issue 链接。AstrBot 的面板沙箱不允许弹出新窗口，打不开时显示地址让用户复制；
// 以后沙箱放开了，这里会自动直接打开
for (const a of document.querySelectorAll("a.head-link")) {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    let win = null;
    try {
      win = window.open(a.href, "_blank");
    } catch {
      win = null;
    }
    const hint = $("link-hint");
    if (win) {
      hint.hidden = true;
      return;
    }
    hint.replaceChildren(el("span", { class: "small" }, t("AstrBot 的面板不允许直接打开外部网页，请复制下面的地址到浏览器打开：")), urlLine(a.href));
    hint.hidden = false;
  });
}
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
$("qs-service").addEventListener("change", renderQuickstartInfo);
$("qs-save").addEventListener("click", (e) =>
  withBusy(e.currentTarget, () => saveKey($("qs-service").value, $("qs-key"), true).catch((err) => toast(tr(err?.message || String(err)), true))),
);
$("qs-test").addEventListener("click", (e) =>
  withBusy(e.currentTarget, () =>
    testService($("qs-service").value, (text, bad) => {
      $("qs-result").textContent = text;
      $("qs-result").className = `small ${bad ? "bad-text" : "ok-text"}`;
    }).catch((err) => toast(tr(err?.message || String(err)), true)),
  ),
);
$("qs-more").addEventListener("click", () => showPage("settings"));
$("btn-refresh").addEventListener("click", (e) => withBusy(e.currentTarget, refreshAll));
$("btn-reset").addEventListener("click", (e) =>
  withBusy(e.currentTarget, async () => {
    renderStatus(await bridge.apiPost("reset-circuit", {}));
    toast(t("已解除暂停"));
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
