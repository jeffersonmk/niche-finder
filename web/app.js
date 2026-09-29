"use strict";
const $ = s => document.querySelector(s);
const S = { channels: [], config: null, genre: "", lang: "", sort: "score", text: "", scanLangs: [] };
const FLAG = { pt: "🇧🇷", en: "🇺🇸", es: "🇪🇸" };

const fmt = n => n >= 1e9 ? (n / 1e9).toFixed(2) + "B" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n);
const full = n => n.toLocaleString("pt-BR");
const norm = s => (s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
const scoreColor = s => `hsl(${Math.max(0, Math.min(130, (s - 30) * 2.6))} 62% 40%)`;

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
function busy(on) { ["#scanBtn", "#searchForm button"].forEach(s => $(s).disabled = on); document.querySelectorAll(".similar-btn").forEach(b => b.disabled = on); }

async function runJob(path, body, label) {
  busy(true); banner(""); status(label);
  try {
    const { job } = await api(path, body);
    while (true) {
      await new Promise(r => setTimeout(r, 900));
      const j = await api("/api/job?id=" + job);
      status(`${label} — ${j.message}`);
      if (j.status === "done") return j.results;
      if (j.status === "error") throw new Error(j.error);
    }
  } finally { status(""); busy(false); refreshQuota(); }
}

async function confirmCost(url, what) {
  const { cost } = await api(url);
  const q = S.quota;
  if (!cost) return true;
  return confirm(`${what}\n\nCusto estimado: até ${full(cost)} unidades de cota\nDisponível hoje: ${full(q.remaining)} de ${full(q.limit)}\n\n(Resultados repetidos em até 12h vêm do cache e não gastam cota.)`);
}

/* ---------------- dados ---------------- */
async function load() {
  const st = await api("/api/state");
  S.channels = st.channels; S.config = st.config; S.quota = st.quota; S.hasKey = st.has_key;
  if (!S.scanLangs.length) S.scanLangs = JSON.parse(localStorage.getItem("nf-scan-langs") || "null") || st.config.scan_languages;
  const c = st.config;
  $("#subtitle").textContent = `Canais criados há até ${c.days} dias · ${full(c.min_subs)}–${full(c.max_subs)} inscritos · ≥ ${c.min_views_per_sub} views por inscrito`;
  $("#criteria").innerHTML = `<b>Critérios</b><br>Canal criado há no máximo <b>${c.days} dias</b> e sem vídeos anteriores a isso<br>
    Inscritos: <b>${full(c.min_subs)} a ${full(c.max_subs)}</b> · Views: <b>≥ ${full(c.min_total_views)}</b> · Views/inscrito: <b>≥ ${c.min_views_per_sub}</b><br>
    <span class="muted small">Ajuste em config.json</span>`;
  $("#foot").textContent = `Última varredura: ${st.last_scan || "nunca"} · Cota hoje: ${full(st.quota.used)} / ${full(st.quota.limit)}`;
  $("#keyState").textContent = st.has_key ? "✓ Chave configurada" : "Nenhuma chave salva ainda";
  if (!st.has_key) banner("Configure sua chave da YouTube Data API em ⚙ Configurações para começar a buscar canais.");
  buildControls(); render();
}
async function refreshQuota() {
  const st = await api("/api/state"); S.quota = st.quota;
  $("#foot").textContent = `Última varredura: ${st.last_scan || "nunca"} · Cota hoje: ${full(st.quota.used)} / ${full(st.quota.limit)}`;
}
function merge(list) { const m = new Map(S.channels.map(c => [c.id, c])); list.forEach(c => m.set(c.id, c)); S.channels = [...m.values()]; }

/* ---------------- filtros ---------------- */
function buildControls() {
  const g = S.config.genres, counts = {};
  S.channels.forEach(c => c.genres.forEach(x => counts[x] = (counts[x] || 0) + 1));
  $("#genreChips").innerHTML = [["", "Todos", S.channels.length], ...Object.entries(g).map(([k, v]) => [k, v, counts[k] || 0])]
    .map(([k, v, n]) => `<button class="chip ${S.genre === k ? "on" : ""}" data-g="${k}">${v}<span class="n">${n}</span></button>`).join("");
  $("#genreChips").querySelectorAll(".chip").forEach(b => b.onclick = () => { S.genre = b.dataset.g; buildControls(); render(); });
  const langs = S.config.languages;
  $("#langSel").innerHTML = `<option value="">Todos os idiomas</option>` + Object.entries(langs).map(([k, v]) => `<option value="${k}" ${S.lang === k ? "selected" : ""}>${FLAG[k] || ""} ${v}</option>`).join("");
  $("#scanLangs").innerHTML = Object.entries(langs).map(([k, v]) => `<button type="button" class="chip ${S.scanLangs.includes(k) ? "on" : ""}" data-l="${k}">${FLAG[k] || ""} ${v}</button>`).join("");
  $("#scanLangs").querySelectorAll(".chip").forEach(b => b.onclick = () => {
    const l = b.dataset.l; S.scanLangs = S.scanLangs.includes(l) ? S.scanLangs.filter(x => x !== l) : [...S.scanLangs, l];
    if (!S.scanLangs.length) S.scanLangs = [l];
    localStorage.setItem("nf-scan-langs", JSON.stringify(S.scanLangs)); buildControls();
  });
  $("#scanMode").value = localStorage.getItem("nf-scan-mode") || "channel";
  updateCost();
}
async function updateCost() {
  try {
    const { cost } = await api(`/api/estimate?kind=scan&mode=${$("#scanMode").value}&langs=${S.scanLangs.join(",")}`);
    $("#costHint").textContent = `Uma varredura custa ~${full(cost)} unidades + ~1–3 por canal avaliado (limite diário: 10.000).`;
  } catch { }
}
$("#langSel").onchange = e => { S.lang = e.target.value; render(); };
$("#sortSel").onchange = e => { S.sort = e.target.value; render(); };
$("#q").oninput = e => { S.text = e.target.value; render(); };
$("#scanMode").onchange = e => { localStorage.setItem("nf-scan-mode", e.target.value); updateCost(); };

function visible() {
  const t = norm(S.text).trim();
  let list = S.channels.filter(c => (!S.genre || c.genres.includes(S.genre)) && (!S.lang || c.lang === S.lang) &&
    (!t || norm(c.title).includes(t) || c.video_titles.some(v => norm(v).includes(t))));
  const [key, dir] = S.sort.endsWith("_asc") ? [S.sort.slice(0, -4), 1] : [S.sort, -1];
  return list.sort((a, b) => (a[key] - b[key]) * dir);
}

/* ---------------- cards ---------------- */
function card(c) {
  const el = $("#cardTpl").content.firstElementChild.cloneNode(true);
  const t = norm(S.text).trim();
  el.querySelector(".avatar").src = c.avatar;
  const name = el.querySelector(".name"); name.textContent = c.title; name.href = c.url; name.title = c.title;
  el.querySelector(".views").textContent = fmt(c.views);
  el.querySelector(".views").title = full(c.views) + " views";
  const sc = el.querySelector(".score"); sc.textContent = c.score; sc.style.background = scoreColor(c.score);
  el.querySelector(".metrics").innerHTML = [
    [fmt(c.subscribers), "inscritos"], [fmt(c.views_per_day), "views/dia"], [c.views_per_sub, "views/insc."], [c.videos, "vídeos"],
  ].map(([b, s]) => `<div class="metric"><b>${b}</b><span>${s}</span></div>`).join("");
  el.querySelector(".age").textContent = `${Math.round(c.age_days)} dias de canal`;
  const max = Math.max(...c.timeline, 1);
  el.querySelector(".spark").innerHTML = c.timeline.map(v => `<i style="height:${Math.max(6, (v / max) * 100)}%" title="${full(v)} views"></i>`).join("");
  el.querySelector(".top").innerHTML = c.top_videos.map(v => {
    const hit = t && norm(v.title).includes(t);
    return `<li class="${hit ? "hit" : ""}"><img src="${esc(v.thumb)}" alt="" loading="lazy">
      <a href="https://www.youtube.com/watch?v=${esc(v.id)}" target="_blank" rel="noopener" title="${esc(v.title)}">${esc(v.title)}</a>
      <span class="v">${full(v.views)}</span></li>`;
  }).join("");
  // se a busca bateu num vídeo fora do top 3, mostra qual
  if (t && !c.top_videos.some(v => norm(v.title).includes(t)) && !norm(c.title).includes(t)) {
    const m = c.video_titles.find(v => norm(v).includes(t));
    if (m) el.querySelector(".top").insertAdjacentHTML("beforeend", `<li class="hit"><span class="muted small">Contém:</span><a title="${esc(m)}">${esc(m)}</a></li>`);
  }
  el.querySelector(".tags").innerHTML = [
    `${FLAG[c.lang] || "🌐"} ${(S.config.languages[c.lang] || c.lang || "?")}`,
    ...c.genres.map(g => S.config.genres[g] || g),
    c.shorts_share >= .6 ? "Shorts" : c.shorts_share <= .2 ? "Vídeos longos" : "Misto",
    `criado ${c.created.split("-").reverse().join("/")}`,
  ].map(x => `<span class="tag">${esc(x)}</span>`).join("");
  el.querySelector(".similar-btn").onclick = () => similar(c);
  return el;
}
const esc = s => String(s ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

function render() {
  const list = visible(), grid = $("#grid");
  grid.replaceChildren(...list.map(card));
  const e = $("#empty");
  e.hidden = list.length > 0;
  if (!S.channels.length) e.innerHTML = S.hasKey
    ? `Nenhum canal salvo ainda.<br><br><button class="btn primary" onclick="scan()">Fazer primeira varredura</button>`
    : `Nenhum canal ainda. Configure a chave da API em ⚙ Configurações e rode uma varredura.`;
  else e.innerHTML = S.text ? `Nenhum canal salvo tem “${esc(S.text)}”. Clique em <b>Buscar no YouTube</b> para procurar novos.` : "Nenhum canal com esses filtros.";
}

/* ---------------- ações ---------------- */
async function scan() {
  const mode = $("#scanMode").value || "channel";
  const url = `/api/estimate?kind=scan&mode=${mode}&langs=${S.scanLangs.join(",")}`;
  if (!await confirmCost(url, `Varredura dos 4 gêneros em: ${S.scanLangs.map(l => S.config.languages[l]).join(", ")}`)) return;
  try {
    const res = await runJob("/api/scan", { langs: S.scanLangs, mode }, "Varredura");
    merge(res); buildControls(); render(); banner(`Varredura concluída: ${res.length} canais aprovados.`);
  } catch (e) { banner(e.message, true); }
}
$("#scanBtn").onclick = scan;

$("#searchForm").onsubmit = async ev => {
  ev.preventDefault();
  const query = $("#q").value.trim();
  if (!query) return $("#q").focus();
  const cost = S.scanLangs.length * 2 * 100;
  if (!confirm(`Buscar no YouTube canais novos com vídeos sobre “${query}”?\n\nCusto: ${full(cost)} unidades de cota (${S.scanLangs.map(l => S.config.languages[l]).join(", ")})\nDisponível hoje: ${full(S.quota.remaining)}`)) return;
  try {
    const res = await runJob("/api/search", { query, langs: S.scanLangs }, `Buscando “${query}”`);
    merge(res); buildControls(); render();
    banner(res.length ? `${res.length} canais novos encontrados para “${query}”.` : `Nenhum canal novo passou nos critérios para “${query}”.`);
  } catch (e) { banner(e.message, true); }
};

async function similar(c) {
  const est = await api(`/api/estimate?kind=similar&channel=${c.id}`);
  const names = est.langs.map(l => S.config.languages[l]).join(", ");
  if (!confirm(`Buscar canais parecidos com “${c.title}” em: ${names}\n\nCusto estimado: até ${full(est.cost)} unidades de cota\nDisponível hoje: ${full(S.quota.remaining)}`)) return;
  const panel = $("#similarPanel");
  try {
    const res = await runJob("/api/similar", { channel: c.id }, `Procurando similares a “${c.title}”`);
    merge(res); buildControls(); render();
    $("#similarTitle").textContent = `Similares a “${c.title}” em outros idiomas (${res.length})`;
    $("#similarGrid").replaceChildren(...res.map(card));
    if (!res.length) $("#similarGrid").innerHTML = `<p class="muted">Nenhum canal novo desse nicho passou nos critérios em ${names}.</p>`;
    panel.hidden = false; panel.scrollIntoView({ behavior: "smooth" });
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
