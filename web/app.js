"use strict";
const $ = s => document.querySelector(s);
const S = { channels: [], config: null, genre: "", lang: "", sort: "score", text: "", scanLangs: [], kwLangs: [] };
const FLAG = { pt: "🇧🇷", en: "🇺🇸", es: "🇪🇸", ja: "🇯🇵", fr: "🇫🇷", de: "🇩🇪" };

const fmt = n => n >= 1e9 ? (n / 1e9).toFixed(2) + "B" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n);
const full = n => Number(n).toLocaleString("pt-BR");
const norm = s => (s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
const esc = s => String(s ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const scoreColor = s => `hsl(${Math.max(0, Math.min(130, (s - 30) * 2.6))} 62% 40%)`;
const langName = l => S.config.languages[l] || l;
const store = (k, v) => localStorage.setItem("nf-" + k, JSON.stringify(v));
const stored = (k, d) => { try { return JSON.parse(localStorage.getItem("nf-" + k)) ?? d; } catch { return d; } };

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.error) throw new Error(j.error || `Erro ${r.status}`);
  return j;
}

/* ---------------- tema ---------------- */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("nf-theme", t);
  $("#themeBtn").textContent = t === "dark" ? "☀" : "☾";
  $("#themeBtn").title = t === "dark" ? "Mudar para modo claro" : "Mudar para modo escuro";
}
$("#themeBtn").onclick = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
applyTheme(document.documentElement.dataset.theme);

/* ---------------- status ---------------- */
function status(msg) { $("#status").hidden = !msg; $("#statusText").textContent = msg || ""; }
function banner(msg, isErr) { const b = $("#banner"); b.hidden = !msg; b.textContent = msg || ""; b.classList.toggle("error", !!isErr); }
function busy(on) { ["#scanBtn", "#kwForm button"].forEach(s => $(s).disabled = on); document.querySelectorAll(".similar-btn").forEach(b => b.disabled = on); }

const FUNNEL_HINT = {
  "vídeos anteriores ao período": "canais com mais tempo — aumente “1º vídeo há no máx.”",
  "média de views baixa": "baixe “Média de views mín.”",
  "fora dos gêneros": "conteúdo fora de Música/Documentário/Curiosidades/Ensino",
};
function showFunnel(f) {
  const el = $("#funnel");
  if (!f) { el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = `<b>Funil desta busca:</b> ` + Object.entries(f).map(([k, v]) =>
    `<span class="fstep ${k === "aprovados" ? "ok" : ""}" title="${esc(FUNNEL_HINT[k] || "")}">${esc(k)} <b>${full(v)}</b></span>`).join("");
}

async function runJob(path, body, label) {
  busy(true); banner(""); showFunnel(null); status(label);
  try {
    const { job } = await api(path, body);
    while (true) {
      await new Promise(r => setTimeout(r, 900));
      const j = await api("/api/job?id=" + job);
      status(`${label} — ${j.message}`);
      if (j.status === "done") { showFunnel(j.funnel); return j; }
      if (j.status === "error") throw new Error(j.error);
    }
  } finally { status(""); busy(false); refreshQuota(); }
}

/* ---------------- critérios (ajustáveis na tela) ---------------- */
function crit() {
  return { days: +$("#cDays").value, min_avg_views: +$("#cAvg").value || 0 };
}
function saveCrit() { store("crit", crit()); subtitle(); }
function subtitle() {
  const c = crit();
  $("#subtitle").textContent = `1º vídeo há no máximo ${c.days} dias · média ≥ ${full(c.min_avg_views)} views por vídeo · qualquer nº de inscritos`;
}
["#cDays", "#cAvg"].forEach(s => $(s).onchange = saveCrit);

/* ---------------- dados ---------------- */
async function load() {
  const st = await api("/api/state");
  S.channels = st.channels; S.config = st.config; S.quota = st.quota; S.hasKey = st.has_key;
  const cc = st.config.criteria, saved = stored("crit", {});
  $("#cDays").value = String(saved.days ?? cc.days);
  if (![...$("#cDays").options].some(o => o.value === $("#cDays").value)) $("#cDays").add(new Option(`${cc.days} dias`, cc.days), 0);
  $("#cAvg").value = saved.min_avg_views ?? cc.min_avg_views;
  S.scanLangs = stored("scan-langs", st.config.scan_languages);
  S.kwLangs = stored("kw-langs", st.config.scan_languages);
  $("#kwTranslate").checked = stored("kw-translate", true);
  $("#kwDuration").value = stored("kw-duration", "");
  $("#kwGenre").innerHTML = `<option value="">Qualquer um dos 4</option>` + Object.entries(st.config.genres).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("#kwGenre").value = stored("kw-genre", "");
  $("#criteria").innerHTML = `<b>Critérios padrão</b> (config.json)<br>
    1º vídeo há no máximo <b>${cc.days} dias</b> · média ≥ <b>${full(cc.min_avg_views)}</b> views por vídeo<br>
    ≥ <b>${cc.min_views_per_sub}</b> views por inscrito · até <b>${cc.max_videos}</b> vídeos · inscritos: ${cc.min_subscribers || cc.max_subscribers ? `<b>${full(cc.min_subscribers)} a ${cc.max_subscribers ? full(cc.max_subscribers) : "∞"}</b>` : "<b>sem limite</b>"}<br>
    <span class="muted small">Idade e média também podem ser ajustadas na tela principal.</span>`;
  $("#scanMode").value = stored("scan-mode", st.config.default_mode);
  $("#keyState").textContent = st.has_key ? "✓ Chave configurada" : "Nenhuma chave salva ainda";
  if (!st.has_key) banner("Configure sua chave da YouTube Data API em ⚙ Configurações para começar a buscar canais.");
  footer(st.last_scan); subtitle(); buildControls(); render();
}
function footer(last) { $("#foot").textContent = `Última busca: ${last || "nunca"} · Cota hoje: ${full(S.quota.used)} / ${full(S.quota.limit)}`; }
async function refreshQuota() { const st = await api("/api/state"); S.quota = st.quota; footer(st.last_scan); }
function merge(list) { const m = new Map(S.channels.map(c => [c.id, c])); list.forEach(c => m.set(c.id, c)); S.channels = [...m.values()]; }

/* ---------------- controles ---------------- */
function langChips(el, selected, onToggle) {
  el.innerHTML = Object.entries(S.config.languages).map(([k, v]) =>
    `<button type="button" class="chip ${selected.includes(k) ? "on" : ""}" data-l="${k}">${FLAG[k] || ""} ${esc(v)}</button>`).join("");
  el.querySelectorAll(".chip").forEach(b => b.onclick = () => onToggle(b.dataset.l));
}
function toggle(list, l) { const r = list.includes(l) ? list.filter(x => x !== l) : [...list, l]; return r.length ? r : [l]; }

function buildControls() {
  const g = S.config.genres, counts = {};
  S.channels.forEach(c => c.genres.forEach(x => counts[x] = (counts[x] || 0) + 1));
  $("#genreChips").innerHTML = [["", "Todos", S.channels.length], ...Object.entries(g).map(([k, v]) => [k, v, counts[k] || 0])]
    .map(([k, v, n]) => `<button class="chip ${S.genre === k ? "on" : ""}" data-g="${k}">${esc(v)}<span class="n">${n}</span></button>`).join("");
  $("#genreChips").querySelectorAll(".chip").forEach(b => b.onclick = () => { S.genre = b.dataset.g; buildControls(); render(); });
  $("#langSel").innerHTML = `<option value="">Todos os idiomas</option>` + Object.entries(S.config.languages)
    .map(([k, v]) => `<option value="${k}" ${S.lang === k ? "selected" : ""}>${FLAG[k] || ""} ${esc(v)}</option>`).join("");
  langChips($("#scanLangs"), S.scanLangs, l => { S.scanLangs = toggle(S.scanLangs, l); store("scan-langs", S.scanLangs); buildControls(); });
  langChips($("#kwLangs"), S.kwLangs, l => { S.kwLangs = toggle(S.kwLangs, l); store("kw-langs", S.kwLangs); buildControls(); hidePlan(); });
  updateCost();
}
async function updateCost() {
  try {
    const { cost } = await api(`/api/estimate?mode=${$("#scanMode").value}&langs=${S.scanLangs.join(",")}`);
    $("#costHint").textContent = `Uma varredura nesses idiomas custa ~${full(cost)} unidades + ~3 por canal avaliado (limite diário: 10.000).`;
  } catch { }
}
$("#langSel").onchange = e => { S.lang = e.target.value; render(); };
$("#sortSel").onchange = e => { S.sort = e.target.value; render(); };
$("#q").oninput = e => { S.text = e.target.value; render(); };
$("#scanMode").onchange = e => { store("scan-mode", e.target.value); updateCost(); };
$("#kwTranslate").onchange = e => { store("kw-translate", e.target.checked); hidePlan(); };
$("#kwDuration").onchange = e => store("kw-duration", e.target.value);
$("#kwGenre").onchange = e => store("kw-genre", e.target.value);
$("#kw").oninput = hidePlan;

/* ---------------- lista ---------------- */
function visible() {
  const t = norm(S.text).trim();
  const list = S.channels.filter(c => (!S.genre || c.genres.includes(S.genre)) && (!S.lang || c.lang === S.lang) &&
    (!t || norm(c.title).includes(t) || c.video_titles.some(v => norm(v).includes(t))));
  const [key, dir] = S.sort.endsWith("_asc") ? [S.sort.slice(0, -4), 1] : [S.sort, -1];
  return list.sort((a, b) => ((a[key] ?? 0) - (b[key] ?? 0)) * dir);
}

function card(c) {
  const el = $("#cardTpl").content.firstElementChild.cloneNode(true);
  const t = norm(S.text).trim();
  el.querySelector(".avatar").src = c.avatar;
  const name = el.querySelector(".name"); name.textContent = c.title; name.href = c.url; name.title = c.title;
  const avg = c.avg_views ?? Math.round(c.views / Math.max(c.videos, 1));
  el.querySelector(".avg").textContent = fmt(avg);
  el.querySelector(".avg").title = `${full(avg)} views de média · mediana ${full(c.median_views ?? 0)}`;
  const sc = el.querySelector(".score"); sc.textContent = c.score; sc.style.background = scoreColor(c.score);
  el.querySelector(".metrics").innerHTML = [
    [c.hidden_subs ? "oculto" : fmt(c.subscribers), "inscritos"], [fmt(c.views), "views totais"], [fmt(c.views_per_day), "views/dia"], [c.videos, "vídeos"],
  ].map(([b, s]) => `<div class="metric"><b>${b}</b><span>${s}</span></div>`).join("");
  el.querySelector(".age").textContent = `1º vídeo há ${Math.round(c.age_days)} dias`;
  const max = Math.max(...c.timeline, 1);
  el.querySelector(".spark").innerHTML = c.timeline.map(v => `<i style="height:${Math.max(6, (v / max) * 100)}%" title="${full(v)} views"></i>`).join("");
  el.querySelector(".top").innerHTML = c.top_videos.map(v => {
    const hit = t && norm(v.title).includes(t);
    return `<li class="${hit ? "hit" : ""}"><img src="${esc(v.thumb)}" alt="" loading="lazy">
      <a href="https://www.youtube.com/watch?v=${esc(v.id)}" target="_blank" rel="noopener" title="${esc(v.title)}">${esc(v.title)}</a>
      <span class="v">${full(v.views)}</span></li>`;
  }).join("");
  if (t && !c.top_videos.some(v => norm(v.title).includes(t)) && !norm(c.title).includes(t)) {
    const m = c.video_titles.find(v => norm(v).includes(t));
    if (m) el.querySelector(".top").insertAdjacentHTML("beforeend", `<li class="hit"><span class="muted small">Contém:</span><a title="${esc(m)}">${esc(m)}</a></li>`);
  }
  const fmtKind = c.shorts_share >= .6 ? "Shorts" : c.shorts_share <= .2 ? (c.avg_minutes >= 20 ? `Longos (~${Math.round(c.avg_minutes)} min)` : "Vídeos longos") : "Misto";
  const found = (c.found_by || []).slice(0, 2).map(f => `🔎 ${f.q}`);
  el.querySelector(".tags").innerHTML = [
    `${FLAG[c.lang] || "🌐"} ${c.lang ? langName(c.lang) : "idioma ?"}`,
    ...c.genres.map(g => S.config.genres[g] || g), fmtKind, ...found,
  ].map(x => `<span class="tag" title="${esc(x)}">${esc(x)}</span>`).join("");
  el.querySelector(".similar-btn").onclick = () => similar(c);
  return el;
}

function render() {
  const list = visible();
  $("#grid").replaceChildren(...list.map(card));
  const e = $("#empty");
  e.hidden = list.length > 0;
  if (!S.channels.length) e.innerHTML = S.hasKey
    ? `Nenhum canal salvo ainda. Digite palavras-chave acima e clique em <b>Buscar canais</b>.`
    : `Nenhum canal ainda. Configure a chave da API em ⚙ Configurações.`;
  else e.innerHTML = S.text ? `Nenhum canal salvo tem “${esc(S.text)}”.` : "Nenhum canal com esses filtros.";
}

/* ---------------- plano de busca (pré-visualização, sem gastar cota do YouTube) ---------------- */
function hidePlan() { $("#plan").hidden = true; S.plan = null; }
function planHtml(queries) {
  return queries.map(x => `<span class="pq">${FLAG[x.lang] || ""} ${esc(x.q)}</span>`).join("");
}

$("#kwForm").onsubmit = async ev => {
  ev.preventDefault();
  const keywords = $("#kw").value.trim();
  if (!keywords) return $("#kw").focus();
  const translate = $("#kwTranslate").checked;
  const langs = translate ? S.kwLangs : [S.kwLangs[0] || "en"];
  const body = { keywords, translate, langs, genres: $("#kwGenre").value ? [$("#kwGenre").value] : [],
                 duration: $("#kwDuration").value, mode: "video", criteria: crit() };
  // 1º clique: mostra as buscas traduzidas e o custo; 2º clique: executa
  const sig = JSON.stringify(body);
  if (!S.plan || S.plan.sig !== sig) {
    status("Traduzindo palavras-chave…");
    try {
      const p = await api("/api/plan", body);
      S.plan = { sig };
      $("#plan").hidden = false;
      $("#plan").innerHTML = `<div><b>${p.queries.length} buscas</b> · custo ~${full(p.cost)} unidades + ~3 por canal avaliado · disponível hoje: ${full(p.remaining)}</div>
        <div class="plan-list">${planHtml(p.queries)}</div>
        <div class="muted small">Confira as traduções e clique em <b>Buscar canais</b> de novo para executar.</div>`;
      $("#kwForm button").textContent = "Confirmar busca";
    } catch (e) { banner(e.message, true); } finally { status(""); }
    return;
  }
  hidePlan(); $("#kwForm button").textContent = "Buscar canais";
  try {
    const j = await runJob("/api/search", body, "Buscando canais");
    merge(j.results); buildControls(); render();
    banner(j.results.length ? `${j.results.length} canais aprovados. Eles aparecem na lista abaixo.`
                            : `Nenhum canal passou nos critérios. Veja no funil onde eles caíram.`);
  } catch (e) { banner(e.message, true); }
};

async function scan() {
  const mode = $("#scanMode").value;
  const { cost } = await api(`/api/estimate?mode=${mode}&langs=${S.scanLangs.join(",")}`);
  if (!confirm(`Varredura dos conceitos do config.json em: ${S.scanLangs.map(langName).join(", ")}\n\nCusto estimado: ~${full(cost)} unidades + ~3 por canal avaliado\nDisponível hoje: ${full(S.quota.remaining)}`)) return;
  try {
    const j = await runJob("/api/scan", { langs: S.scanLangs, mode, criteria: crit() }, "Varredura");
    merge(j.results); buildControls(); render(); banner(`Varredura concluída: ${j.results.length} canais aprovados.`);
  } catch (e) { banner(e.message, true); }
}
$("#scanBtn").onclick = scan;

async function similar(c) {
  let p;
  try { status("Traduzindo títulos…"); p = await api("/api/plan", { channel: c.id, langs: S.kwLangs }); } catch (e) { return banner(e.message, true); } finally { status(""); }
  const lines = p.queries.map(x => `  ${(FLAG[x.lang] || x.lang)} ${x.q}`).join("\n");
  if (!confirm(`Canais parecidos com “${c.title}”\n\nBuscas geradas a partir dos títulos dos vídeos mais vistos:\n${lines}\n\nCusto: ~${full(p.cost)} unidades + ~3 por canal avaliado\nDisponível hoje: ${full(p.remaining)}`)) return;
  try {
    const j = await runJob("/api/similar", { channel: c.id, langs: S.kwLangs, criteria: crit() }, `Procurando canais parecidos com “${c.title}”`);
    merge(j.results); buildControls(); render();
    $("#similarTitle").textContent = `Parecidos com “${c.title}” (${j.results.length})`;
    $("#similarQueries").innerHTML = planHtml(j.extra.queries);
    $("#similarGrid").replaceChildren(...j.results.map(card));
    if (!j.results.length) $("#similarGrid").innerHTML = `<p class="muted">Nenhum canal parecido passou nos critérios. Veja o funil acima.</p>`;
    $("#similarPanel").hidden = false; $("#similarPanel").scrollIntoView({ behavior: "smooth" });
  } catch (e) { banner(e.message, true); }
}
$("#closeSimilar").onclick = () => $("#similarPanel").hidden = true;

/* ---------------- configurações ---------------- */
$("#settingsBtn").onclick = () => $("#settings").showModal();
$("#saveKey").onclick = async ev => {
  const key = $("#keyInput").value.trim();
  if (!key) return;
  ev.preventDefault();
  $("#keyState").textContent = "Validando…";
  try {
    const r = await api("/api/key", { key });
    $("#keyInput").value = "";
    if (r.ok) { $("#keyState").textContent = "✓ Chave válida e salva"; S.hasKey = true; banner(""); render(); }
    else $("#keyState").textContent = "⚠ Salva, mas a validação falhou: " + r.error;
  } catch (e) { $("#keyState").textContent = "⚠ " + e.message; }
};

load().catch(e => banner("Não foi possível carregar: " + e.message, true));
