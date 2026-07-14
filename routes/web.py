from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()


def _container(req: Request):
    return req.app.state.container


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>电子宠物面板</title>
<style>
  :root {
    --bg: #15171c; --panel: #1e2128; --line: #2c3038;
    --text: #e6e8ec; --muted: #8b919c; --accent: #7aa2f7;
    --good: #9ece6a; --warn: #e0af68; --bad: #f7768e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: 14px; line-height: 1.6;
  }
  header {
    padding: 16px 24px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  }
  header h1 { font-size: 18px; margin: 0; }
  select, input[type=number], input[type=text] {
    background: var(--bg); color: var(--text);
    border: 1px solid var(--line); border-radius: 6px;
    padding: 6px 10px; font-size: 14px;
  }
  select { background: var(--panel); }
  button {
    background: var(--accent); color: #15171c; border: none;
    border-radius: 6px; padding: 6px 12px; font-size: 13px; cursor: pointer;
  }
  button:hover { opacity: .85; }
  button:disabled { opacity: .4; cursor: default; }
  button.secondary { background: var(--line); color: var(--text); }
  .muted { color: var(--muted); }
  main { padding: 24px; display: grid; gap: 20px;
    grid-template-columns: minmax(280px, 1fr) minmax(280px, 1fr); }
  .panel {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 10px; padding: 16px;
  }
  .panel h2 { font-size: 15px; margin: 0 0 12px; }
  .span2 { grid-column: 1 / -1; }
  .meta { display: flex; flex-wrap: wrap; gap: 18px; }
  .meta div span { color: var(--muted); margin-right: 6px; }
  .bar-row { display: flex; align-items: center; gap: 10px; margin: 8px 0; }
  .bar-label { width: 84px; flex-shrink: 0; }
  .bar-track {
    flex: 1; height: 12px; background: var(--line);
    border-radius: 6px; overflow: hidden;
  }
  .bar-fill { display: block; height: 100%; border-radius: 6px; transition: width .4s; }
  .bar-input { width: 56px; text-align: right; padding: 4px 6px; font-size: 13px; }
  .vibe-edit { display: flex; gap: 8px; align-items: center; margin-top: 14px; }
  .vibe-edit input { flex: 1; }
  .state-controls { margin-top: 12px; }
  .gm-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .gm-actions input[type=text] { flex: 1; min-width: 200px; }
  .gm-actions label { color: var(--muted); display: flex; align-items: center; gap: 4px; }
  .gm-result { margin-top: 12px; font-size: 13px; color: var(--muted); min-height: 20px; }
  .gm-result.ok { color: var(--good); }
  .gm-result.bad { color: var(--bad); }
  .gameplay-box {
    border: 1px solid var(--line); border-radius: 8px; padding: 12px;
    background: #00000018;
  }
  .gameplay-box h3 { margin: 0 0 8px; font-size: 13px; color: var(--muted); }
  .card {
    border-left: 3px solid var(--accent); padding: 6px 12px;
    margin-bottom: 10px; background: #00000022; border-radius: 0 6px 6px 0;
  }
  .card .who { color: var(--accent); }
  .card .when { color: var(--muted); font-size: 12px; }
  .card .vibe-tag { color: var(--warn); font-size: 12px; }
  .msg { padding: 4px 0; border-bottom: 1px solid var(--line); }
  .msg:last-child { border-bottom: none; }
  .msg .name { color: var(--accent); }
  .msg.assistant .name { color: var(--good); }
  .msg.observer { opacity: .6; }
  .msg .time { color: var(--muted); font-size: 12px; margin-left: 8px; }
  .scroll { max-height: 420px; overflow-y: auto; }
  .scroll.small { max-height: 220px; }
  .empty { color: var(--muted); padding: 12px 0; }
  #err { color: var(--bad); padding: 24px; }
  @media (max-width: 720px) { main { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>🐾 电子宠物面板</h1>
  <select id="petSelect"></select>
  <span class="muted" id="refreshInfo"></span>
</header>
<div id="err"></div>
<main id="main" style="display:none">
  <div class="panel span2">
    <h2>基本信息</h2>
    <div class="meta" id="meta"></div>
  </div>
  <div class="panel">
    <h2>状态 <span class="muted">（可改数值后保存）</span></h2>
    <div id="bars"></div>
    <div class="vibe-edit">
      <span class="muted">vibe</span>
      <input type="text" id="vibeInput" placeholder="今日 vibe">
      <button class="secondary" id="vibeRandom">🎲 随机</button>
    </div>
    <div class="state-controls">
      <button id="saveState">💾 保存状态</button>
    </div>
  </div>
  <div class="panel">
    <h2>GM 操作</h2>
    <div class="gm-actions">
      <input type="text" id="speakTrigger" placeholder="主动发言触发情境（可选）">
      <button class="gm-btn" data-act="speak">💬 主动发言</button>
      <button class="gm-btn" data-act="dream">🌙 梦境</button>
      <button class="gm-btn" data-act="diary">📔 日记</button>
      <label><input type="checkbox" id="markDate"> 标记当天已发</label>
      <button class="gm-btn secondary" data-act="tick">⏱ 跑一轮 tick</button>
    </div>
    <div class="gm-result" id="gmResult"></div>
  </div>
  <div class="panel span2">
    <h2>当前需求</h2>
    <div class="gameplay-box">
      <div id="needBox"></div>
    </div>
  </div>
  <div class="panel span2">
    <h2>记忆卡片 <span class="muted" id="cardCount"></span></h2>
    <div class="scroll" id="cards"></div>
  </div>
  <div class="panel span2">
    <h2>最近消息</h2>
    <div class="scroll" id="messages"></div>
  </div>
</main>
<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const REFRESH_MS = 15000;
let currentPet = null;
let barLabels = {};
let numericKeys = [];

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}
function withToken(path) {
  const sep = path.includes("?") ? "&" : "?";
  return path + sep + "token=" + encodeURIComponent(TOKEN);
}
function api(path) {
  return fetch(withToken(path))
    .then(r => r.json().then(j => { if (!r.ok) throw new Error(j.error || r.status); return j; }));
}
function apiPost(path, body) {
  return fetch(withToken(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  }).then(r => r.json().then(j => { if (!r.ok) throw new Error(j.error || r.status); return j; }));
}
function fmtAge(bornAt) {
  if (!bornAt) return "—";
  const sec = Date.now() / 1000 - bornAt;
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  return d > 0 ? d + " 天 " + h + " 小时" : h + " 小时";
}
function fmtTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}
function barColor(v) {
  if (v >= 60) return "var(--good)";
  if (v >= 30) return "var(--warn)";
  return "var(--bad)";
}
function isEditing() {
  const a = document.activeElement;
  return a && (a.classList.contains("bar-input")
    || a.id === "vibeInput" || a.id === "speakTrigger");
}
function setBusy(busy) {
  document.querySelectorAll("button").forEach(b => { b.disabled = busy; });
}

function renderState(pet) {
  const st = pet.state;
  document.getElementById("bars").innerHTML = numericKeys.map(dim => {
    const shown = st[dim] == null ? 50 : st[dim];
    const label = barLabels[dim] || dim;
    return '<div class="bar-row" data-dim="' + dim + '">'
      + '<span class="bar-label">' + esc(label) + '</span>'
      + '<span class="bar-track"><span class="bar-fill" style="width:'
      + shown + '%;background:' + barColor(shown) + '"></span></span>'
      + '<input class="bar-input" type="number" min="0" max="100" value="'
      + Math.round(shown) + '"></div>';
  }).join("");
  document.querySelectorAll("#bars .bar-row").forEach(row => {
    const input = row.querySelector(".bar-input");
    const fill = row.querySelector(".bar-fill");
    input.addEventListener("input", () => {
      const v = Math.max(0, Math.min(100, Number(input.value) || 0));
      fill.style.width = v + "%";
      fill.style.background = barColor(v);
    });
  });
  document.getElementById("vibeInput").value = st.recent_vibe || "";

  document.getElementById("meta").innerHTML = [
    ["chat_id", esc(pet.chat_id)],
    ["pet_id", pet.id],
    ["年龄", fmtAge(pet.born_at)],
    ["消息数", pet.message_count],
    ["记忆卡片", pet.card_count],
    ["最后互动", fmtTime(st.last_update_ts)],
    ["最后主动发言", fmtTime(st.last_proactive_ts)],
  ].map(([k, v]) => "<div><span>" + k + "</span>" + v + "</div>").join("");
  renderGameplay(st);
}

function renderGameplay(st) {
  const need = st.active_need || {};
  document.getElementById("needBox").innerHTML = need.kind
    ? '<div><strong>' + esc(need.title || need.kind) + '</strong></div>'
      + '<div>' + esc(need.description || "") + '</div>'
      + '<div class="muted">severity ' + esc(need.severity || 1)
      + ' · expires ' + fmtTime(need.expires_at) + '</div>'
    : '<div class="empty">暂无需求</div>';
}

function renderCards(cards) {
  document.getElementById("cardCount").textContent = "(" + cards.length + ")";
  const box = document.getElementById("cards");
  if (!cards.length) { box.innerHTML = '<div class="empty">还没有记忆卡片</div>'; return; }
  box.innerHTML = cards.map(c =>
    '<div class="card">'
    + '<div class="when">' + esc(c.when || "") + "</div>"
    + '<div><span class="who">' + esc(c.who || "") + "</span> "
    + esc(c.what || "") + "</div>"
    + (c.vibe ? '<div class="vibe-tag">' + esc(c.vibe) + "</div>" : "")
    + "</div>"
  ).join("");
}

function renderMessages(msgs) {
  const box = document.getElementById("messages");
  if (!msgs.length) { box.innerHTML = '<div class="empty">还没有消息</div>'; return; }
  box.innerHTML = msgs.map(m => {
    const cls = m.role === "assistant" ? "assistant" : (m.is_observer ? "observer" : "");
    const name = m.role === "assistant" ? "🐾 宠物"
      : (m.sender_name || "群友") + (m.is_observer ? "（旁听）" : "");
    return '<div class="msg ' + cls + '">'
      + '<span class="name">' + esc(name) + "</span>"
      + '<span class="time">' + fmtTime(m.ts) + "</span>"
      + "<div>" + esc(m.content) + "</div></div>";
  }).join("");
  box.scrollTop = box.scrollHeight;
}

async function loadPet(petId) {
  const [cardsResp, msgResp] = await Promise.all([
    api("/gm/cards?pet_id=" + petId + "&limit=50"),
    api("/gm/messages?pet_id=" + petId + "&limit=60"),
  ]);
  renderCards(cardsResp.cards);
  renderMessages(msgResp.messages);
}

async function refresh() {
  try {
    const data = await api("/gm/pets");
    barLabels = data.bar_labels || {};
    numericKeys = data.numeric_keys || [];
    const pets = data.pets || [];
    const sel = document.getElementById("petSelect");
    if (sel.options.length !== pets.length) {
      sel.innerHTML = pets.map(p =>
        '<option value="' + p.id + '">#' + p.id + " " + esc(p.chat_id) + "</option>").join("");
    }
    if (!pets.length) {
      document.getElementById("err").textContent = "还没有宠物。";
      return;
    }
    document.getElementById("err").textContent = "";
    document.getElementById("main").style.display = "grid";
    if (currentPet == null) currentPet = pets[0].id;
    sel.value = currentPet;
    const pet = pets.find(p => p.id == currentPet) || pets[0];
    renderState(pet);
    await loadPet(pet.id);
    document.getElementById("refreshInfo").textContent =
      "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (e) {
    document.getElementById("err").textContent = "加载失败: " + e.message;
  }
}

async function gmAction(path, body, okMsg) {
  const res = document.getElementById("gmResult");
  res.className = "gm-result";
  res.textContent = "处理中…";
  setBusy(true);
  try {
    const j = await apiPost(path, body);
    res.className = "gm-result ok";
    res.textContent = okMsg
      + (j.reply ? " — " + j.reply : "")
      + (j.spoke === false ? "（未触发发言）" : "");
    await refresh();
  } catch (e) {
    res.className = "gm-result bad";
    res.textContent = "失败: " + e.message;
  } finally {
    setBusy(false);
  }
}

function saveState() {
  if (currentPet == null) return;
  const set = {};
  document.querySelectorAll("#bars .bar-row").forEach(row => {
    const dim = row.dataset.dim;
    const shown = Math.max(0, Math.min(100,
      Number(row.querySelector(".bar-input").value) || 0));
    set[dim] = shown;
  });
  const body = { pet_id: Number(currentPet), set: set };
  const vibe = document.getElementById("vibeInput").value.trim();
  if (vibe) body.recent_vibe = vibe;
  gmAction("/gm/state", body, "状态已保存");
}

function gmButton(act) {
  if (currentPet == null) return;
  const body = { pet_id: Number(currentPet) };
  if (act === "tick") { gmAction("/gm/tick", body, "已 tick 当前宠物"); return; }
  if (act === "speak") {
    const t = document.getElementById("speakTrigger").value.trim();
    if (t) body.trigger = t;
    gmAction("/gm/speak", body, "已触发主动发言");
    return;
  }
  body.mark = document.getElementById("markDate").checked;
  gmAction("/gm/" + act, body, act === "dream" ? "已触发梦境" : "已触发日记");
}

document.getElementById("petSelect").addEventListener("change", e => {
  currentPet = e.target.value;
  refresh();
});
document.getElementById("saveState").addEventListener("click", saveState);
document.getElementById("vibeRandom").addEventListener("click", () => {
  if (currentPet == null) return;
  gmAction("/gm/state", { pet_id: Number(currentPet), recent_vibe: "random" }, "已重抽 vibe");
});
document.querySelectorAll(".gm-btn").forEach(btn => {
  btn.addEventListener("click", () => gmButton(btn.dataset.act));
});
refresh();
setInterval(() => { if (!isEditing()) refresh(); }, REFRESH_MS);
</script>
</body>
</html>
"""


@router.get("/web")
async def web_dashboard(req: Request):
    container = _container(req)
    if not container.config.gm_token:
        return JSONResponse({"error": "gm_disabled"}, status_code=403)
    token = req.query_params.get("token") or req.headers.get("X-GM-Token")
    if not hmac.compare_digest(token or "", container.config.gm_token):
        return JSONResponse({"error": "gm_unauthorized"}, status_code=401)
    return HTMLResponse(_DASHBOARD_HTML)
