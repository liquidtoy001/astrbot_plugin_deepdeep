// DeepDeep · 插件 WebUI
// 所有用户相关内容（搜索词、链接、错误信息）都用 textContent 渲染，避免 XSS。

// AstrBot 4.29 起叫 AstrBotPluginView，4.26–4.28 叫 AstrBotPluginPage（4.29 仍保留这个别名）
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
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
  const t = $("toast");
  t.textContent = message;
  t.className = bad ? "toast bad" : "toast";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 6000);
}

async function withBusy(button, fn) {
  button.disabled = true;
  try {
    return await fn();
  } catch (err) {
    toast(err?.message || String(err), true);
    return undefined;
  } finally {
    button.disabled = false;
  }
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
  let title = "DeepSeek 搜索正常";
  let detail = `路由模式：${MODE_TEXT[mode] || mode}`;
  if (mode === "fallback_only") {
    level = "warn";
    title = "已手动暂停 DeepSeek，正在使用其他搜索工具";
  } else if (!s.available) {
    level = s.unavailable_code === "no_key" ? "bad" : "warn";
    title = UNAVAILABLE_TITLES[s.unavailable_code] || "DeepSeek 暂不可用";
    detail = `${s.unavailable_message}。${
      mode === "deepseek_only"
        ? "当前为“只用 DeepSeek”模式，不会降级。"
        : mode === "guide_only"
          ? "模型调用 DeepDeep 时会收到改用其他搜索工具的建议。"
          : "已自动降级到其他搜索工具。"
    }`;
  } else if (s.budget.warning) {
    level = "warn";
    title = "DeepSeek 搜索正常，费用接近上限";
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
  $("today-limit").textContent = b.daily_limit ? `上限 ${money(b.daily_limit)}` : "不限制";
  $("month-limit").textContent = b.monthly_limit ? `上限 ${money(b.monthly_limit)}` : "不限制";
  setMeter("today-meter", b.daily_ratio);
  setMeter("month-meter", b.monthly_ratio);

  for (const kind of ["search", "fetch"]) {
    const c = s.today[kind];
    $(`today-${kind}`).textContent = `${c.billable} 次`;
    $(`today-${kind}-detail`).textContent = `缓存 ${c.cache_hit} · 无结果 ${c.empty} · 失败 ${c.failed}`;
  }

  const k = s.key;
  $("key-info").textContent = k.configured ? `Key：${k.masked}（${k.source}）` : "未找到 API Key";

  if (!formDirty) fillForm(s.settings);
  // 汇率可能刚加载或刚修改，图表里的人民币金额跟着刷新
  if (usageDays.length) renderChart(usageDays);
}

function fillForm(st) {
  for (const input of form.querySelectorAll('input[name="routing_mode"]')) {
    input.checked = input.value === st.routing_mode;
  }
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

// 输入费用上限时实时显示对应的人民币金额（按表单里的汇率）
function updateBudgetHints() {
  // 跟随表单里尚未保存的币种和汇率
  const code = form.billing_currency.value || "cny";
  const r = Number(form.exchange_rate.value || 0);
  for (const node of document.querySelectorAll(".cur-symbol")) node.textContent = SYMBOLS[code];
  const hint = (value, base) => {
    const v = Number(value || 0);
    const other = v > 0 ? converted(v, code, r) : "";
    return other ? `约 ${other}，${base}` : base;
  };
  $("daily-hint").textContent = hint(form.daily_budget.value, "0 表示不限制");
  $("monthly-hint").textContent = hint(form.monthly_budget.value, "按自然月统计，0 表示不限制");
}

const lines = (text) => text.split("\n").map((s) => s.trim()).filter(Boolean);

function readForm() {
  return {
    routing_mode: form.querySelector('input[name="routing_mode"]:checked')?.value,
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

// ---------- 图表 ----------

function renderChart(days) {
  const box = $("chart");
  box.replaceChildren();
  const NS = "http://www.w3.org/2000/svg";
  // 按容器实际宽度绘制，避免拉伸导致文字变形
  const W = Math.max(280, Math.round(box.clientWidth || 720));
  const H = 180;
  const pad = { l: 28, r: 6, t: 8, b: 20 };
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("role", "img");

  const totals = days.map((d) => d.search.billable + d.fetch.billable);
  const max = Math.max(...totals, 0);
  const sum = totals.reduce((a, b) => a + b, 0);
  const cost = days.reduce((a, d) => a + d.cost, 0);
  $("chart-summary").textContent = `30 天合计 ${sum} 次，预估 ${money(cost)}`;
  svg.setAttribute("aria-label", `近 30 天计费 ${sum} 次`);

  const text = (x, y, content, cls, anchor = "start") => {
    const t = document.createElementNS(NS, "text");
    t.setAttribute("x", x);
    t.setAttribute("y", y);
    t.setAttribute("class", cls);
    t.setAttribute("text-anchor", anchor);
    t.textContent = content;
    svg.append(t);
  };

  if (max === 0) {
    text(W / 2, H / 2, "还没有计费调用", "empty", "middle");
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
    tip.textContent = `${d.date}  搜索 ${d.search.billable} · 抓取 ${d.fetch.billable} · ${money(d.cost)}`;
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
  return visible ? el("span", { class: "badge ok" }, "可见") : el("span", { class: "badge" }, "隐藏");
}

function renderTools(tools) {
  const body = $("tools-body");
  body.replaceChildren();
  if (!tools.length) {
    body.append(emptyRow(5, "没有读取到工具"));
    return;
  }
  const roles = {
    deepseek: ["DeepSeek", "accent"],
    fallback: ["备用搜索", "warn"],
    specialist: ["专用搜索", ""],
    other: ["其他", ""],
  };
  // 只列出和搜索路由有关的，其他工具折叠成一行统计
  const relevant = tools.filter((t) => t.role !== "other");
  const others = tools.length - relevant.length;
  for (const t of relevant) {
    const [label, cls] = roles[t.role];
    body.append(
      el(
        "tr",
        {},
        el("td", {}, el("code", {}, t.name)),
        el("td", {}, el("span", { class: `badge ${cls}` }, t.specialty ? `${label} · ${t.specialty}` : t.same_backend ? `${label} · DeepSeek 计费` : label)),
        el("td", {}, visibility(t.visible_when_ok)),
        el("td", {}, visibility(t.visible_when_down)),
        el("td", {}, visibility(t.visible_when_budget)),
      ),
    );
  }
  if (others) body.append(emptyRow(5, `另有 ${others} 个其他工具不受影响`));
}

// 展开状态按请求 ID 记住，30 秒自动刷新时不会收起
const openRows = new Set();

function kv(pairs) {
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of pairs) {
    if (v === undefined || v === null || v === "") continue;
    dl.append(el("dt", {}, k), el("dd", {}, typeof v === "object" ? JSON.stringify(v) : String(v)));
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

function renderDetail(it) {
  const d = it.detail || {};
  const grid = el("div", { class: "detail-grid" });

  grid.append(
    section(
      "概要",
      kv([
        ["请求 ID", it.request_id ? `#${it.request_id}（与工具结果第一行、日志一致）` : ""],
        ["时间", new Date(it.ts * 1000).toLocaleString()],
        ["结果", (OUTCOME[it.outcome] || CALL_STATUS[it.outcome] || [it.outcome])[0]],
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
        "请求参数",
        kv([
          ["搜索词", r.query],
          ["链接", r.url],
          ["结果数", r.count],
          ["附带正文", r.need_content === undefined ? "" : r.need_content ? "是" : "否"],
          ["用户原始问题", r.ori_question],
        ]),
      ),
    );
  }

  if (d.blocked) {
    grid.append(section("未调用的原因", kv([["代码", d.blocked.code], ["说明", d.blocked.message]])));
  }

  if (d.attempts?.length) {
    const rows = d.attempts.map((a) =>
      el(
        "tr",
        {},
        el("td", {}, a.attempt),
        el("td", {}, a.status || "网络错误"),
        el("td", { class: "num" }, `${a.latency_ms} ms`),
        el("td", {}, a.error || ""),
        el("td", {}, a.retry_in_s !== undefined ? `${a.retry_in_s}s 后重试` : ""),
      ),
    );
    grid.append(
      section(
        `请求过程（${d.attempts.length} 次）`,
        el(
          "table",
          { class: "mini" },
          el("thead", {}, el("tr", {}, ...["次", "状态", "耗时", "错误", "重试"].map((h) => el("th", {}, h)))),
          el("tbody", {}, ...rows),
        ),
      ),
    );
  }

  if (d.error) {
    grid.append(
      section(
        "错误",
        kv([
          ["HTTP 状态", d.error.status || "网络错误"],
          ["说明", d.error.message],
          ["DeepSeek 返回", d.error.detail],
          ["触发熔断", d.error.circuit_opened ? "是" : ""],
        ]),
      ),
    );
  }

  if (d.fallback) {
    const f = d.fallback;
    grid.append(
      section(
        "降级",
        kv([
          ["路由模式", MODE_TEXT[f.mode] || f.mode],
          ["放出的备用工具", f.restored_tools?.length ? f.restored_tools.join("、") : "无可用备用工具"],
          ["撤下 DeepSeek 工具", f.deepseek_tools_removed ? "是" : "否"],
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
        el("td", {}, el("div", {}, r.title || "(无标题)"), el("div", {}, safeLink(r.url))),
        el("td", { class: "num" }, (r.date || "").slice(0, 10) || "–"),
        el("td", { class: "num" }, Number(r.score ?? 0).toFixed(2)),
        el("td", { class: "num" }, r.authority >= 0 ? r.authority : "–"),
        el("td", {}, r.site || ""),
      ),
    );
    grid.append(
      section(
        "返回结果（相关度低于阈值的不会给模型；查询带 after:/before: 或问“最新”时不过滤）",
        el(
          "div",
          { class: "table-wrap" },
          el(
            "table",
            { class: "mini" },
            el("thead", {}, el("tr", {}, ...["#", "标题 / 链接", "日期", "相关度", "权威度", "来源"].map((h) => el("th", {}, h)))),
            el("tbody", {}, ...rows),
          ),
        ),
      ),
    );
  }

  if (d.page) {
    grid.append(
      section(
        "网页",
        kv([
          ["标题", d.page.title],
          ["正文字数", d.page.content_chars],
          ["是否截断", d.page.truncated ? "是（超过抓取字数上限）" : "否"],
        ]),
      ),
    );
  }

  const raw = el("details", {}, el("summary", { class: "muted small" }, "原始记录（JSON）"));
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
const callFilters = $("call-filters");

function sourceLabel(src) {
  if (!src) return "";
  if (src === "deepseek") return "DeepSeek";
  if (src === "builtin") return "AstrBot 内置";
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
      "概要",
      kv([
        ["记录 ID", call.id],
        ["请求 ID", call.request_id ? `#${call.request_id}（与工具结果第一行、日志一致）` : ""],
        ["时间", new Date(call.ts * 1000).toLocaleString()],
        ["工具", call.tool],
        ["来源", `${sourceLabel(call.source)}（${call.source}）`],
        ["会话", call.session],
        ["结果", label],
        ["HTTP 状态", call.http_status || ""],
        ["耗时", call.duration_ms !== null && call.duration_ms !== undefined ? `${call.duration_ms} ms` : ""],
        ["返回内容长度", call.result_chars !== null && call.result_chars !== undefined ? `${call.result_chars} 字` : ""],
        ["说明", call.note],
        ["错误", call.error],
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
    if (call.args !== null && call.args !== undefined) grid.append(section("调用参数", pre(call.args)));
    if (call.result) grid.append(section("返回结果", pre(call.result)));
  } else {
    grid.append(
      el(
        "p",
        { class: "muted small" },
        call.source === "deepseek"
          ? "未记录搜索词和结果：设置里关闭了「记录 DeepSeek 的搜索词、链接和返回结果」。"
          : "未记录参数和结果：默认只记录其他工具的名称、耗时和状态。需要时在上方设置中开启「记录其他工具的完整参数和结果」，之后的调用会保存完整内容。",
      ),
    );
  }
  const raw = el("details", {}, el("summary", { class: "muted small" }, "原始记录（JSON）"));
  raw.append(pre(call));
  grid.append(raw);
  return grid;
}

function renderCalls(items) {
  const body = $("calls-body");
  body.replaceChildren();
  if (!items.length) {
    body.append(emptyRow(6, "没有符合条件的调用记录"));
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
      el("td", {}, el("span", { class: `badge ${cls}` }, label + status)),
      el("td", { class: "num" }, it.duration_ms !== null && it.duration_ms !== undefined ? `${it.duration_ms} ms` : "–"),
      el("td", { class: "target" }, it.error || it.note || (it.request_id ? `#${it.request_id}` : "")),
    );
    const detail = el("tr", { class: "row-detail" }, el("td", { colspan: 6 }, el("span", { class: "muted small" }, "加载中…")));
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
          detail.firstChild.replaceChildren(el("span", { class: "muted small" }, `加载失败：${err.message || err}`));
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

async function loadCalls() {
  const data = await bridge.apiGet("calls", { ...currentCallFilters(), limit: PAGE_SIZE, offset: callPage * PAGE_SIZE });
  callTotal = data.total;
  renderCalls(data.items);
  const pages = Math.max(1, Math.ceil(callTotal / PAGE_SIZE));
  $("page-info").textContent = `第 ${callPage + 1} / ${pages} 页 · 共 ${callTotal} 条`;
  $("btn-prev").disabled = callPage === 0;
  $("btn-next").disabled = callPage + 1 >= pages;
  const s = data.summary;
  const oldest = s.oldest_ts ? new Date(s.oldest_ts * 1000).toLocaleDateString() : "–";
  $("calls-summary").textContent = `所有工具的调用记录 · 已保存 ${s.total} 条（最早 ${oldest}），保留 ${s.retention_days} 天 · 点击一行查看详情`;
}

async function loadFacets() {
  const f = await bridge.apiGet("call-facets");
  const fill = (name, values, label) => {
    const select = callFilters.elements[name];
    const current = select.value;
    select.replaceChildren(el("option", { value: "" }, label), ...values.map((v) => el("option", { value: v }, name === "source" ? sourceLabel(v) : v)));
    select.value = values.includes(current) ? current : "";
  };
  fill("source", f.sources, "全部来源");
  fill("tool", f.tools, "全部工具");
}

callFilters.addEventListener("submit", (e) => {
  e.preventDefault();
  callPage = 0;
  loadCalls().catch((err) => toast(err.message, true));
});
for (const name of ["range", "source", "tool", "status"]) {
  callFilters.elements[name].addEventListener("change", () => {
    callPage = 0;
    loadCalls().catch((err) => toast(err.message, true));
  });
}
$("btn-prev").addEventListener("click", () => {
  callPage = Math.max(0, callPage - 1);
  loadCalls().catch((err) => toast(err.message, true));
});
$("btn-next").addEventListener("click", () => {
  callPage += 1;
  loadCalls().catch((err) => toast(err.message, true));
});
$("btn-export").addEventListener("click", (e) =>
  withBusy(e.currentTarget, async () => {
    await bridge.download("call-export", currentCallFilters());
    toast("已导出当前筛选条件下的调用日志（JSON Lines）");
  }),
);
// 面板运行在受限 iframe 里，不能用 confirm()，改为两次点击确认
const clearBtn = $("btn-clear");
let clearTimer;
clearBtn.addEventListener("click", (e) => {
  if (!clearBtn.classList.contains("armed")) {
    clearBtn.classList.add("armed");
    clearBtn.textContent = "再点一次确认清空";
    clearTimer = setTimeout(() => {
      clearBtn.classList.remove("armed");
      clearBtn.textContent = "清空";
    }, 4000);
    return;
  }
  clearTimeout(clearTimer);
  clearBtn.classList.remove("armed");
  clearBtn.textContent = "清空";
  withBusy(e.currentTarget, async () => {
    const r = await bridge.apiPost("call-clear", {});
    callPage = 0;
    await Promise.allSettled([loadCalls(), loadFacets()]);
    toast(`已清空 ${r.removed} 条调用日志`);
  });
});

// ---------- 各搜索工具用量 ----------

const CATEGORY = {
  deepdeep: ["DeepDeep", "accent"],
  fallback: ["备用搜索", "warn"],
  special: ["专用搜索", ""],
};

function renderSearchStats(data) {
  const body = $("stats-body");
  body.replaceChildren();
  if (!data.rows.length) {
    body.append(emptyRow(9, data.logging_enabled ? "这段时间没有搜索类工具的调用" : "调用日志已关闭，无法统计"));
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
        el("td", {}, el("span", { class: `badge ${cls}` }, r.same_backend ? `${label} · DeepSeek 计费` : label)),
        el("td", { class: "num" }, r.total),
        el("td", { class: "num" }, success),
        el("td", { class: "num" }, r.error || "–"),
        el("td", { class: "num" }, r.blocked || "–"),
        el("td", { class: "num" }, r.avg_ms !== null && r.avg_ms !== undefined ? `${r.avg_ms} ms` : "–"),
        el("td", { class: "num" }, r.last_ts ? new Date(r.last_ts * 1000).toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "–"),
      ),
    );
  }
  $("stats-note").textContent = `合计 ${data.total} 次 · 数据来自调用日志，只统计本插件已启用的会话；成功率不含被拦截的调用`;
}

async function loadSearchStats() {
  renderSearchStats(await bridge.apiGet("search-stats", { range: $("stats-range").value }));
}
$("stats-range").addEventListener("change", () => loadSearchStats().catch((err) => toast(err.message, true)));

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

async function refreshAll() {
  const results = await Promise.allSettled([loadStatus(), loadUsage(), loadSearchStats(), loadCalls(), loadFacets(), loadTools(formDirty)]);
  const failed = results.find((r) => r.status === "rejected");
  if (failed) toast(`加载失败：${failed.reason?.message || failed.reason}`, true);
}

// ---------- 事件 ----------

form.addEventListener("change", updateBudgetHints);
form.addEventListener("input", () => {
  formDirty = true;
  updateBudgetHints();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const btn = form.querySelector('button[type="submit"]');
  await withBusy(btn, async () => {
    const status = await bridge.apiPost("settings", readForm());
    formDirty = false;
    renderStatus(status);
    await loadTools();
    toast("设置已保存并立即生效");
  });
});

$("btn-copy-repo").addEventListener("click", async () => {
  const text = $("repo-url").textContent;
  try {
    // 沙箱里剪贴板接口可能不可用，失败时选中文字让用户手动复制
    await navigator.clipboard.writeText(text);
    toast("已复制仓库链接，在浏览器打开即可点 Star");
  } catch {
    const range = document.createRange();
    range.selectNodeContents($("repo-url"));
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    toast("浏览器不允许自动复制，已选中链接，请按 Ctrl+C 复制");
  }
});
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
    toast("已复制诊断信息");
  } catch {
    toast("浏览器不允许自动复制，已选中全部内容，请按 Ctrl+C 复制");
  }
});
$("btn-preview").addEventListener("click", (e) => withBusy(e.currentTarget, () => loadTools(true)));
$("btn-refresh").addEventListener("click", (e) => withBusy(e.currentTarget, refreshAll));
$("btn-reset").addEventListener("click", (e) =>
  withBusy(e.currentTarget, async () => {
    renderStatus(await bridge.apiPost("reset-circuit", {}));
    toast("已解除熔断");
  }),
);
$("btn-test").addEventListener("click", (e) =>
  withBusy(e.currentTarget, async () => {
    const r = await bridge.apiPost("test", {});
    toast(r.ok ? `${r.message}${r.first_result ? `：${r.first_result}` : ""}` : `连接失败：${r.message}`, !r.ok);
    await Promise.allSettled([loadStatus(), loadCalls(), loadUsage()]);
  }),
);

await bridge.ready();
await refreshAll();
setInterval(() => {
  if (document.visibilityState === "visible") {
    // 只在第一页且没有展开详情时自动刷新，免得打断查看
    const tasks = [loadStatus()];
    if (callPage === 0 && !document.querySelector("tr.row-main.open")) tasks.push(loadCalls());
    Promise.allSettled(tasks);
  }
}, 30000);
