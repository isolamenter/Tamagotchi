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
<title>小苍蝇 Chatbot 控制面板</title>
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
  .gm-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .gm-actions input[type=text] { flex: 1; min-width: 200px; }
  .gm-actions label { color: var(--muted); display: flex; align-items: center; gap: 4px; }
  .gm-result { margin-top: 12px; font-size: 13px; color: var(--muted); min-height: 20px; }
  .gm-result.ok { color: var(--good); }
  .gm-result.bad { color: var(--bad); }
  .card {
    border-left: 3px solid var(--accent); padding: 6px 12px;
    margin-bottom: 10px; background: #00000022; border-radius: 0 6px 6px 0;
  }
  .card.imagination { border-left-color: var(--warn); }
  .card.hearsay { border-left-color: var(--muted); }
  .card .who { color: var(--accent); }
  .card .when { color: var(--muted); font-size: 12px; }
  .card .vibe-tag { color: var(--warn); font-size: 12px; }
  .card .kind-badge {
    display: inline-block; padding: 1px 6px; border-radius: 4px;
    font-size: 11px; margin-right: 6px; background: var(--line);
  }
  .msg { padding: 4px 0; border-bottom: 1px solid var(--line); }
  .msg:last-child { border-bottom: none; }
  .msg .name { color: var(--accent); }
  .msg.assistant .name { color: var(--good); }
  .msg.observer { opacity: .6; }
  .msg .time { color: var(--muted); font-size: 12px; margin-left: 8px; }
  .scroll { max-height: 420px; overflow-y: auto; }
  .scroll.small { max-height: 220px; }
  .image-drop {
    border: 1.5px dashed var(--line); border-radius: 8px;
    padding: 18px; text-align: center; color: var(--muted);
    cursor: pointer; transition: border-color .2s, background .2s;
  }
  .image-drop.dragover { border-color: var(--accent); background: #7aa2f722; }
  .image-preview { margin-top: 12px; text-align: center; }
  .image-preview img { max-width: 100%; max-height: 260px; border-radius: 8px; border: 1px solid var(--line); }
  .image-meta { margin-top: 6px; font-size: 12px; color: var(--muted); }
  .empty { color: var(--muted); padding: 12px 0; }
  #err { color: var(--bad); padding: 24px; }
  @media (max-width: 720px) { main { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>🪰 小苍蝇 Chatbot 控制面板</h1>
  <select id="petSelect"></select>
  <span class="muted" id="refreshInfo"></span>
</header>
<div id="err"></div>
<main id="main" style="display:none">
  <div class="panel span2">
    <h2>基本信息</h2>
    <div class="meta" id="meta"></div>
  </div>
  <div class="panel span2">
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
    <h2>📝 发送文字</h2>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <input type="text" id="sendTextInput" placeholder="要发到当前群的内容" style="flex:1;min-width:200px">
      <button id="sendTextBtn">📤 发送到当前群</button>
    </div>
    <div class="gm-result" id="textResult"></div>
  </div>
  <div class="panel span2">
    <h2>🖼️ 发送图片 <span class="muted" style="font-weight:normal">— 粘贴或选择，不在服务器留存</span></h2>
    <div class="image-drop" id="imageDrop" tabindex="0">
      <div>点击此处按 <b>Ctrl+V</b> 粘贴截图，或拖拽/选择图片</div>
      <div style="margin-top:6px;font-size:12px">支持 png / jpg / gif / webp，≤10MB</div>
      <input type="file" id="imageFile" accept="image/*" style="display:none">
    </div>
    <div class="image-preview" id="imagePreview" style="display:none">
      <img id="previewImg" alt="preview">
      <div class="image-meta" id="previewMeta"></div>
    </div>
    <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button id="sendImageBtn" disabled>📤 发送到当前群</button>
      <button class="secondary" id="clearImageBtn" style="display:none">清除</button>
      <span class="muted" id="imageHint">目标：上方下拉选中的群聊</span>
    </div>
    <div class="gm-result" id="imageResult"></div>
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
function setBusy(busy) {
  document.querySelectorAll("button").forEach(b => { b.disabled = busy; });
}

function renderMeta(pet) {
  const st = pet.state || {};
  document.getElementById("meta").innerHTML = [
    ["chat_id", esc(pet.chat_id)],
    ["pet_id", pet.id],
    ["创建时长", fmtAge(pet.born_at)],
    ["消息总数", pet.message_count],
    ["记忆卡片", pet.card_count],
    ["最后互动", fmtTime(st.last_update_ts)],
    ["最后回复", fmtTime(st.last_reply_ts)],
    ["最后主动发言", fmtTime(st.last_proactive_ts)],
    ["前次主动话题", esc(st.last_proactive_topic || "无")],
  ].map(([k, v]) => "<div><span>" + k + "</span>" + v + "</div>").join("");
}

function renderCards(cards) {
  document.getElementById("cardCount").textContent = "(" + cards.length + ")";
  const box = document.getElementById("cards");
  if (!cards.length) { box.innerHTML = '<div class="empty">还没有记忆卡片</div>'; return; }
  box.innerHTML = cards.map(c => {
    const kind = c.kind || "reality";
    const kindLabel = kind === "imagination" ? "🌙 梦境/想象" : (kind === "hearsay" ? "🗣️ 转述" : "📌 真实互动");
    return '<div class="card ' + esc(kind) + '">'
      + '<div class="when"><span class="kind-badge">' + kindLabel + '</span>' + esc(c.when || "") + "</div>"
      + '<div><span class="who">' + esc(c.who || "") + "</span> "
      + esc(c.what || "") + "</div>"
      + (c.vibe ? '<div class="vibe-tag">' + esc(c.vibe) + "</div>" : "")
      + "</div>";
  }).join("");
}

function renderMessages(msgs) {
  const box = document.getElementById("messages");
  if (!msgs.length) { box.innerHTML = '<div class="empty">还没有消息</div>'; return; }
  box.innerHTML = msgs.map(m => {
    const cls = m.role === "assistant" ? "assistant" : (m.is_observer ? "observer" : "");
    const name = m.role === "assistant" ? "🪰 小苍蝇"
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
  renderCards(cardsResp.cards || []);
  renderMessages(msgResp.messages || []);
}

async function refreshAll(keepSelection) {
  try {
    const data = await api("/gm/pets");
    const pets = data.pets || [];
    if (!pets.length) {
      document.getElementById("err").textContent = "数据库里还没有群聊/宠物记录。在飞书群里 @ 它一次即可自动注册。";
      document.getElementById("main").style.display = "none";
      return;
    }
    document.getElementById("err").textContent = "";
    document.getElementById("main").style.display = "grid";

    const sel = document.getElementById("petSelect");
    const prevId = keepSelection && sel.value ? Number(sel.value) : (currentPet ? currentPet.id : pets[0].id);
    sel.innerHTML = pets.map(p =>
      '<option value="' + p.id + '">' + esc(p.chat_id) + " (id=" + p.id + ")</option>"
    ).join("");
    const matched = pets.find(p => p.id === prevId) || pets[0];
    sel.value = String(matched.id);
    currentPet = matched;
    renderMeta(matched);
    await loadPet(matched.id);
    document.getElementById("refreshInfo").textContent = "更新于 " + new Date().toLocaleTimeString();
  } catch (err) {
    document.getElementById("err").textContent = "加载失败: " + err.message;
  }
}

document.getElementById("petSelect").addEventListener("change", async (e) => {
  const id = Number(e.target.value);
  const data = await api("/gm/pets");
  const pet = (data.pets || []).find(p => p.id === id);
  if (pet) {
    currentPet = pet;
    renderMeta(pet);
    await loadPet(id);
  }
});

document.querySelectorAll(".gm-btn").forEach(btn => {
  btn.addEventListener("click", async () => {
    if (!currentPet) return;
    const act = btn.dataset.act;
    const res = document.getElementById("gmResult");
    res.className = "gm-result";
    res.textContent = "执行中...";
    setBusy(true);
    try {
      let r;
      if (act === "speak") {
        const trig = document.getElementById("speakTrigger").value.trim();
        r = await apiPost("/gm/speak", { pet_id: currentPet.id, trigger: trig || undefined });
      } else if (act === "dream") {
        r = await apiPost("/gm/dream", { pet_id: currentPet.id, mark: document.getElementById("markDate").checked });
      } else if (act === "diary") {
        r = await apiPost("/gm/diary", { pet_id: currentPet.id, mark: document.getElementById("markDate").checked });
      } else if (act === "tick") {
        r = await apiPost("/gm/tick", { pet_id: currentPet.id });
      }
      res.className = "gm-result ok";
      res.textContent = "成功: " + JSON.stringify(r);
      await refreshAll(true);
    } catch (err) {
      res.className = "gm-result bad";
      res.textContent = "失败: " + err.message;
    } finally {
      setBusy(false);
    }
  });
});

document.getElementById("sendTextBtn").addEventListener("click", async () => {
  if (!currentPet) return;
  const input = document.getElementById("sendTextInput");
  const text = input.value.trim();
  const res = document.getElementById("textResult");
  if (!text) { res.className = "gm-result bad"; res.textContent = "请输入文字"; return; }
  res.className = "gm-result"; res.textContent = "发送中...";
  setBusy(true);
  try {
    await apiPost("/gm/text", { pet_id: currentPet.id, text });
    res.className = "gm-result ok"; res.textContent = "已发送";
    input.value = "";
    await refreshAll(true);
  } catch (err) {
    res.className = "gm-result bad"; res.textContent = "失败: " + err.message;
  } finally {
    setBusy(false);
  }
});

let pendingImageFile = null;
const imageDrop = document.getElementById("imageDrop");
const imageFile = document.getElementById("imageFile");
const imagePreview = document.getElementById("imagePreview");
const previewImg = document.getElementById("previewImg");
const previewMeta = document.getElementById("previewMeta");
const sendImageBtn = document.getElementById("sendImageBtn");
const clearImageBtn = document.getElementById("clearImageBtn");
const imageResult = document.getElementById("imageResult");

function setImageFile(file) {
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    imageResult.className = "gm-result bad"; imageResult.textContent = "不是图片文件: " + file.type;
    return;
  }
  if (file.size > 10 * 1024 * 1024) {
    imageResult.className = "gm-result bad"; imageResult.textContent = "图片不能超过 10MB";
    return;
  }
  pendingImageFile = file;
  const url = URL.createObjectURL(file);
  previewImg.src = url;
  previewMeta.textContent = (file.name || "paste.png") + " · " + (file.size / 1024).toFixed(1) + " KB";
  imagePreview.style.display = "block";
  sendImageBtn.disabled = false;
  clearImageBtn.style.display = "inline-block";
  imageResult.textContent = "";
}

function clearImage() {
  pendingImageFile = null;
  imagePreview.style.display = "none";
  previewImg.src = "";
  previewMeta.textContent = "";
  sendImageBtn.disabled = true;
  clearImageBtn.style.display = "none";
  imageFile.value = "";
  imageResult.textContent = "";
}

imageDrop.addEventListener("click", () => imageFile.click());
imageFile.addEventListener("change", () => { if (imageFile.files[0]) setImageFile(imageFile.files[0]); });
clearImageBtn.addEventListener("click", clearImage);

window.addEventListener("paste", (e) => {
  const items = (e.clipboardData || {}).items || [];
  for (const item of items) {
    if (item.type && item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (file) { setImageFile(file); e.preventDefault(); break; }
    }
  }
});

sendImageBtn.addEventListener("click", async () => {
  if (!currentPet || !pendingImageFile) return;
  imageResult.className = "gm-result"; imageResult.textContent = "上传并发送中...";
  sendImageBtn.disabled = true;
  try {
    const fd = new FormData();
    fd.append("file", pendingImageFile);
    fd.append("pet_id", String(currentPet.id));
    const r = await fetch(withToken("/gm/image"), { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || j.detail || r.status);
    imageResult.className = "gm-result ok"; imageResult.textContent = "发送成功 (key=" + j.image_key + ")";
    clearImage();
    await refreshAll(true);
  } catch (err) {
    imageResult.className = "gm-result bad"; imageResult.textContent = "失败: " + err.message;
    sendImageBtn.disabled = false;
  }
});

refreshAll();
setInterval(() => {
  const active = document.activeElement;
  if (!active || active.tagName !== "INPUT") {
    refreshAll(true);
  }
}, REFRESH_MS);
</script>
</body>
</html>
"""


@router.get("/web", response_class=HTMLResponse)
async def web_dashboard(req: Request):
    container = _container(req)
    if not container.config.gm_token:
        return JSONResponse({"error": "gm_disabled"}, status_code=403)
    token = req.query_params.get("token") or req.headers.get("X-GM-Token")
    if not hmac.compare_digest(token or "", container.config.gm_token):
        return JSONResponse({"error": "gm_unauthorized"}, status_code=401)
    return HTMLResponse(_DASHBOARD_HTML)
