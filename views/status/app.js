// 双人格热切换 · 面板（主控 + 设置）
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

let CURRENT = {};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
const badge = (t, tone) => `<span class="badge ${tone || ""}">${esc(t)}</span>`;
const yesNo = (f) => (f ? badge("开", "ok") : badge("关", "off"));
const words = (list) =>
  Array.isArray(list) && list.length
    ? list.map((w) => `<code>${esc(w)}</code>`).join(" ")
    : "无";

/* ---------------- 主控（只读） ---------------- */

function personaCard(key, p) {
  const state = !p.configured
    ? badge("未配置", "bad")
    : p.loaded
      ? badge("已加载", "ok")
      : badge("读取失败", "warn");
  return `
    <article class="card persona">
      <header><h2>人格 ${key.toUpperCase()}</h2>${state}</header>
      <p class="name">${esc(p.name || "未设置")}</p>
      <p class="id">ID：<code>${esc(p.id || "—")}</code></p>
      <p class="meta">${esc(p.prompt_length)} 字 · 约 ${esc(p.prompt_tokens ?? "—")} tokens</p>
      <pre class="preview">${esc(p.prompt_preview || "（无内容）")}</pre>
    </article>`;
}

function renderSatiety(sat) {
  const box = $("satiety");
  if (!sat || !sat.enabled) {
    box.innerHTML = '<p class="dim">情绪数值未启用（弱词命中即切）。</p>';
    return;
  }
  const rows = (window.__sessions || []).slice(0, 5).map((s) => {
    const pct = Math.max(0, Math.min(100, (Number(s.satiety || 0) / sat.threshold) * 100));
    const dwell = s.dwell_remain > 0 ? `驻留剩余 ${s.dwell_remain}s` : "未驻留";
    return `<div class="sat-row">
      <div class="sat-head">
        <span>${esc(s.persona_name)} <span class="dim">${esc(s.umo)}</span></span>
        <span class="dim">${esc(s.satiety ?? 0)}/${esc(sat.threshold)} · ${esc(dwell)}</span>
      </div>
      <div class="sat-bar"><i style="width:${pct}%"></i></div>
    </div>`;
  }).join("");
  box.innerHTML =
    (rows || '<p class="dim">还没有会话数据。</p>') +
    `<p class="note">弱词 +${esc(sat.weak_add)} · 每条 -${esc(sat.decay_msg)} · 每10分钟 -${esc(sat.decay_10min)} · 回落 ≤${esc(sat.release)} · 驻留 ${esc(sat.min_dwell_min)} 分</p>`;
}

function renderBudget(budget) {
  const cells = [
    { label: "人设 A", value: budget.persona_tokens?.a ?? "—" },
    { label: "人设 B", value: budget.persona_tokens?.b ?? "—" },
    { label: "状态块", value: budget.state_block_tokens ?? "—" },
  ];
  $("budget").innerHTML = cells.map((c) =>
    `<div class="metric">
      <span class="metric-label">${esc(c.label)}</span>
      <span class="metric-value">${esc(c.value)}</span>
      <span class="metric-unit">tokens</span>
    </div>`).join("");
}

const kvRow = (k, v) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`;

function renderOverview(data) {
  const night = data.night || {};
  $("subtitle").textContent =
    `v${data.version || "?"} · ` +
    (night.active
      ? `🌙 深夜生效（${night.start}–${night.end}）`
      : `⏰ 非深夜（${night.start}–${night.end}）`);

  $("personas").innerHTML = ["a", "b"]
    .map((k) => personaCard(k, (data.personas || {})[k] || {})).join("");

  renderSatiety(data.satiety);
  renderBudget(data.prompt_budget || {});

  const cfg = data.effective_config || {};
  const bName = (data.personas || {}).b?.name || "B";
  const aName = (data.personas || {}).a?.name || "A";
  $("config").innerHTML = [
    kvRow("人格作用范围", cfg.state_scope === "global"
      ? badge("全局互通", "ok") : badge("按会话隔离", "warn")),
    kvRow("自动切换", yesNo(cfg.auto_switch)),
    kvRow("手动切换", yesNo(cfg.manual_switch)),
    kvRow("关键词权限", cfg.auto_switch_admin_only ? badge("仅管理员", "ok") : badge("所有人", "off")),
    kvRow("深夜区间", `${esc(cfg.night_start)}–${esc(cfg.night_end)} → ${esc(cfg.night_target_name || "A")}`),
    kvRow(`强词 → ${bName}`, words(cfg.strong_words_to_b)),
    kvRow(`强词 → ${aName}`, words(cfg.strong_words_to_a)),
    kvRow(`弱词 → ${bName}`, words(cfg.trigger_words_a)),
    kvRow(`弱词 → ${aName}`, words(cfg.trigger_words_b)),
    kvRow("弱词阈值 / 冷却", `${esc(cfg.weak_need)} 个 · ${esc(cfg.switch_cooldown_min)} 分`),
    kvRow("模型自主切换", cfg.llm_switch ? badge("开", "ok") : badge("关", "off")),
    kvRow("切换提示", cfg.switch_notice_mode === "off"
      ? badge("不发", "off") : badge(cfg.switch_notice_mode, "ok")),
    kvRow("自然接管提示", yesNo(cfg.natural_handoff)),
    kvRow("AI 自知", yesNo(cfg.self_aware)),
    kvRow("人设拼装方式", `<code>${esc(cfg.prompt_mode)}</code>`),
    kvRow("快捷后缀", `<code>/${esc(cfg.switch_trigger_suffix)}</code>`),
    kvRow("管理员数（配置内）", esc(cfg.admin_count)),
  ].join("");

  const sessions = data.sessions || [];
  window.__sessions = sessions;
  $("sessions").innerHTML = sessions.length
    ? `<thead><tr><th>会话</th><th>当前</th><th>tokens</th><th>锁定</th><th>活动</th></tr></thead><tbody>` +
      sessions.map((s) => `<tr>
        <td><code>${esc(s.umo)}</code></td>
        <td>${esc(s.persona_name)} <span class="dim">(${esc(s.persona_key?.toUpperCase())})</span></td>
        <td class="dim">${esc(s.tokens || "—")}</td>
        <td>${s.manual_lock ? badge("锁定", "warn") : "—"}</td>
        <td class="dim">${esc(s.updated)}</td>
      </tr>`).join("") + `</tbody>`
    : `<tbody><tr><td class="dim">还没有会话使用过。</td></tr></tbody>`;
  $("note").textContent = data.note || "";
}

/* ---------------- 设置（可编辑） ---------------- */

const FIELDS = {
  "form-core": [
    { k: "state_scope", t: "select", label: "人格作用范围",
      opts: [["global", "全局互通"], ["session", "按会话隔离"]] },
    { k: "manual_switch_enabled", t: "bool", label: "允许手动切换" },
    { k: "auto_switch_enabled", t: "bool", label: "启用自动切换" },
    { k: "auto_switch_admin_only", t: "bool", label: "关键词仅管理员生效" },
  ],
  "form-satiety": [
    { k: "satiety_enabled", t: "bool", label: "启用情绪数值" },
    { k: "satiety_threshold", t: "num", label: "激发阈值", min: 10, max: 100 },
    { k: "satiety_weak_add", t: "num", label: "每个弱词加分", min: 1, max: 100 },
    { k: "satiety_mood_add", t: "num", label: "同向情绪维持", min: 0, max: 100 },
    { k: "satiety_decay_msg", t: "num", label: "每条消息衰减", min: 0, max: 50 },
    { k: "satiety_decay_10min", t: "num", label: "每10分钟衰减", min: 0, max: 100 },
    { k: "satiety_release", t: "num", label: "回落阈值", min: 0, max: 100 },
    { k: "min_dwell_min", t: "num", label: "最短驻留（分钟）", min: 0, max: 720 },
    { k: "allow_single_char_words", t: "bool", label: "允许单字情绪词" },
    { k: "satiety_show_in_prompt", t: "bool", label: "把情绪值告诉模型" },
    { k: "weak_need", t: "num", label: "弱词命中数门槛", min: 1, max: 10 },
  ],
  "form-night": [
    { k: "night_start_time", t: "text", label: "深夜开始（HH:MM）" },
    { k: "night_end_time", t: "text", label: "深夜结束（HH:MM）" },
    { k: "night_target", t: "select", label: "深夜倾向",
      opts: [["a", "人格 A"], ["b", "人格 B"]] },
    { k: "llm_switch_enabled", t: "bool", label: "让模型自己判断换人" },
    { k: "natural_handoff", t: "bool", label: "自然接管提示" },
  ],
  "form-prompt": [
    { k: "prompt_mode", t: "select", label: "人设拼装方式",
      opts: [["prepend", "人格在前"], ["append", "平台在前"], ["override", "只用人格"]] },
    { k: "self_aware", t: "bool", label: "AI 自知" },
    { k: "self_aware_compact", t: "bool", label: "状态块压缩成一行" },
    { k: "switch_notice_mode", t: "select", label: "切换提示",
      opts: [["off", "不发"], ["auto_only", "仅自动切换"], ["manual_only", "仅手动切换"], ["always", "每次"]] },
    { k: "switch_notice_template", t: "text", label: "提示文案" },
    { k: "switch_trigger_suffix", t: "text", label: "快捷切换后缀" },
  ],
};

function fieldHtml(f, value) {
  const id = `f-${f.k}`;
  if (f.t === "bool") {
    return `<label class="field check" for="${id}">
      <input type="checkbox" id="${id}" data-k="${f.k}" data-t="bool" ${value ? "checked" : ""} />
      <span>${esc(f.label)}</span></label>`;
  }
  if (f.t === "select") {
    return `<label class="field" for="${id}">
      <span class="flabel">${esc(f.label)}</span>
      <select id="${id}" data-k="${f.k}" data-t="str">
        ${f.opts.map(([v, t]) =>
          `<option value="${esc(v)}" ${String(value) === v ? "selected" : ""}>${esc(t)}</option>`).join("")}
      </select></label>`;
  }
  const type = f.t === "num" ? "number" : "text";
  const extra = f.t === "num" ? `min="${f.min}" max="${f.max}" step="1"` : "";
  return `<label class="field" for="${id}">
    <span class="flabel">${esc(f.label)}</span>
    <input type="${type}" ${extra} id="${id}" data-k="${f.k}" data-t="${f.t}" value="${esc(value ?? "")}" />
  </label>`;
}

function renderSettings(data) {
  const cfg = data.effective_config || {};
  const extra = {
    state_scope: cfg.state_scope,
    satiety_enabled: data.satiety?.enabled,
    satiety_threshold: data.satiety?.threshold,
    satiety_weak_add: data.satiety?.weak_add,
    satiety_mood_add: data.satiety?.mood_add,
    satiety_decay_msg: data.satiety?.decay_msg,
    satiety_decay_10min: data.satiety?.decay_10min,
    satiety_release: data.satiety?.release,
    min_dwell_min: data.satiety?.min_dwell_min,
    allow_single_char_words: cfg.allow_single_char_words,
    satiety_show_in_prompt: cfg.satiety_show_in_prompt,
  };
  for (const [boxId, fields] of Object.entries(FIELDS)) {
    $(boxId).innerHTML = fields
      .map((f) => fieldHtml(f, extra[f.k] !== undefined ? extra[f.k] : cfg[f.k]))
      .join("");
  }
}

function collectSettings() {
  const out = {};
  document.querySelectorAll("#view-settings [data-k]").forEach((el) => {
    const k = el.dataset.k;
    if (el.dataset.t === "bool") out[k] = el.checked;
    else if (el.type === "number") {
      const n = Number(el.value);
      if (!Number.isNaN(n)) out[k] = n;
    } else out[k] = el.value;
  });
  return out;
}

async function save() {
  const msg = $("save-msg");
  msg.hidden = true;
  try {
    const res = await bridge.apiPost("settings", collectSettings());
    if (res?.error) throw new Error(res.error);
    const errs = res?.errors || {};
    if (Object.keys(errs).length) {
      msg.hidden = false;
      msg.textContent = "部分未保存：" +
        Object.entries(errs).map(([k, v]) => `${k}（${v}）`).join("；");
    } else {
      msg.hidden = false;
      msg.className = "ok-msg";
      msg.textContent = `已保存 ${res?.saved?.length || 0} 项，已立即生效`;
      setTimeout(() => { msg.hidden = true; msg.className = "err"; }, 2500);
    }
    await load();
  } catch (e) {
    msg.hidden = false;
    msg.textContent = `保存失败：${e?.message || e}`;
  }
}

/* ---------------- 加载与导航 ---------------- */

async function load() {
  if (!bridge) {
    $("subtitle").textContent = "未检测到插件视图 bridge，请在插件详情页打开本面板。";
    return;
  }
  try {
    const data = await bridge.apiGet("state");
    CURRENT = data || {};
    renderOverview(CURRENT);
    renderSettings(CURRENT);
  } catch (e) {
    $("subtitle").textContent = `读取失败：${e?.message || e}`;
  }
}

function switchView(name) {
  document.querySelectorAll(".tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name));
  $("view-overview").hidden = name !== "overview";
  $("view-settings").hidden = name !== "settings";
}

if (bridge) {
  await bridge.ready();
  bridge.onContext?.((next) => {
    if (next?.isDark !== undefined) {
      document.documentElement.dataset.theme = next.isDark ? "dark" : "light";
    }
  });
  document.querySelectorAll(".tab").forEach((b) =>
    b.addEventListener("click", () => switchView(b.dataset.view)));
  $("refresh").addEventListener("click", load);
  $("save").addEventListener("click", save);
  await load();
  setInterval(() => { if (!$("view-overview").hidden) load(); }, 15000);
}
