/* 拼影工坊 前端（无框架、无构建） */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const media = rel => rel ? "/media/" + rel.split("/").map(encodeURIComponent).join("/") : "";
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
};
const CAMS = [["推近", "Push in"], ["拉远", "Pull out"], ["左移", "Truck left"], ["右移", "Truck right"], ["左摇", "Pan left"], ["右摇", "Pan right"],
  ["上升", "Pedestal up"], ["下降", "Pedestal down"], ["上仰", "Tilt up"], ["下俯", "Tilt down"], ["跟随", "Tracking shot"], ["晃动", "Shake"], ["固定", "Static shot"]];
const FIT = { blur: "模糊背景填充", crop: "裁切铺满", pad: "黑边留白" };
const S = { meta: null, project: null, usage: {}, gen: {}, plan: {}, quota: null, poll: null, saveTimers: {}, route: "" };
const app = $("#app");

// ------------------------------------------------------------ utils
async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(url, opt); } catch (e) { throw new Error("连不上服务器，请检查网络"); }
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (res.status === 401 && !url.endsWith("/login")) { renderLogin(); throw new Error("请先登录"); }
  if (!res.ok) { const err = new Error(data.error || ("请求失败 HTTP " + res.status)); err.status = res.status; throw err; }
  return data;
}
function upload(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", url + (url.includes("?") ? "&" : "?") + "name=" + encodeURIComponent(file.name));
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = e => e.lengthComputable && onProgress && onProgress(e.loaded / e.total);
    x.onload = () => { let d = {}; try { d = JSON.parse(x.responseText); } catch (e) {} x.status < 300 ? resolve(d) : reject(new Error(d.error || "上传失败")); };
    x.onerror = () => reject(new Error("上传失败，网络中断"));
    x.send(file);
  });
}
function pickFile(accept) {
  return new Promise(res => { const i = document.createElement("input"); i.type = "file"; i.accept = accept; i.onchange = () => res(i.files[0] || null); i.click(); });
}
function toast(msg, type = "") {
  const t = document.createElement("div"); t.className = "toast " + type; t.textContent = msg; $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), type === "err" ? 7000 : 3500);
}
function modal(html, onMount) {
  const m = $("#modal"); $("#modalBox").innerHTML = html; m.classList.add("show");
  const close = () => m.classList.remove("show");
  $$("[data-close]", m).forEach(b => b.onclick = close);
  onMount && onMount($("#modalBox"), close);
  return close;
}
function confirmBox(title, body, okText = "确认") {
  return new Promise(res => {
    modal(`<h2>${esc(title)}</h2><div class="muted" style="margin-top:8px">${body}</div>
      <div class="acts"><button class="btn" data-no>取消</button><button class="btn primary" data-yes>${esc(okText)}</button></div>`, (box, close) => {
      $("[data-no]", box).onclick = () => { close(); res(false); };
      $("[data-yes]", box).onclick = () => { close(); res(true); };
    });
  });
}
const fmtTime = ts => { const d = new Date(ts * 1000); return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
const prov = id => (S.meta?.providers || []).find(p => p.id === id);
const provName = id => id === "upload" ? "上传" : (prov(id)?.name || id);

async function loadQuota() {
  try { S.quota = await api("GET", "/api/quota"); } catch (e) { return; }
  const q = S.quota; let pill = $("#qpill");
  if (!pill) { pill = document.createElement("a"); pill.id = "qpill"; pill.href = "#/quota"; pill.className = "badge acc"; pill.style.marginLeft = "auto"; pill.style.marginRight = "8px"; $(".topbar").insertBefore(pill, $("#nav")); }
  const tf = q.unlimited.length ? "不限" : q.today_free;
  pill.innerHTML = `<span class="long">今日免费 ${q.unlimited.length ? "" : "≈ "}<b>${tf}</b>${q.unlimited.length ? "" : " 条"}${q.trial_free ? ` · 试用 ≈ ${q.trial_free}` : ""}</span><span class="short">免费 <b>${tf}</b>${q.trial_free ? "+" + q.trial_free : ""}</span>`;
}

// ------------------------------------------------------------ router
async function boot() {
  try {
    const ping = await api("GET", "/api/ping");
    if (ping.auth_required && !ping.logged_in) return renderLogin();
    S.meta = await api("GET", "/api/meta");
    await loadQuota();
    window.onhashchange = route; route();
  } catch (e) { app.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
function setNav(k) { $$("#nav a").forEach(a => a.classList.toggle("on", a.dataset.nav === k)); $("#nav").style.display = ""; }
async function route() {
  clearInterval(S.poll); S.poll = null;
  const h = location.hash || "#/";
  let m;
  try {
    if ((m = h.match(/^#\/p\/(\d+)/))) { setNav("projects"); await openProject(+m[1]); }
    else if (h.startsWith("#/settings")) { setNav("settings"); await renderSettings(); }
    else if (h.startsWith("#/usage") || h.startsWith("#/quota")) { setNav("usage"); await renderQuota(); }
    else { setNav("projects"); await renderProjects(); }
  } catch (e) { if (e.message !== "请先登录") app.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  window.scrollTo(0, 0);
}

// ------------------------------------------------------------ login
function renderLogin() {
  $("#nav").style.display = "none";
  app.innerHTML = `<div class="login"><div class="card stack">
    <h1>拼影工坊</h1><div class="muted small">多模型 AI 视频生成与拼接</div>
    <div><label class="f">登录密码</label><input type="password" id="pw" autocomplete="current-password" autofocus></div>
    <button class="btn primary lg" id="loginBtn" style="width:100%">登录</button></div></div>`;
  const go = async () => {
    try { await api("POST", "/api/login", { password: $("#pw").value }); boot(); } catch (e) { toast(e.message, "err"); }
  };
  $("#loginBtn").onclick = go; $("#pw").onkeydown = e => e.key === "Enter" && go();
}

// ------------------------------------------------------------ projects
async function renderProjects() {
  const { projects } = await api("GET", "/api/projects");
  const ready = S.meta.providers.filter(p => p.ready).length;
  app.innerHTML = `
  <div class="hero"><div><h1>我的项目</h1><div class="muted small" style="margin-top:4px">一个视频一个项目：写分镜 → 每个镜头挑模型生成或上传片段 → 一键合成成片</div></div>
    <div class="row"><button class="btn" data-act="sample">✨ 从示例创建</button><button class="btn primary" data-act="newp">+ 新建项目</button></div></div>
  ${ready ? "" : `<div class="note" style="margin-top:16px">还没有配置任何模型。先去 <a href="#/settings">模型设置</a> 填 API Key（你已有 MiniMax 的 Token Plan Key）。没有 Key 也能用：把 App 里生成的视频上传到镜头里拼接。</div>`}
  ${S.meta.ffmpeg ? "" : `<div class="note" style="margin-top:12px">服务器未安装 ffmpeg，「合成成片」功能暂不可用。安装：<code>apt install ffmpeg</code> 或 <code>yum install ffmpeg</code></div>`}
  <div class="pgrid">${projects.length ? projects.map(p => `
    <div class="pcard" data-open="${p.id}">
      <div class="cover">${p.cover ? `<img src="${media(p.cover)}" alt="" loading="lazy">` : "🎬"}</div>
      <div class="meta"><div style="font-weight:600">${esc(p.name)}</div>
      <div class="muted small">${p.shots} 个镜头 · ${esc(p.aspect)} · ${fmtTime(p.updated)}</div></div>
    </div>`).join("") : `<div class="empty" style="grid-column:1/-1">还没有项目，点右上角新建，或「从示例创建」看看效果</div>`}</div>`;
  $$("[data-open]").forEach(el => el.onclick = () => location.hash = "#/p/" + el.dataset.open);
  $("[data-act=newp]").onclick = () => newProject(false);
  $("[data-act=sample]").onclick = () => newProject(true);
}
function newProject(sample) {
  modal(`<h2>${sample ? "从示例创建" : "新建项目"}</h2>
    ${sample ? `<div class="note info" style="margin-top:10px">示例「月弓守望者」：3 个镜头的国风玄幻短片，提示词已写好，直接选模型生成即可。</div>` : ""}
    <div class="stack" style="margin-top:12px">
      <div><label class="f">项目名称</label><input type="text" id="pn" value="${sample ? "月弓守望者" : ""}" placeholder="例如：月下蹬弓"></div>
      <div><label class="f">成片比例</label><select id="pa">${S.meta.aspects.map(a => `<option ${a === "9:16" ? "selected" : ""}>${a}</option>`).join("")}</select>
      <div class="small dim" style="margin-top:4px">9:16 适合抖音/视频号竖屏；不同比例的素材合成时会自动适配</div></div>
    </div>
    <div class="acts"><button class="btn" data-close>取消</button><button class="btn primary" id="pc">创建</button></div>`, (box, close) => {
    $("#pc", box).onclick = async () => {
      try { const r = await api("POST", "/api/projects", { name: $("#pn", box).value, aspect: $("#pa", box).value, sample }); close(); location.hash = "#/p/" + r.id; }
      catch (e) { toast(e.message, "err"); }
    };
  });
}

// ------------------------------------------------------------ project editor
async function loadUsage() {
  try { const u = await api("GET", "/api/usage"); S.usage = {}; u.providers.forEach(p => S.usage[p.provider] = p); } catch (e) {}
}
async function openProject(id) {
  const [p] = await Promise.all([api("GET", "/api/projects/" + id), loadUsage()]);
  S.project = p; renderProject(); startPoll();
}
function startPoll() {
  clearInterval(S.poll);
  S.poll = setInterval(() => {
    const p = S.project; if (!p || !location.hash.startsWith("#/p/")) return;
    const busy = p.shots.some(s => s.takes.some(t => ["running", "queued", "uploading"].includes(t.status))) || p.renders.some(r => ["queued", "running"].includes(r.status));
    if (busy) refreshProject();
  }, 5000);
}
async function refreshProject(full) {
  const p = S.project; if (!p) return;
  await flushSaves();
  const np = await api("GET", "/api/projects/" + p.id);
  const sig = x => x.shots.map(s => s.id).join(",");
  if (full || sig(np) !== sig(p)) { S.project = np; renderProject(); return; }
  np.shots.forEach((s, i) => {
    const o = p.shots[i];
    if (JSON.stringify([o.takes, o.selected_take]) !== JSON.stringify([s.takes, s.selected_take])) { const el = $("#takes-" + s.id); if (el) el.innerHTML = takesHTML(s); }
    if (o.image !== s.image) { const el = $("#frame-" + s.id); if (el) el.outerHTML = frameHTML(s); }
    s.prompt = o.prompt; s.title = o.title; // 保留本地正在编辑的内容
  });
  const rendersChanged = JSON.stringify(p.renders) !== JSON.stringify(np.renders);
  S.project = np;
  if (rendersChanged) { const el = $("#renders"); if (el) el.innerHTML = rendersHTML(np); }
  const sum = $("#sumline"); if (sum) sum.innerHTML = summaryHTML(np);
}

function renderProject() {
  const p = S.project;
  app.innerHTML = `
  <div class="spread">
    <div class="grow"><a href="#/" class="small muted">← 全部项目</a><br><input class="titlein" data-pf="name" value="${esc(p.name)}" maxlength="60"></div>
    <div class="row"><button class="btn" data-act="bulk">📋 批量导入分镜</button><button class="btn" data-act="addshot">+ 加镜头</button><button class="btn primary" data-act="genall">⚡ 一键生成全部镜头</button></div>
  </div>
  <details class="card" style="margin-top:14px" ${p.style || p.character ? "" : "open"}><summary style="cursor:pointer;font-weight:600">🎭 角色与风格设定 <span class="small dim" style="font-weight:400">· 自动加到每个镜头前面，换平台生成人物也尽量一致</span></summary>
    <div class="row" style="margin-top:10px;align-items:flex-start">
      <div class="grow" style="min-width:240px"><label class="f">画面风格</label><textarea data-pf="style" rows="2" placeholder="例如：国风玄幻CG电影画面，超高细节，体积光，冷蓝色调">${esc(p.style || "")}</textarea></div>
      <div class="grow" style="min-width:240px"><label class="f">主角外貌</label><textarea data-pf="character" rows="2" placeholder="例如：东方女弓手，黑色长发带一缕银白挑染，深蓝月白轻甲长裙，青玉色飘带">${esc(p.character || "")}</textarea></div>
    </div>
    <label class="chk" style="margin-top:8px"><input type="checkbox" data-pf="auto_translate" ${p.auto_translate !== 0 ? "checked" : ""}> 海外模型自动翻译成英文（借用 MiniMax 文本模型，需配置 MiniMax Key）</label>
  </details>
  <div class="editor">
    <div id="shots">${p.shots.length ? p.shots.map((s, i) => shotHTML(s, i)).join("") : `<div class="empty">还没有镜头，点「+ 加镜头」</div>`}
      <div class="addbar"><button class="btn" data-act="addshot">+ 加镜头</button><button class="btn" data-act="bulk">📋 批量导入分镜</button></div>
    </div>
    <aside class="side">${sideHTML(p)}</aside>
  </div>`;
}

function shotHTML(s, i) {
  const n = S.project.shots.length;
  return `<section class="shot" data-sid="${s.id}">
    <div class="shot-h"><span class="shot-n">${i + 1}</span><input class="grow" data-sf="title" value="${esc(s.title)}" maxlength="60">
      <button class="btn ghost sm" data-act="up" ${i === 0 ? "disabled" : ""} title="上移">↑</button>
      <button class="btn ghost sm" data-act="down" ${i === n - 1 ? "disabled" : ""} title="下移">↓</button>
      <button class="btn ghost sm danger" data-act="delshot" title="删除镜头">✕</button></div>
    <div class="shot-b">
      <div>
        <textarea data-sf="prompt" rows="6" placeholder="描述这个镜头：人物、动作、场景、光影、镜头运动……">${esc(s.prompt)}</textarea>
        <div class="spread small dim"><details><summary style="cursor:pointer">运镜指令（海螺专用，其他模型写中文描述即可）</summary>
          <div class="chips">${CAMS.map(([z, e]) => `<span class="chip" data-cam="${e}">${z} [${e}]</span>`).join("")}</div></details>
          <span data-count>${(s.prompt || "").length} 字</span></div>
        <div class="gen" id="gen-${s.id}">${genHTML(s)}</div>
      </div>
      ${frameHTML(s)}
    </div>
    <div class="takes" id="takes-${s.id}">${takesHTML(s)}</div>
  </section>`;
}

function frameHTML(s) {
  return `<div class="frame" id="frame-${s.id}">${s.image ? `<img src="${media(s.image)}" alt="首帧">
    <div class="fa"><button class="btn sm" data-act="imgup">换图</button><button class="btn sm" data-act="imggen">AI 重画</button><button class="btn sm danger" data-act="imgdel">移除</button></div>`
    : `<div>首帧图（可选）</div><div class="dim">有首帧就用「图生视频」，人物更统一，还能控制竖屏</div>
    <button class="btn sm" data-act="imgup">上传图片</button><button class="btn sm" data-act="imggen">AI 生成</button>`}</div>`;
}

// ---- generation panel
function genDefaults() { return store.get("cf_gen", {}); }
function genState(s) {
  const ready = S.meta.providers.filter(p => p.ready && p.video_models.length);
  if (!ready.length) return null;
  const g = Object.assign({ use_image: true, optimizer: false, provider: "auto", target: 5, pref: S.quota?.prefs?.pref || "save" }, genDefaults(), S.gen[s.id] || {});
  if (g.provider === "auto") return { auto: true, ready, g, mode: s.image && g.use_image ? "i2v" : "t2v" };
  let p = ready.find(x => x.id === g.provider) || ready[0];
  const mode = s.image && g.use_image ? "i2v" : "t2v";
  const models = p.video_models.filter(m => m.modes.includes(mode));
  let m = models.find(x => x.id === g.model) || models[0];
  if (!m) { // 这个平台没有支持当前模式的模型，换一个平台
    const alt = ready.find(x => x.video_models.some(mm => mm.modes.includes(mode)));
    if (alt) { p = alt; m = alt.video_models.find(mm => mm.modes.includes(mode)); }
  }
  const st = { ready, p, mode, models: p.video_models.filter(mm => mm.modes.includes(mode)), m, g };
  if (m) {
    // 默认：海螺按条计费选 10 秒；其他平台按秒计费选最短。清晰度默认 720p 档
    const defDur = p.id === "minimax" && m.durations.includes(10) ? 10 : m.durations[0];
    st.duration = m.durations.includes(+g.duration) ? +g.duration : defDur;
    const defRes = m.resolutions.find(r => /^(720p|768p)$/i.test(r)) || m.resolutions[0];
    st.resolution = m.resolutions.includes(g.resolution) ? g.resolution : defRes;
    if (p.id === "minimax" && st.resolution === "1080P" && st.duration === 10) st.resolution = "768P";
    const ratios = m.ratios ? (mode === "i2v" ? m.ratios : m.ratios.filter(r => r !== "adaptive")) : null;
    st.ratios = ratios;
    st.ratio = ratios ? (ratios.includes(g.ratio) ? g.ratio : (ratios.includes(S.project.aspect) ? S.project.aspect : ratios[0])) : null;
  }
  return st;
}
function autoGenHTML(s, st) {
  const g = st.g;
  setTimeout(() => loadPlan(s), 0);
  return `<div class="grid">
    <div><label class="f">平台</label><select data-g="provider"><option value="auto" selected>✨ 智能选择（自动用免费额度）</option>${["cn", "global"].map(gr => {
      const xs = st.ready.filter(x => (x.group || "cn") === gr);
      return xs.length ? `<optgroup label="手动指定 · ${gr === "cn" ? "国内" : "海外"}">${xs.map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</optgroup>` : ""; }).join("")}</select></div>
    <div><label class="f">目标时长</label><select data-g="target">${[3, 4, 5, 6, 8, 10].map(v => `<option value="${v}" ${+g.target === v ? "selected" : ""}>${v} 秒</option>`).join("")}</select></div>
    <div><label class="f">偏好</label><select data-g="pref"><option value="save" ${g.pref === "save" ? "selected" : ""}>省额度（先用每日免费）</option><option value="quality" ${g.pref === "quality" ? "selected" : ""}>画质优先</option></select></div>
  </div>
  <div class="spread" style="margin-top:8px">
    <div class="row">${s.image ? `<label class="chk"><input type="checkbox" data-g="use_image" ${g.use_image ? "checked" : ""}> 用首帧图（人物更统一）</label>` : `<span class="small dim">没有首帧图：文生视频</span>`}</div>
    <div class="row"><button class="btn primary" data-act="gen">✨ 生成视频</button></div>
  </div>
  <div class="small dim" id="plan-${s.id}" style="margin-top:6px">正在计算用哪家…</div>`;
}
async function loadPlan(s) {
  const el = $("#plan-" + s.id); if (!el) return;
  const g = genState(s).g;
  try {
    const r = await api("GET", `/api/shots/${s.id}/route?target=${g.target}&pref=${g.pref}&use_image=${s.image && g.use_image ? 1 : 0}`);
    S.plan[s.id] = r.plan;
    if (!r.plan.length) { el.innerHTML = `<span style="color:var(--err)">⚠ 没有可用的免费额度了</span> · <a href="#/quota">去额度中心看看</a>，或上传 App 里生成的片段`; return; }
    const c = r.plan[0];
    const kind = { daily: "每日免费", free: "永久免费", trial: "试用额度", paid: "付费" }[c.kind];
    el.innerHTML = `将使用 <b style="color:var(--text)">${esc(c.provider_name)}</b> · ${esc(c.model_label)} · ${c.duration}s（${kind}）` +
      (r.plan.length > 1 ? ` · 失败会自动换 ${r.plan.slice(1).map(x => esc(x.provider_name.replace(/（.*/, ""))).join("、")}` : "");
  } catch (e) { el.textContent = ""; }
}
function genHTML(s) {
  const st = genState(s);
  if (!st) return `<div class="small muted">还没有可用的模型。<a href="#/settings">去配置 API Key</a>，或者直接在下面上传 App 里生成好的片段。</div>`;
  if (st.auto) return autoGenHTML(s, st);
  const { ready, p, mode, models, m } = st;
  const u = S.usage[p.id] || {}; const lim = p.daily_limit;
  const opt = (arr, cur, lab = x => x) => arr.map(v => `<option value="${esc(v)}" ${String(v) === String(cur) ? "selected" : ""}>${esc(lab(v))}</option>`).join("");
  return `<div class="grid">
    <div><label class="f">平台</label><select data-g="provider"><option value="auto">✨ 智能选择</option>${["cn", "global"].map(g => { const xs = ready.filter(x => (x.group || "cn") === g);
      return xs.length ? `<optgroup label="${g === "cn" ? "国内" : "海外"}">${opt(xs.map(x => x.id), p.id, id => prov(id).name)}</optgroup>` : ""; }).join("")}</select></div>
    <div><label class="f">模型</label><select data-g="model">${m ? opt(models.map(x => x.id), m.id, id => models.find(x => x.id === id).label) : `<option>该平台无${mode === "i2v" ? "图生" : "文生"}模型</option>`}</select></div>
    ${m ? `<div><label class="f">时长</label><select data-g="duration">${opt(m.durations, st.duration, d => d + " 秒")}</select></div>
    <div><label class="f">清晰度</label><select data-g="resolution">${opt(m.resolutions, st.resolution)}</select></div>
    ${st.ratios ? `<div><label class="f">比例</label><select data-g="ratio">${opt(st.ratios, st.ratio, r => r === "adaptive" ? "跟随首帧" : r)}</select></div>` : ""}` : ""}
  </div>
  <div class="spread" style="margin-top:8px">
    <div class="row">
      ${s.image ? `<label class="chk"><input type="checkbox" data-g="use_image" ${st.g.use_image ? "checked" : ""}> 用首帧图（图生视频）</label>` : `<span class="small dim">文生视频${m && !m.ratios ? "（该模型文生固定横屏）" : ""}</span>`}
      <label class="chk" title="让平台自动改写/扩写提示词"><input type="checkbox" data-g="optimizer" ${st.g.optimizer ? "checked" : ""}> AI 改写提示词</label>
    </div>
    <div class="row"><span class="small dim">今日 ${u.video_today || 0}${lim ? " / " + lim : ""} 条</span>
      <button class="btn primary" data-act="gen" ${m ? "" : "disabled"}>生成视频</button></div>
  </div>
  ${m && m.note ? `<div class="small dim" style="margin-top:6px">💡 ${esc(m.note)}</div>` : ""}`;
}

// ---- takes
function takesHTML(s) {
  const horiz = S.project.aspect === "16:9" || S.project.aspect === "4:3";
  const tiles = s.takes.map(t => {
    const sel = t.id === s.selected_take && t.status === "succeeded";
    const badge = { running: `<span class="badge run"><span class="spin"></span>生成中</span>`, queued: `<span class="badge run"><span class="spin"></span>排队</span>`,
      uploading: `<span class="badge run"><span class="spin"></span>上传中</span>`, succeeded: `<span class="badge ok">完成</span>`, failed: `<span class="badge err">失败</span>` }[t.status] || "";
    const p = t.params || {};
    const desc = t.source === "upload" ? "上传：" + t.model : `${p.auto ? "✨自动 · " : ""}${provName(t.source).replace(/（.*/, "")} · ${t.model}${p.duration ? " · " + p.duration + "s" : ""}${p.mode === "i2v" ? " · 图生" : ""}${p.translated ? " · 已译英文" : ""}`;
    return `<div class="take ${sel ? "sel" : ""} ${horiz ? "h" : ""}" data-tid="${t.id}">
      ${sel ? `<span class="badge acc selmark" style="background:var(--bg)">✓ 成片使用</span>` : ""}
      <div class="vid">${t.status === "succeeded" ? `<video src="${media(t.file)}" ${t.thumb ? `poster="${media(t.thumb)}"` : ""} preload="none" controls playsinline></video>`
        : t.status === "failed" ? `<div style="padding:10px;color:var(--err)">✕ ${esc(t.error || "失败")}</div>` : `<div class="muted"><span class="spin"></span> ${t.status === "uploading" ? "上传中" : "生成中，通常 1~5 分钟"}</div>`}</div>
      <div class="tb"><div class="spread">${badge}<span class="dim">${t.duration ? t.duration.toFixed(1) + "s" : fmtTime(t.created)}</span></div>
        <div class="dim" style="margin-top:4px;word-break:break-all">${esc(desc)}</div>
        ${t.error && t.status !== "failed" ? `<div class="small" style="color:var(--err)">${esc(t.error)}</div>` : ""}
        <div class="acts">
          ${t.status === "succeeded" && !sel ? `<button class="btn sm primary" data-act="select">用这条</button>` : ""}
          ${t.status === "succeeded" ? `<button class="btn sm" data-act="trim">剪辑</button><a class="btn sm" href="${media(t.file)}?dl=1" title="下载">⬇</a>` : ""}
          ${t.task_id && t.status !== "succeeded" ? `<button class="btn sm" data-act="refresh" title="立即查询状态（不消耗额度）">↻</button>` : ""}
          <button class="btn sm ghost danger" data-act="deltake" title="删除">✕</button>
        </div>
        <div class="trim" data-trim style="display:none">
          <div><label>起点(秒)</label><input type="number" step="0.1" min="0" data-tf="start" value="${t.start || 0}"></div>
          <div><label>终点(0=结尾)</label><input type="number" step="0.1" min="0" data-tf="end" value="${t.end_t || 0}"></div>
          <div><label>速度</label><select data-tf="speed">${[0.5, 0.75, 1, 1.25, 1.5, 2].map(v => `<option value="${v}" ${+t.speed === v ? "selected" : ""}>${v}x</option>`).join("")}</select></div>
        </div>
      </div></div>`;
  }).join("");
  return tiles + `<div class="take" style="display:flex;align-items:center;justify-content:center;min-height:150px;border-style:dashed">
    <div style="text-align:center;padding:10px"><button class="btn sm" data-act="upclip">⬆ 上传片段</button>
    <div class="small dim" style="margin-top:6px">即梦/可灵/海螺 App 里免费生成的视频，下载后传到这里</div></div></div>`;
}

// ---- side (export)
function selectedItems(p) {
  return p.shots.map(s => s.takes.find(t => t.id === s.selected_take && t.status === "succeeded")).filter(Boolean);
}
function summaryHTML(p) {
  const items = selectedItems(p);
  let total = items.reduce((a, t) => a + Math.max(0, ((t.end_t || t.duration || 0) - (t.start || 0)) / (t.speed || 1)), 0);
  if (items.length > 1 && p.transition !== "none") total -= (items.length - 1) * p.tdur;
  return `已选 <b>${items.length}</b> / ${p.shots.length} 个镜头 · 预计 <b>${Math.max(0, total).toFixed(1)}</b> 秒`;
}
function sideHTML(p) {
  const opt = (obj, cur) => Object.entries(obj).map(([k, v]) => `<option value="${k}" ${k === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
  return `<div class="card stack">
    <h2>🎬 合成成片</h2>
    <div class="small muted" id="sumline">${summaryHTML(p)}</div>
    <div class="row" style="gap:8px">
      <div class="grow"><label class="f">成片比例</label><select data-pf="aspect">${opt(Object.fromEntries(S.meta.aspects.map(a => [a, a])), p.aspect)}</select></div>
      <div class="grow"><label class="f">素材适配</label><select data-pf="fit">${opt(FIT, p.fit)}</select></div>
    </div>
    <div class="row" style="gap:8px">
      <div class="grow"><label class="f">转场</label><select data-pf="transition">${opt(S.meta.transitions, p.transition)}</select></div>
      <div style="width:96px"><label class="f">转场时长</label><input type="number" step="0.1" min="0.1" max="2" data-pf="tdur" value="${p.tdur}"></div>
    </div>
    <div><label class="f">背景音乐</label>
      ${p.music ? `<div class="spread"><span class="small" style="word-break:break-all">🎵 ${esc(p.music_name || "音乐")}</span><button class="btn sm ghost danger" data-act="musicdel">移除</button></div>`
        : `<button class="btn sm" data-act="musicup">⬆ 上传音乐（mp3/m4a/wav）</button>`}
    </div>
    ${p.music ? `<div><label class="f">音乐音量 ${Math.round(p.music_vol * 100)}%</label><input type="range" min="0" max="1.5" step="0.05" data-pf="music_vol" value="${p.music_vol}" style="width:100%"></div>
      <label class="chk"><input type="checkbox" data-pf="keep_audio" ${p.keep_audio ? "checked" : ""}> 保留片段原声</label>
      ${p.keep_audio ? `<div><label class="f">原声音量 ${Math.round(p.orig_vol * 100)}%</label><input type="range" min="0" max="1.5" step="0.05" data-pf="orig_vol" value="${p.orig_vol}" style="width:100%"></div>` : ""}` : ""}
    <button class="btn primary lg" data-act="render" ${S.meta.ffmpeg ? "" : "disabled title='服务器未安装 ffmpeg'"}>合成成片</button>
    <div class="renders" id="renders">${rendersHTML(p)}</div>
  </div>`;
}
function rendersHTML(p) {
  if (!p.renders.length) return `<div class="small dim">合成好的视频会出现在这里，可以直接播放和下载。</div>`;
  return p.renders.map((r, i) => `<div class="r" data-rid="${r.id}">
    <div class="spread"><span class="small">${fmtTime(r.created)}${r.duration ? " · " + r.duration.toFixed(1) + "s" : ""}</span>
      ${r.status === "succeeded" ? `<span class="row"><a class="btn sm" href="${media(r.file)}?dl=1">⬇ 下载</a><button class="btn sm ghost danger" data-act="delrender">✕</button></span>`
        : r.status === "failed" ? `<span class="row"><span class="badge err">失败</span><button class="btn sm ghost danger" data-act="delrender">✕</button></span>` : `<span class="badge run"><span class="spin"></span>${esc(r.msg || "处理中")}</span>`}</div>
    ${["queued", "running"].includes(r.status) ? `<div class="bar"><i style="width:${Math.round((r.progress || 0) * 100)}%"></i></div>` : ""}
    ${r.status === "failed" ? `<div class="small" style="color:var(--err);white-space:pre-wrap">${esc(r.error)}</div>` : ""}
    ${r.status === "succeeded" && i === 0 ? `<video src="${media(r.file)}" controls playsinline preload="metadata"></video>` : ""}
  </div>`).join("");
}

// ---- saving
function queueSave(key, fn, delay = 600) {
  clearTimeout(S.saveTimers[key]?.t);
  S.saveTimers[key] = { fn, t: setTimeout(async () => { delete S.saveTimers[key]; try { await fn(); } catch (e) { toast(e.message, "err"); } }, delay) };
}
async function flushSaves() {
  const all = Object.entries(S.saveTimers); S.saveTimers = {};
  for (const [, v] of all) { clearTimeout(v.t); try { await v.fn(); } catch (e) {} }
}
const shotOf = el => { const sec = el.closest("[data-sid]"); return sec ? S.project.shots.find(s => s.id === +sec.dataset.sid) : null; };
const takeOf = el => { const s = shotOf(el); const t = el.closest("[data-tid]"); return s && t ? s.takes.find(x => x.id === +t.dataset.tid) : null; };

document.addEventListener("input", e => {
  const el = e.target;
  if (el.dataset.sf) {
    const s = shotOf(el); if (!s) return;
    s[el.dataset.sf] = el.value;
    if (el.dataset.sf === "prompt") { const c = el.closest(".shot").querySelector("[data-count]"); if (c) c.textContent = el.value.length + " 字"; }
    queueSave("shot" + s.id + el.dataset.sf, () => api("PUT", "/api/shots/" + s.id, { [el.dataset.sf]: el.value }));
  } else if (el.dataset.pf === "style" || el.dataset.pf === "character") {
    const k = el.dataset.pf; S.project[k] = el.value; queueSave("p" + k, () => api("PUT", "/api/projects/" + S.project.id, { [k]: el.value }));
  } else if (el.dataset.pf === "name") {
    S.project.name = el.value; queueSave("pname", () => api("PUT", "/api/projects/" + S.project.id, { name: el.value }));
  } else if (el.type === "range" && el.dataset.pf) {
    const lab = el.parentElement.querySelector("label"); if (lab) lab.textContent = lab.textContent.replace(/\d+%/, Math.round(el.value * 100) + "%");
  }
});
document.addEventListener("change", async e => {
  const el = e.target;
  if (el.dataset.g) {
    const s = shotOf(el); if (!s) return;
    const v = el.type === "checkbox" ? el.checked : el.value;
    S.gen[s.id] = Object.assign({}, genState(s)?.g, S.gen[s.id], { [el.dataset.g]: v });
    if (el.dataset.g === "provider") delete S.gen[s.id].model;
    const st = genState(s);
    if (st && st.auto) store.set("cf_gen", Object.assign(genDefaults(), { provider: "auto", [el.dataset.g]: v }));
    else if (st && st.m) { S.gen[s.id] = Object.assign(S.gen[s.id], { provider: st.p.id, model: st.m.id }); store.set("cf_gen", Object.assign(genDefaults(), { provider: st.p.id, model: st.m.id, [el.dataset.g]: v })); }
    $("#gen-" + s.id).innerHTML = genHTML(s);
  } else if (el.dataset.pf && !["name", "style", "character"].includes(el.dataset.pf)) {
    const k = el.dataset.pf; let v = el.type === "checkbox" ? el.checked : el.value;
    if (["tdur", "music_vol", "orig_vol"].includes(k)) v = +v;
    S.project[k] = v;
    try { await api("PUT", "/api/projects/" + S.project.id, { [k]: v }); } catch (err) { return toast(err.message, "err"); }
    if (k === "keep_audio" || k === "aspect") { const side = $(".side"); if (side) side.innerHTML = sideHTML(S.project); if (k === "aspect") S.project.shots.forEach(s => { const t = $("#takes-" + s.id); if (t) t.innerHTML = takesHTML(s); }); }
    const sum = $("#sumline"); if (sum) sum.innerHTML = summaryHTML(S.project);
  } else if (el.dataset.tf) {
    const t = takeOf(el); if (!t) return;
    const f = el.dataset.tf; const v = +el.value || (f === "speed" ? 1 : 0);
    if (f === "end") t.end_t = v; else t[f] = v;
    try { await api("PUT", "/api/takes/" + t.id, { start: t.start, end: t.end_t, speed: t.speed }); toast("已保存剪辑设置", "ok"); } catch (err) { toast(err.message, "err"); }
    const sum = $("#sumline"); if (sum) sum.innerHTML = summaryHTML(S.project);
  }
});

document.addEventListener("click", async e => {
  const cam = e.target.closest("[data-cam]");
  if (cam) {
    const ta = cam.closest(".shot").querySelector("textarea[data-sf=prompt]");
    const pos = ta.selectionStart ?? ta.value.length; const ins = "[" + cam.dataset.cam + "] ";
    ta.value = ta.value.slice(0, pos) + ins + ta.value.slice(ta.selectionEnd ?? pos); ta.focus(); ta.selectionStart = ta.selectionEnd = pos + ins.length;
    ta.dispatchEvent(new Event("input", { bubbles: true })); return;
  }
  const b = e.target.closest("[data-act]"); if (!b || !S.project || b.disabled) return;
  const act = b.dataset.act, p = S.project, s = shotOf(b), t = takeOf(b);
  try {
    switch (act) {
      case "addshot": await flushSaves(); await api("POST", `/api/projects/${p.id}/shots`, {}); await refreshProject(true);
        setTimeout(() => { const last = $$(".shot").pop(); last && last.scrollIntoView({ behavior: "smooth", block: "center" }); last?.querySelector("textarea")?.focus(); }, 50); break;
      case "bulk": bulkImport(); break;
      case "genall": {
        await flushSaves();
        const todo = p.shots.filter(x => (x.prompt || "").trim() && !x.takes.some(t => ["running", "queued", "succeeded"].includes(t.status)));
        if (!todo.length) return toast("所有镜头都已经有片段或正在生成了");
        const g = Object.assign({ target: 5, pref: "save" }, genDefaults());
        if (!(await confirmBox("一键生成全部？", `会给 <b style="color:var(--text)">${todo.length}</b> 个还没有片段的镜头自动生成视频（目标 ${g.target} 秒，${g.pref === "quality" ? "画质优先" : "省额度"}）。<br>平台会按「每日免费 → 试用额度」自动分配，额度不够会自动换家。`, "⚡ 开始"))) return;
        b.disabled = true; b.innerHTML = `<span class="spin"></span> 分配中`;
        try {
          const r = await api("POST", `/api/projects/${p.id}/generate_all`, { target: +g.target, pref: g.pref });
          toast(`已开始 ${r.started.length} 个镜头：\n${r.started.join("\n")}${r.errors.length ? "\n\n未能开始：\n" + r.errors.join("\n") : ""}`, r.errors.length && !r.started.length ? "err" : "ok");
          await loadQuota(); await refreshProject(); startPoll();
        } finally { b.disabled = false; b.textContent = "⚡ 一键生成全部镜头"; }
        break; }
      case "up": case "down": await flushSaves(); await api("POST", `/api/shots/${s.id}/move`, { dir: act === "up" ? -1 : 1 }); await refreshProject(true); break;
      case "delshot": if (await confirmBox("删除这个镜头？", `「${esc(s.title)}」以及它的所有片段都会被删除。`, "删除")) { await api("DELETE", "/api/shots/" + s.id); await refreshProject(true); } break;
      case "imgup": { const f = await pickFile("image/jpeg,image/png,image/webp"); if (!f) return; toast("上传中…"); await upload(`/api/shots/${s.id}/image`, f); await refreshProject(); toast("首帧已更新", "ok"); break; }
      case "imgdel": await api("DELETE", `/api/shots/${s.id}/image`); await refreshProject(); break;
      case "imggen": genImage(s); break;
      case "gen": await generate(s, b); break;
      case "upclip": { const f = await pickFile("video/*"); if (!f) return; b.disabled = true;
        try { await upload(`/api/shots/${s.id}/upload`, f, x => b.textContent = "上传中 " + Math.round(x * 100) + "%"); toast("片段已上传", "ok"); }
        finally { await refreshProject(); } break; }
      case "select": await api("POST", `/api/takes/${t.id}/select`); await refreshProject(); break;
      case "trim": { const tr = b.closest(".take").querySelector("[data-trim]"); tr.style.display = tr.style.display === "none" ? "grid" : "none"; break; }
      case "refresh": b.disabled = true; await api("POST", `/api/takes/${t.id}/refresh`); await refreshProject(); break;
      case "deltake": if (await confirmBox("删除这条片段？", "视频文件会从服务器删除。", "删除")) { await api("DELETE", "/api/takes/" + t.id); await refreshProject(); } break;
      case "musicup": { const f = await pickFile("audio/*"); if (!f) return; toast("上传中…"); await upload(`/api/projects/${p.id}/music`, f); await refreshProject(true); toast("音乐已添加", "ok"); break; }
      case "musicdel": await api("DELETE", `/api/projects/${p.id}/music`); await refreshProject(true); break;
      case "render": { await flushSaves(); const r = await api("POST", `/api/projects/${p.id}/render`);
        toast(r.shots_used < r.shots_total ? `开始合成：${r.shots_total} 个镜头里有 ${r.shots_used} 个有选中片段，其余跳过` : "开始合成，请稍候", "ok"); await refreshProject(); startPoll(); break; }
      case "delrender": { const rid = +b.closest("[data-rid]").dataset.rid; await api("DELETE", "/api/renders/" + rid); await refreshProject(); break; }
    }
  } catch (err) { toast(err.message, "err"); if (act === "refresh") b.disabled = false; }
});

async function generate(s, btn) {
  await flushSaves();
  if (!(s.prompt || "").trim()) return toast("先写这个镜头的提示词", "err");
  const st = genState(s); if (!st) return;
  if (st.auto) return generateAuto(s, btn, st);
  if (!st.m) return;
  const body = { provider: st.p.id, model: st.m.id, duration: st.duration, resolution: st.resolution, ratio: st.ratio, optimizer: !!st.g.optimizer, use_image: st.mode === "i2v" };
  const ok = await confirmBox("确认生成？", `<div>镜头：<b style="color:var(--text)">${esc(s.title)}</b></div>
    <div>${esc(st.p.name)} · ${esc(st.m.label)} · ${st.duration} 秒 · ${esc(st.resolution)}${st.ratio ? " · " + esc(st.ratio === "adaptive" ? "跟随首帧" : st.ratio) : ""} · ${st.mode === "i2v" ? "图生视频" : "文生视频"}</div>
    <div style="margin-top:8px;color:#efd3a3">会消耗该平台 1 次视频额度，提交后无法撤回。</div>`, "确认生成");
  if (!ok) return;
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span> 提交中`;
  const send = async force => api("POST", `/api/shots/${s.id}/generate`, Object.assign({}, body, { force }));
  try {
    try { await send(false); }
    catch (e) {
      if (e.status === 409) {
        if (!(await confirmBox("超过每日上限", esc(e.message.replace(/^LIMIT:/, "")) + "。<br>仍然要提交吗？（如果平台额度真的用完，会提交失败）", "仍然提交"))) return;
        await send(true);
      } else throw e;
    }
    toast("已提交！生成一般需要 1~5 分钟，完成后自动出现在下方", "ok");
    await loadUsage(); await refreshProject(); startPoll();
  } catch (e) { toast(e.message, "err"); }
  finally { const el = $("#gen-" + s.id); if (el) el.innerHTML = genHTML(s); }
}

async function generateAuto(s, btn, st) {
  const plan = S.plan[s.id] || [];
  if (!plan.length) return toast("没有可用的免费额度了，去「额度」页看看", "err");
  const c = plan[0];
  const ok = await confirmBox("确认生成？", `<div>镜头：<b style="color:var(--text)">${esc(s.title)}</b> · 目标 ${st.g.target} 秒</div>
    <div>自动选择：${esc(c.provider_name)} · ${esc(c.model_label)}</div>
    <div class="small" style="margin-top:6px">${c.unit === "usd" ? `预计花费试用金约 $${c.cost}` : "消耗 1 次额度"}。提交失败会自动换下一家，不用你操心。</div>`, "✨ 生成");
  if (!ok) return;
  btn.disabled = true; btn.innerHTML = `<span class="spin"></span> 提交中`;
  try {
    const r = await api("POST", `/api/shots/${s.id}/generate`, { provider: "auto", target: +st.g.target, pref: st.g.pref, use_image: !!st.g.use_image });
    toast(`已交给 ${r.provider} 生成${r.tried.length ? `（${r.tried.length} 家额度不足已自动跳过）` : ""}，1~5 分钟后自动出现`, "ok");
    await Promise.all([loadUsage(), loadQuota()]); await refreshProject(); startPoll();
  } catch (e) { toast(e.message, "err"); }
  finally { const el = $("#gen-" + s.id); if (el) el.innerHTML = genHTML(s); }
}

function genImage(s) {
  const ps = S.meta.providers.filter(p => p.ready && p.image_models.length);
  if (!ps.length) return toast("还没有支持生图的平台：配置 MiniMax 或火山方舟后可用。也可以用豆包/即梦免费生图后上传。", "err");
  const ratio = S.project.aspect;
  modal(`<h2>AI 生成首帧图</h2>
    <div class="note info small" style="margin-top:10px">首帧图决定人物长相和画面比例。描述一个<b>静止画面</b>效果最好，不用写镜头运动。</div>
    <div class="stack" style="margin-top:12px">
      <div class="row" style="gap:8px"><div class="grow"><label class="f">平台 / 模型</label><select id="gip">${ps.map(p => p.image_models.map(m => `<option value="${p.id}|${m.id}">${esc(p.name)} · ${esc(m.label)}</option>`).join("")).join("")}</select></div>
      <div style="width:110px"><label class="f">比例</label><select id="gir">${["9:16", "16:9", "1:1", "3:4", "4:3"].map(r => `<option ${r === ratio ? "selected" : ""}>${r}</option>`).join("")}</select></div></div>
      <div><label class="f">画面描述</label><textarea id="git" rows="7">${esc((s.prompt || "").replace(/\[[^\]]*\]\s*/g, ""))}</textarea></div>
    </div>
    <div class="acts"><button class="btn" data-close>取消</button><button class="btn primary" id="gigo">生成图片（消耗 1 次生图额度）</button></div>`, (box, close) => {
    $("#gigo", box).onclick = async ev => {
      const [provider, model] = $("#gip", box).value.split("|");
      ev.target.disabled = true; ev.target.innerHTML = `<span class="spin"></span> 生成中，约 10~60 秒…`;
      try { await api("POST", `/api/shots/${s.id}/genimage`, { provider, model, ratio: $("#gir", box).value, prompt: $("#git", box).value }); close(); toast("首帧图已生成", "ok"); await loadUsage(); await refreshProject(); }
      catch (e) { toast(e.message, "err"); ev.target.disabled = false; ev.target.textContent = "重试"; }
    };
  });
}

function bulkImport() {
  modal(`<h2>批量导入分镜</h2>
    <div class="small muted" style="margin-top:6px">每个镜头之间<b>空一行</b>。如果某段第一行很短（30 字内），会当作镜头标题。</div>
    <textarea id="bt" rows="12" style="margin-top:10px" placeholder="镜头1：出场\n国风玄幻CG电影画面，夜空中悬着一轮巨大的明月……\n\n镜头2：蓄力\n……"></textarea>
    <div class="acts"><button class="btn" data-close>取消</button><button class="btn primary" id="bgo">导入</button></div>`, (box, close) => {
    $("#bgo", box).onclick = async () => {
      try { const r = await api("POST", `/api/projects/${S.project.id}/shots/bulk`, { text: $("#bt", box).value }); close(); toast(`已导入 ${r.added} 个镜头`, "ok"); await refreshProject(true); }
      catch (e) { toast(e.message, "err"); }
    };
  });
}

// ------------------------------------------------------------ settings
const FREE_APPS_HTML = `<details class="card pset"><summary style="cursor:pointer;font-weight:600">🎁 App 里的免费额度（生成后下载，再到镜头里「上传片段」）</summary>
  <div class="tablewrap" style="margin-top:10px"><table>
  <tr><th>App / 网站</th><th>免费方式</th></tr>
  <tr><td>即梦（剪映同门）</td><td>每天登录送积分，可生成视频</td></tr>
  <tr><td>可灵 Kling</td><td>每天登录送灵感值（有资料称每日 66）</td></tr>
  <tr><td>海螺 AI / Vidu / 通义万相 App</td><td>每日或新用户赠送免费次数</td></tr>
  <tr><td>Google Vids</td><td>个人 Google 账号每月约 10 条 Veo 3.1 视频，无需付费订阅</td></tr>
  <tr><td>Runway / Luma / Pika</td><td>新用户一次性免费积分（通常带水印）</td></tr>
  <tr><td>Sora、Grok、Meta AI 等 App</td><td>部分地区提供免费生成次数，以 App 当前规则为准</td></tr>
  </table></div>
  <div class="small dim" style="margin-top:8px">各家免费规则经常调整，以 App 内显示为准。请遵守各平台条款，不要批量注册小号薅额度，容易封号。</div></details>`;
async function renderSettings() {
  S.meta = await api("GET", "/api/meta");
  app.innerHTML = `<h1>模型设置</h1>
  <div class="note info" style="margin-top:12px">这里填的都是各平台<b>开放平台的 API Key</b>，Key 保存在你自己的服务器上。
  App 里每天送的免费积分无法通过接口使用 —— 那部分请在 App 里生成后，回到项目里「上传片段」拼接。</div>
  ${FREE_APPS_HTML}
  ${S.meta.providers.map((p, i, arr) => `${i === 0 || arr[i - 1].group !== p.group ? `<h2 style="margin:26px 0 0">${p.group === "global" ? "🌏 海外平台" : "🇨🇳 国内平台（大多也有海外接口）"}</h2>` : ""}
  <div class="card pset" data-pid="${p.id}">
    <div class="spread"><div class="row"><span class="dot ${p.ready ? "on" : ""}"></span><h2>${esc(p.name)}</h2>${p.ready ? `<span class="badge ok">可用</span>` : `<span class="badge">未配置</span>`}</div>
      <label class="chk"><input type="checkbox" data-k="enabled" ${p.enabled ? "checked" : ""}> 启用</label></div>
    <div class="small muted" style="margin-top:6px">💰 ${esc(p.free)} · <a href="${esc(p.home)}" target="_blank" rel="noopener">去开放平台申请 Key ↗</a></div>
    <div class="grid">
      ${p.fields.map(f => f.multiline ? `<div style="grid-column:1/-1"><label class="f">${esc(f.label)}</label><textarea data-k="${f.k}" rows="4" autocomplete="off" spellcheck="false"
        style="font-family:ui-monospace,Menlo,monospace;font-size:12px" placeholder="${f.set ? "已设置（留空则不修改）" : "{ \"type\": \"service_account\", ... }"}"></textarea></div>`
        : `<div><label class="f">${esc(f.label)}</label><input type="${f.secret ? "password" : "text"}" data-k="${f.k}" autocomplete="off"
        placeholder="${f.set ? "已设置（留空则不修改）" : "未设置"}" value="${f.secret ? "" : esc(f.value)}"></div>`).join("")}
      <div><label class="f">接口地区</label><select data-k="region">${Object.entries(p.regions).map(([k, v]) => `<option value="${k}" ${k === p.region ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></div>
      <div><label class="f">每日上限提醒（0=不限）</label><input type="number" min="0" data-k="daily_limit" value="${p.daily_limit || 0}"></div>
      <div><label class="f">额度备注（自己看的）</label><input type="text" data-k="quota_note" value="${esc(p.quota_note)}" placeholder="例如：Token Plan 每天 3 条"></div>
      <div><label class="f">额外模型 ID（逗号分隔）</label><input type="text" data-k="extra_models" value="${esc(p.extra_models)}" placeholder="平台出了新模型就填这里"></div>
      <div><label class="f">额度类型</label><select data-k="free_kind">${[["daily", "每日免费（每天清零）"], ["free", "永久免费（不限量）"], ["trial", "试用额度（用完/过期为止）"], ["paid", "付费（仅兜底时用）"]].map(([k, v]) => `<option value="${k}" ${p.budget.free_kind === k ? "selected" : ""}>${v}</option>`).join("")}</select></div>
      <div class="row" style="gap:6px;align-items:flex-end"><div class="grow"><label class="f">额度数量（0=不知道）</label><input type="number" min="0" step="0.01" data-k="amount" value="${p.budget.amount || 0}"></div>
        <div style="width:90px"><select data-k="unit"><option value="count" ${p.budget.unit === "count" ? "selected" : ""}>条</option><option value="usd" ${p.budget.unit === "usd" ? "selected" : ""}>美元</option></select></div></div>
      ${p.id === "minimax" ? `<div><label class="f">文本模型（用于自动翻译）</label><input type="text" data-k="text_model" value="${esc(p.text_model || "")}" placeholder="默认 MiniMax-M2.7"></div>` : ""}
    </div>
    <label class="chk" style="margin-top:10px"><input type="checkbox" data-k="auto" ${p.budget.auto ? "checked" : ""}> 加入「✨ 智能选择」（自动模式会用这家的额度）</label>
    ${p.model_hint ? `<div class="small dim" style="margin-top:8px">💡 ${esc(p.model_hint)}</div>` : ""}
    <div class="small dim" style="margin-top:8px">内置模型：${p.video_models.map(m => esc(m.label)).join("、")}${p.image_models.length ? "；生图：" + p.image_models.map(m => esc(m.label)).join("、") : ""}</div>
    <div class="row" style="margin-top:12px;justify-content:flex-end">${p.fields.some(f => f.set) ? `<button class="btn sm ghost danger" data-clear>清除 Key</button><button class="btn sm" data-test>测试连接</button>` : ""}<button class="btn primary" data-save>保存</button></div>
  </div>`).join("")}
  <div class="card pset small muted">登录状态：<button class="btn sm" id="logout">退出登录</button></div>`;
  $$(".pset[data-pid]").forEach(card => {
    const pid = card.dataset.pid;
    const collect = () => { const o = {}; $$("[data-k]", card).forEach(i => o[i.dataset.k] = i.type === "checkbox" ? i.checked : i.value); return o; };
    $("[data-save]", card).onclick = async () => { try { await api("PUT", "/api/providers/" + pid, collect()); toast("已保存", "ok"); await loadQuota(); renderSettings(); } catch (e) { toast(e.message, "err"); } };
    const tb = $("[data-test]", card);
    if (tb) tb.onclick = async () => { tb.disabled = true; tb.innerHTML = `<span class="spin"></span> 测试中`; try { const r = await api("POST", `/api/providers/${pid}/test`); toast(r.msg, r.ok ? "ok" : "err"); } catch (e) { toast(e.message, "err"); } tb.disabled = false; tb.textContent = "测试连接"; };
    const c = $("[data-clear]", card);
    if (c) c.onclick = async () => { if (!(await confirmBox("清除 Key？", "清除后这个平台将不可用。", "清除"))) return;
      const o = {}; prov(pid).fields.forEach(f => o["clear_" + f.k] = true); await api("PUT", "/api/providers/" + pid, o); renderSettings(); };
  });
  $("#logout").onclick = async () => { await api("POST", "/api/logout"); location.reload(); };
}

// ------------------------------------------------------------ quota center
async function renderQuota() {
  await loadQuota();
  const q = S.quota;
  const unitTxt = r => r.unit === "usd" ? "$" : "";
  const rk = { daily: 0, free: 1, trial: 2, paid: 3 };
  const rows = q.providers.slice().sort((a, b) => (b.ready - a.ready) || (rk[a.free_kind] - rk[b.free_kind]));
  app.innerHTML = `<h1>额度中心</h1>
  <div class="muted small" style="margin-top:4px">所有平台的免费额度合在一起用。生成时选「✨ 智能选择」，平台会先用每天会清零的额度，再用会过期的试用额度，失败自动换下一家。</div>
  <div class="pgrid" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr))">
    <div class="card"><div class="muted small">今天还能免费生成</div><div style="font-size:30px;font-weight:700">${q.unlimited.length ? "不限" : "≈ " + q.today_free} <span style="font-size:15px">${q.unlimited.length ? "" : "条"}</span></div><div class="small dim">${q.unlimited.length ? `每日额度 ≈ ${q.today_free} 条 + 永久免费：${esc(q.unlimited.join("、").replace(/（[^）]*）/g, ""))}` : "每日额度，明天自动恢复"}</div></div>
    <div class="card"><div class="muted small">试用额度还能生成</div><div style="font-size:30px;font-weight:700">≈ ${q.trial_free} <span style="font-size:15px">条</span></div><div class="small dim">${q.unknown.length ? "另有 " + esc(q.unknown.join("、")) + " 额度未知，用完会自动跳过" : "按 6 秒/条估算"}</div></div>
    <div class="card stack"><div class="muted small">智能模式设置</div>
      <label class="chk"><input type="checkbox" id="allowPaid" ${q.prefs.allow_paid ? "checked" : ""}> 免费额度用完后允许付费兜底</label>
      <div><select id="prefSel"><option value="save" ${q.prefs.pref === "save" ? "selected" : ""}>默认：省额度</option><option value="quality" ${q.prefs.pref === "quality" ? "selected" : ""}>默认：画质优先</option></select></div></div>
  </div>
  <div class="card" style="margin-top:14px"><div class="tablewrap"><table>
    <tr><th>平台</th><th>类型</th><th>剩余</th><th>约可生成</th><th>状态</th><th></th></tr>
    ${rows.map(r => `<tr data-qp="${r.id}"><td><b>${esc(r.name)}</b><div class="small dim">${esc(r.free)}</div></td>
      <td><span class="badge ${["daily", "free"].includes(r.free_kind) ? "ok" : r.free_kind === "trial" ? "acc" : ""}">${esc(r.kind_label)}</span></td>
      <td>${!r.ready ? "—" : r.free_kind === "free" ? `<span class="dim">免费（已用 ${r.used}）</span>` : r.remaining == null ? `<span class="dim">未知（已用 ${unitTxt(r)}${r.used}）</span>` : `${unitTxt(r)}${r.remaining} / ${unitTxt(r)}${r.amount}
        <div class="bar" style="width:110px"><i style="width:${Math.max(0, Math.min(100, r.remaining / r.amount * 100))}%"></i></div>`}</td>
      <td>${!r.ready ? "—" : r.free_kind === "free" && !r.exhausted ? "不限" : r.est_videos == null ? "?" : r.est_videos + " 条"}</td>
      <td>${!r.ready ? `<a class="btn sm" href="${esc(r.home)}" target="_blank" rel="noopener">去领取 ↗</a> <a class="btn sm" href="#/settings">填 Key</a>`
        : r.exhausted ? `<span class="badge err" title="${esc(r.exhausted.msg)}">${r.exhausted.day ? "今日已用完" : "已用完/异常"}</span>` : r.auto ? `<span class="badge ok">可用</span>` : `<span class="badge">未加入智能模式</span>`}</td>
      <td class="row" style="gap:4px">${r.ready ? `<button class="btn sm" data-qtest>测试</button>${r.exhausted ? `<button class="btn sm" data-qreset title="额度已经恢复（比如充值了）">恢复</button>` : ""}` : ""}</td></tr>`).join("")}
  </table></div>
  <div class="small dim" style="margin-top:8px">「剩余」是本平台按你设置的额度估算的，实际以各平台控制台为准；在「模型设置」里可以修改每家的额度类型和数量。测试连接不消耗额度。</div></div>
  <div id="usageBox"></div>`;
  $("#allowPaid").onchange = async e => { await api("PUT", "/api/prefs", { allow_paid: e.target.checked }); toast("已保存", "ok"); loadQuota(); };
  $("#prefSel").onchange = async e => { await api("PUT", "/api/prefs", { pref: e.target.value }); toast("已保存", "ok"); };
  $$("[data-qp]").forEach(tr => {
    const pid = tr.dataset.qp;
    const t = $("[data-qtest]", tr); if (t) t.onclick = async () => { t.disabled = true; t.innerHTML = `<span class="spin"></span>`; try { const r = await api("POST", `/api/providers/${pid}/test`); toast(r.msg, r.ok ? "ok" : "err"); } catch (e) { toast(e.message, "err"); } t.disabled = false; t.textContent = "测试"; renderQuota(); };
    const rs = $("[data-qreset]", tr); if (rs) rs.onclick = async () => { await api("POST", `/api/providers/${pid}/reset`); renderQuota(); };
  });
  renderUsage($("#usageBox"));
}
async function renderUsage(box) {
  const u = await api("GET", "/api/usage");
  box.innerHTML = `<h2 style="margin-top:22px">用量记录</h2><div class="muted small" style="margin-top:4px">统计本平台提交成功的次数（不含上传的片段）。各平台实际剩余额度请以官方控制台为准。</div>
  <div class="card" style="margin-top:16px"><div class="tablewrap"><table><tr><th>平台</th><th>今日视频</th><th>累计视频</th><th>今日生图</th><th>累计生图</th><th>每日上限</th><th>备注</th></tr>
  ${u.providers.filter(p => p.video_total || p.image_total).map(p => `<tr><td>${esc(p.name)}</td><td><b>${p.video_today}</b></td><td>${p.video_total}</td><td>${p.image_today}</td><td>${p.image_total}</td><td>${p.daily_limit || "—"}</td><td class="muted">${esc(p.quota_note || "")}</td></tr>`).join("")}
  </table></div></div>
  <div class="card" style="margin-top:14px"><h3>最近记录</h3><div class="tablewrap" style="margin-top:8px"><table><tr><th>时间</th><th>平台</th><th>类型</th><th>模型</th><th>结果</th></tr>
  ${u.recent.length ? u.recent.map(r => `<tr><td>${fmtTime(r.created)}</td><td>${esc(provName(r.provider))}</td><td>${r.kind === "video" ? "视频" : "图片"}</td><td class="muted">${esc(r.model)}</td><td>${r.ok ? `<span class="badge ok">提交成功</span>` : `<span class="badge err">失败</span>`}</td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">暂无记录</td></tr>`}</table></div></div>`;
}

boot();
