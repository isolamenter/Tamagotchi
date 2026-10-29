"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  token: sessionStorage.getItem("dafeiyu-token") || "",
  rooms: [],
  selected: null,
  overview: null,
  view: "messages",
  filter: "all",
  generation: 0,
  detailRequest: 0,
  busy: false,
  refreshPromise: null,
  messages: new Map(),
  memories: new Map(),
  drafts: new Map(),
  hasMessages: false,
  hasMemories: false,
  messageSignature: "",
  journal: [],
  journalDate: "",
  journalSignature: "",
  journalImages: new Map(),
  imageURL: null,
  action: "",
  actionId: null,
  toastTimer: null,
};
const names = {
  reality: "真实互动",
  hearsay: "群友转述",
  imagination: "梦境想象",
};
const sourceNames = {
  proactive: "主动插话",
  dream: "梦境",
  diary: "日记",
  manual: "手动发送",
};
const journalKinds = { diary: "日记", dream: "梦境", "": "类型未记录" };
const roomName = (room) => room.title || `群聊 ${room.id}`;
const currentRoom = () =>
  state.rooms.find((room) => room.id === state.selected);
const draft = () => {
  if (!state.drafts.has(state.selected))
    state.drafts.set(state.selected, { text: "", file: null });
  return state.drafts.get(state.selected);
};
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
const time = (value) =>
  value
    ? new Date(value * 1000).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "还没有";
const clock = (value) =>
  new Date(value * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

async function api(path, { method = "GET", body, token = state.token } = {}) {
  const headers = { "X-GM-Token": token };
  if (body && !(body instanceof FormData))
    headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch("/gm/" + path, {
      method,
      headers,
      body:
        body instanceof FormData
          ? body
          : body
            ? JSON.stringify(body)
            : undefined,
      cache: "no-store",
    });
  } catch {
    throw new Error("连接中断，请检查网络后重试");
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("暂时无法读取服务响应，请稍后重试");
  }
  if (!response.ok) {
    if (
      response.status === 401 &&
      token === state.token &&
      !$("workspace").hidden
    )
      logout("管理口令已失效，请重新登录");
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "请求参数不正确，请检查后重试",
    );
  }
  return data;
}
function toast(message, error = false) {
  clearTimeout(state.toastTimer);
  $("toast").textContent = message;
  $("toast").className = "toast" + (error ? " error" : "");
  $("toast").hidden = false;
  state.toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 4500);
}
function setBusy(busy) {
  state.busy = busy;
  document
    .querySelectorAll(
      ".room-button, [data-action], #send-text, #rename-room, #logout, #mobile-logout",
    )
    .forEach((button) => {
      button.disabled = busy;
    });
  $("send-image").disabled = busy || !state.selected || !draft().file;
  $("confirm-action").disabled = busy;
  $("cancel-dialog").disabled = busy;
  $("close-dialog").disabled = busy;
  $("send-text").textContent = busy ? "操作进行中…" : "发送文字 ↗";
}
function logout(message = "") {
  state.token = "";
  sessionStorage.removeItem("dafeiyu-token");
  state.generation++;
  state.detailRequest++;
  state.selected = null;
  state.rooms = [];
  state.messages.clear();
  state.memories.clear();
  state.drafts.clear();
  clearJournal();
  if (state.imageURL) URL.revokeObjectURL(state.imageURL);
  state.imageURL = null;
  state.messageSignature = "";
  state.busy = false;
  $("workspace").hidden = true;
  $("login").hidden = false;
  $("login-error").textContent = message;
  $("access-token").value = "";
  $("action-dialog").close();
  $("messages").replaceChildren();
  $("memories").replaceChildren();
  $("message-draft").value = "";
  $("preview-image").removeAttribute("src");
  $("image-file").value = "";
}
async function login(token) {
  $("login-button").disabled = true;
  $("login-error").textContent = "";
  try {
    const overview = await api("overview", { token });
    state.token = token;
    state.overview = overview;
    state.generation++;
    sessionStorage.setItem("dafeiyu-token", token);
    $("access-token").value = "";
    $("login").hidden = true;
    $("workspace").hidden = false;
    renderOverview();
    await refresh();
  } catch (error) {
    logout(error.message);
  } finally {
    $("login-button").disabled = false;
  }
}
function renderOverview() {
  const overview = state.overview;
  if (!overview) return;
  $("version").textContent = overview.version;
  $("style-state").textContent = overview.style_ready
    ? "已就绪"
    : overview.style_failed
      ? "初始化失败"
      : "准备中";
  $("memory-index-state").textContent = overview.memory_index.pending
    ? `${overview.memory_index.pending} 段待构建`
    : "已就绪";
  for (const event of overview.scheduled_events) {
    const label = $(`${event.kind}-schedule`);
    if (label)
      label.textContent = `${event.hour}:00，${event.kind === "dream" ? "讲点梦里的事" : "发布每日整理的日记"}`;
  }
}
function renderRooms() {
  const query = $("room-search").value.trim().toLowerCase();
  const rooms = state.rooms.filter((room) =>
    `${roomName(room)} ${room.chat_id}`.toLowerCase().includes(query),
  );
  $("room-count").textContent = state.rooms.length;
  $("rooms").innerHTML = rooms.length
    ? rooms
        .map(
          (room) =>
            `<button class="room-button${room.id === state.selected ? " active" : ""}" data-room="${room.id}" aria-pressed="${room.id === state.selected}" ${state.busy ? "disabled" : ""}><span class="room-avatar">${esc(roomName(room).slice(0, 1))}</span><span><strong>${esc(roomName(room))}</strong><small>${room.message_count} 条消息 · ${room.memory_count} 段记忆</small></span></button>`,
        )
        .join("")
    : '<p class="empty-content">没有匹配的群聊</p>';
}
function renderRoomInfo() {
  const room = currentRoom();
  if (!room) return;
  $("room-title").textContent = roomName(room);
  $("breadcrumb-room").textContent = roomName(room);
  $("send-target").textContent = `${roomName(room)} · ${room.chat_id}`;
  $("chat-id").textContent = room.chat_id;
  $("chat-id").title = room.chat_id;
  $("total-messages").textContent = room.message_count.toLocaleString();
  $("total-memories").textContent = room.memory_count.toLocaleString();
  $("last-reply").textContent = time(room.state.last_reply_ts);
  $("last-proactive").textContent = time(room.state.last_proactive_ts);
  $("last-topic").textContent =
    room.state.last_proactive_topic || "还没主动开过口";
}
function saveDraft() {
  if (state.selected) draft().text = $("message-draft").value;
}
function restoreDraft() {
  $("message-draft").value = draft().text;
  renderImage();
}
async function selectRoom(id) {
  if (state.busy || id === state.selected) return;
  saveDraft();
  state.selected = id;
  state.generation++;
  state.detailRequest++;
  state.messages.clear();
  state.memories.clear();
  clearJournal();
  state.messageSignature = "";
  state.hasMessages = state.hasMemories = false;
  $("workspace-error").hidden = true;
  $("new-messages").hidden = true;
  renderRooms();
  renderRoomInfo();
  restoreDraft();
  $("messages").innerHTML = '<div class="empty-content">正在载入消息…</div>';
  $("memories").innerHTML = '<div class="empty-content">正在载入记忆…</div>';
  $("journal").innerHTML = '<div class="empty-content">正在载入日记梦境…</div>';
  document.querySelector(".inspector").classList.remove("open");
  $("toggle-info").setAttribute("aria-expanded", "false");
  await loadDetails(id, true);
}
function renderMessages({ first = false, older = false, force = false } = {}) {
  const messages = [...state.messages.values()].sort((a, b) => a.id - b.id);
  const visible = messages.filter(
    (message) =>
      state.filter === "all" ||
      (state.filter === "assistant" && message.role === "assistant") ||
      (state.filter === "observer" && message.is_observer) ||
      (state.filter === "direct" &&
        message.role === "user" &&
        !message.is_observer),
  );
  $("message-summary").textContent = `${visible.length} 条已加载`;
  const signature = JSON.stringify([state.filter, state.hasMessages, visible]);
  if (signature === state.messageSignature && !force) return;
  state.messageSignature = signature;
  const box = $("messages");
  const nearBottom = box.scrollHeight - box.clientHeight - box.scrollTop < 80;
  const oldTop = box.scrollTop,
    oldHeight = box.scrollHeight;
  let date = "";
  let html = state.hasMessages
    ? '<button id="older-messages" class="load-more">加载更早的消息</button>'
    : "";
  html += visible.length
    ? visible
        .map((message) => {
          const day = new Date(message.ts * 1000).toLocaleDateString("zh-CN", {
            month: "long",
            day: "numeric",
            weekday: "short",
          });
          let separator = "";
          if (day !== date) {
            date = day;
            separator = `<div class="day-separator">${esc(day)}</div>`;
          }
          const bot = message.role === "assistant";
          const name = bot
            ? state.overview?.name || "大肥鱼"
            : message.sender_name || "群友";
          return (
            separator +
            `<article class="message-row${bot ? " assistant" : message.is_observer ? " observer" : ""}" data-message-id="${message.id}"><div class="message-avatar">${bot ? '<img src="/static/character.png" alt="">' : esc(name.slice(0, 1))}</div><div class="message-body"><div class="message-meta"><strong>${esc(name)}</strong>${message.is_observer ? '<span class="source-tag">旁听</span>' : ""}${sourceNames[message.source] ? `<span class="source-tag">${sourceNames[message.source]}</span>` : ""}<time>${clock(message.ts)}</time></div><div class="message-text">${esc(message.content)}</div>${message.meme_tag ? '<div class="subtle">附带一张梗图</div>' : ""}</div></article>`
          );
        })
        .join("")
    : '<div class="empty-content"><strong>这里还很安静。</strong><span>这个筛选下暂时没有消息。</span></div>';
  box.innerHTML = html;
  if (first || (nearBottom && !older)) {
    box.scrollTop = box.scrollHeight;
    $("new-messages").hidden = true;
  } else if (older) box.scrollTop = oldTop + box.scrollHeight - oldHeight;
  else {
    box.scrollTop = oldTop;
    $("new-messages").hidden = false;
  }
}
function renderMemories() {
  const query = $("memory-search").value.trim().toLowerCase(),
    kind = $("memory-kind").value;
  const memories = [...state.memories.values()]
    .sort((a, b) => b.id - a.id)
    .filter(
      (memory) =>
        (kind === "all" || memory.kind === kind) &&
        `${memory.who} ${memory.what} ${memory.hooks}`
          .toLowerCase()
          .includes(query),
    );
  $("memories").innerHTML = memories.length
    ? memories
        .map(
          (memory) =>
            `<article class="memory-item"><div class="memory-item-top"><span class="kind-badge ${["imagination", "hearsay"].includes(memory.kind) ? memory.kind : ""}">${names[memory.kind] || "真实互动"}</span><span>${esc(memory.when || time(memory.created_at))}</span></div><div class="memory-who">${esc(memory.who || "群里")}</div><p class="memory-content">${esc(memory.what)}</p>${memory.vibe ? `<p class="memory-vibe">${esc(memory.vibe)}</p>` : ""}</article>`,
        )
        .join("")
    : '<div class="empty-content"><strong>暂时没有这类记忆。</strong><span>值得留下的人和事，会慢慢记在这里。</span></div>';
  $("older-memories").hidden = !state.hasMemories;
}
function clearJournal() {
  for (const image of state.journalImages.values())
    image.then((result) => result.url && URL.revokeObjectURL(result.url));
  state.journalImages.clear();
  state.journal = [];
  state.journalDate = "";
  state.journalSignature = "";
}
function journalStatus(entry) {
  if (entry.status === "draft")
    return entry.ready
      ? "草稿 · 当日整理已完成，没有发送记录"
      : "草稿 · 当日整理尚未完成，没有发送记录";
  return entry.card ? "已发送（卡片）" : "已发送";
}
function renderJournal() {
  const dates = [...new Set(state.journal.map((entry) => entry.date))]
    .sort()
    .reverse();
  if (!dates.includes(state.journalDate)) state.journalDate = dates[0] || "";
  $("journal-date").innerHTML = dates
    .map(
      (date) =>
        `<option value="${esc(date)}"${date === state.journalDate ? " selected" : ""}>${esc(date)}</option>`,
    )
    .join("");
  $("journal-date").disabled = !dates.length;
  $("journal-summary").textContent = dates.length
    ? `${dates.length} 天有记录`
    : "";
  const entries = state.journal.filter(
    (entry) => entry.date === state.journalDate,
  );
  $("journal").innerHTML = entries.length
    ? entries
        .map(
          (entry) =>
            `<article class="memory-item"><div class="memory-item-top"><span class="kind-badge ${entry.kind === "dream" ? "imagination" : entry.kind ? "" : "hearsay"}">${journalKinds[entry.kind] ?? "类型未记录"}</span><span>${esc(entry.ts ? time(entry.ts) : entry.date)}</span></div><div class="memory-who">${esc(journalStatus(entry))}</div><p class="memory-content">${esc(entry.text)}</p>${entry.card?.has_image ? `<div class="journal-image" data-card="${esc(entry.card.card_id)}">正在读取配图…</div>` : entry.kind === "dream" ? '<p class="memory-vibe">没有配图记录</p>' : ""}</article>`,
        )
        .join("")
    : '<div class="empty-content"><strong>还没有日记或梦境。</strong><span>发送过的日记、梦境和当前日记草稿会出现在这里。</span></div>';
  document.querySelectorAll("#journal [data-card]").forEach(showJournalImage);
}
async function fetchJournalImage(id, cardId) {
  try {
    const response = await fetch(
      `/gm/journal/image?conversation_id=${id}&card_id=${encodeURIComponent(cardId)}`,
      { headers: { "X-GM-Token": state.token }, cache: "no-store" },
    );
    if (!response.ok) {
      const detail = await response.json().then(
        (data) => data.detail,
        () => "",
      );
      return {
        error: typeof detail === "string" && detail ? detail : "读取失败",
      };
    }
    return { url: URL.createObjectURL(await response.blob()) };
  } catch {
    return { error: "连接中断，请稍后刷新重试" };
  }
}
async function showJournalImage(box) {
  const cardId = box.dataset.card;
  if (!state.journalImages.has(cardId))
    state.journalImages.set(cardId, fetchJournalImage(state.selected, cardId));
  const result = await state.journalImages.get(cardId);
  if (!box.isConnected) return;
  box.innerHTML = result.url
    ? `<img src="${result.url}" alt="梦境配图">`
    : `配图无法显示：${esc(result.error)}（只保存了飞书图片标识）`;
}
async function loadJournal(id) {
  const generation = state.generation;
  try {
    const data = await api(`journal?conversation_id=${id}`);
    if (id !== state.selected || generation !== state.generation) return;
    // Polling must not re-render (and flash images) when nothing changed.
    const signature = JSON.stringify(data.entries);
    if (signature === state.journalSignature) return;
    state.journalSignature = signature;
    state.journal = data.entries;
    renderJournal();
  } catch (error) {
    if (id !== state.selected || generation !== state.generation) return;
    state.journalSignature = "";
    $("journal").innerHTML =
      `<div class="empty-content">${esc(error.message)}</div>`;
  }
}
async function loadDetails(id, first = false) {
  if (state.view === "journal") loadJournal(id);
  const generation = state.generation,
    request = ++state.detailRequest;
  try {
    const [messages, memories] = await Promise.all([
      api(`messages?conversation_id=${id}&limit=60`),
      api(`memories?conversation_id=${id}&limit=50`),
    ]);
    if (
      id !== state.selected ||
      generation !== state.generation ||
      request !== state.detailRequest
    )
      return;
    messages.messages.forEach((message) =>
      state.messages.set(message.id, message),
    );
    memories.memories.forEach((memory) =>
      state.memories.set(memory.id, memory),
    );
    if (first) {
      state.hasMessages = messages.has_more;
      state.hasMemories = memories.has_more;
    }
    renderMessages({ first });
    renderMemories();
    $("workspace-error").hidden = true;
  } catch (error) {
    if (
      id !== state.selected ||
      generation !== state.generation ||
      request !== state.detailRequest
    )
      return;
    $("workspace-error").textContent = error.message;
    $("workspace-error").hidden = false;
    if (first) {
      $("messages").innerHTML =
        '<div class="empty-content">消息加载失败，请点击上方刷新重试。</div>';
      $("memories").innerHTML =
        '<div class="empty-content">记忆加载失败，请点击上方刷新重试。</div>';
    }
  }
}
async function refresh() {
  if (!state.token) return;
  if (state.refreshPromise) return state.refreshPromise;
  const generation = state.generation,
    token = state.token;
  $("refresh").disabled = true;
  state.refreshPromise = (async () => {
    try {
      const [rooms, overview] = await Promise.all([
        api("conversations"),
        api("overview"),
      ]);
      if (token !== state.token) return;
      state.rooms = rooms.conversations;
      state.overview = overview;
      renderOverview();
      renderRooms();
      $("no-rooms").hidden = !!state.rooms.length;
      $("room-workspace").hidden = !state.rooms.length;
      if (state.rooms.length) {
        if (!state.rooms.some((room) => room.id === state.selected)) {
          state.selected = null;
          await selectRoom(state.rooms[0].id);
        } else if (generation === state.generation) {
          renderRoomInfo();
          await loadDetails(state.selected);
        }
      }
      if (token !== state.token) return;
      $("connection-status").textContent = "已连接";
      $("connection-status").className = "status-pill";
      $("last-refresh").textContent =
        "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
    } catch (error) {
      if (token !== state.token) return;
      $("connection-status").textContent = "连接中断";
      $("connection-status").className = "status-pill offline";
      $("workspace-error").textContent = error.message;
      $("workspace-error").hidden = false;
    } finally {
      state.refreshPromise = null;
      $("refresh").disabled = false;
    }
  })();
  return state.refreshPromise;
}
async function afterAction() {
  if (state.refreshPromise) await state.refreshPromise;
  await refresh();
}
function setView(view) {
  state.view = view;
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
    if (button.dataset.view === view)
      button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  for (const name of ["messages", "memories", "journal", "send"])
    $(`view-${name}`).hidden = view !== name;
  if (view === "messages") renderMessages({ force: true });
  if (view === "journal" && state.selected) loadJournal(state.selected);
}
function renderImage() {
  if (state.imageURL) URL.revokeObjectURL(state.imageURL);
  state.imageURL = null;
  const file = draft().file;
  $("image-preview").hidden = !file;
  if (file) {
    state.imageURL = URL.createObjectURL(file);
    $("preview-image").src = state.imageURL;
    $("image-name").textContent = file.name;
    $("image-size").textContent = (file.size / 1024).toFixed(1) + " KB";
  } else {
    $("preview-image").removeAttribute("src");
    $("image-file").value = "";
  }
  $("send-image").disabled = state.busy || !file;
}
function setImage(file) {
  if (!file || !state.selected || state.busy) return;
  if (
    ![
      "image/png",
      "image/jpeg",
      "image/gif",
      "image/webp",
      "image/bmp",
    ].includes(file.type)
  ) {
    toast("请选择 PNG、JPEG、GIF、WebP 或 BMP 图片", true);
    return;
  }
  if (!file.size || file.size > 10 * 1024 * 1024) {
    toast("图片不能为空且不能超过 10 MB", true);
    return;
  }
  draft().file = file;
  renderImage();
}
function openAction(action) {
  const room = currentRoom();
  if (!room || state.busy) return;
  state.action = action;
  state.actionId = room.id;
  const titles = {
    speak: "让她接一句",
    dream: "讲个梦",
    diary: "发送今天的日记",
    tick: "检查开口时机",
    "tick-all": "检查所有会话",
    rename: "给群聊起个名字",
  };
  $("action-title").textContent = titles[action];
  $("action-description").textContent =
    action === "rename"
      ? "名称只用于这个工作台，飞书群名不会改变。"
      : action === "tick" || action === "tick-all"
        ? "按当前的静默、冷却和群聊内容判断，满足条件时会向群聊发言。"
        : action === "diary"
          ? "发送每日整理已保存的当日日记；尚未生成或没有合适内容时不会发送。"
          : "内容生成后，会直接以大肥鱼的身份发送到群聊。";
  $("action-target").textContent =
    action === "tick-all"
      ? `目标：全部 ${state.rooms.length} 个会话`
      : `目标：${roomName(room)} · ${room.chat_id}`;
  $("trigger-field").hidden = action !== "speak";
  $("rename-field").hidden = action !== "rename";
  $("mark-field").hidden = !["dream", "diary"].includes(action);
  $("mark-date").checked = false;
  $("action-trigger").value = "";
  $("room-name").value = room.title || "";
  $("confirm-action").textContent =
    action === "rename"
      ? "保存名称"
      : action.startsWith("tick")
        ? "确认检查"
        : "确认发送";
  $("action-error").textContent = "";
  $("action-dialog").showModal();
}

$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const token = $("access-token").value.trim();
  if (token) login(token);
});
$("logout").addEventListener("click", () => logout());
$("mobile-logout").addEventListener("click", () => logout());
$("rooms").addEventListener("click", (event) => {
  const button = event.target.closest("[data-room]");
  if (button) selectRoom(Number(button.dataset.room));
});
$("room-search").addEventListener("input", renderRooms);
$("refresh").addEventListener("click", refresh);
document
  .querySelectorAll("[data-view]")
  .forEach((button) =>
    button.addEventListener("click", () => setView(button.dataset.view)),
  );
document.querySelectorAll("[data-filter]").forEach((button) =>
  button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    document
      .querySelectorAll("[data-filter]")
      .forEach((b) => b.classList.toggle("selected", b === button));
    renderMessages({ force: true });
  }),
);
$("memory-search").addEventListener("input", renderMemories);
$("memory-kind").addEventListener("change", renderMemories);
$("journal-date").addEventListener("change", () => {
  state.journalDate = $("journal-date").value;
  renderJournal();
});
$("messages").addEventListener("scroll", () => {
  const box = $("messages");
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 80)
    $("new-messages").hidden = true;
});
$("new-messages").addEventListener("click", () => {
  $("messages").scrollTop = $("messages").scrollHeight;
  $("new-messages").hidden = true;
});
$("messages").addEventListener("click", async (event) => {
  if (!event.target.closest("#older-messages")) return;
  const id = state.selected,
    generation = state.generation,
    button = $("older-messages");
  button.disabled = true;
  try {
    const response = await api(
      `messages?conversation_id=${id}&limit=60&before_id=${Math.min(...state.messages.keys())}`,
    );
    if (id !== state.selected || generation !== state.generation) return;
    response.messages.forEach((row) => state.messages.set(row.id, row));
    state.hasMessages = response.has_more;
    renderMessages({ older: true });
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
});
$("older-memories").addEventListener("click", async () => {
  const id = state.selected,
    generation = state.generation;
  $("older-memories").disabled = true;
  try {
    const response = await api(
      `memories?conversation_id=${id}&limit=50&before_id=${Math.min(...state.memories.keys())}`,
    );
    if (id !== state.selected || generation !== state.generation) return;
    response.memories.forEach((row) => state.memories.set(row.id, row));
    state.hasMemories = response.has_more;
    renderMemories();
  } catch (error) {
    toast(error.message, true);
  } finally {
    $("older-memories").disabled = false;
  }
});
$("message-draft").addEventListener("input", saveDraft);
$("message-draft").addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    $("send-form").requestSubmit();
  }
});
$("send-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.selected || state.busy) return;
  saveDraft();
  const id = state.selected,
    text = draft().text.trim();
  if (!text) {
    toast("先写点要说的话", true);
    $("message-draft").focus();
    return;
  }
  setBusy(true);
  try {
    await api("text", { method: "POST", body: { conversation_id: id, text } });
    if (state.drafts.get(id)?.text.trim() === text) {
      state.drafts.get(id).text = "";
      if (id === state.selected) $("message-draft").value = "";
    }
    toast("文字已发送");
    await afterAction();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(false);
  }
});
$("image-file").addEventListener("change", (event) =>
  setImage(event.target.files[0]),
);
$("clear-image").addEventListener("click", () => {
  if (!state.busy) {
    draft().file = null;
    renderImage();
  }
});
$("image-drop").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    $("image-file").click();
  }
});
for (const name of ["dragenter", "dragover"])
  $("image-drop").addEventListener(name, (event) => {
    event.preventDefault();
    $("image-drop").classList.add("dragover");
  });
for (const name of ["dragleave", "drop"])
  $("image-drop").addEventListener(name, (event) => {
    event.preventDefault();
    $("image-drop").classList.remove("dragover");
    if (name === "drop") setImage(event.dataTransfer.files[0]);
  });
window.addEventListener("paste", (event) => {
  if (state.view !== "send" || $("action-dialog").open || !state.token) return;
  for (const item of event.clipboardData?.items || [])
    if (item.type.startsWith("image/")) {
      setImage(item.getAsFile());
      event.preventDefault();
      break;
    }
});
$("send-image").addEventListener("click", async () => {
  if (!state.selected || state.busy || !draft().file) return;
  const id = state.selected,
    file = draft().file,
    body = new FormData();
  body.append("conversation_id", id);
  body.append("file", file);
  setBusy(true);
  try {
    await api("image", { method: "POST", body });
    if (state.drafts.get(id)?.file === file) state.drafts.get(id).file = null;
    renderImage();
    toast("图片已发送");
    await afterAction();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(false);
  }
});
document
  .querySelectorAll("[data-action]")
  .forEach((button) =>
    button.addEventListener("click", () => openAction(button.dataset.action)),
  );
$("rename-room").addEventListener("click", () => openAction("rename"));
$("close-dialog").addEventListener("click", () => {
  if (!state.busy) $("action-dialog").close();
});
$("cancel-dialog").addEventListener("click", () => $("action-dialog").close());
$("action-dialog").addEventListener("cancel", (event) => {
  if (state.busy) event.preventDefault();
});
$("action-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  const action = state.action,
    id = state.actionId;
  setBusy(true);
  $("action-error").textContent = "";
  try {
    let response;
    if (action === "rename")
      response = await api(`conversations/${id}`, {
        method: "PATCH",
        body: { title: $("room-name").value.trim() },
      });
    else {
      const body = action === "tick-all" ? {} : { conversation_id: id };
      if (action === "speak") body.trigger = $("action-trigger").value;
      if (["dream", "diary"].includes(action))
        body.mark = $("mark-date").checked;
      response = await api(action, { method: "POST", body });
    }
    $("action-dialog").close();
    toast(response.message);
    await afterAction();
  } catch (error) {
    $("action-error").textContent = error.message;
  } finally {
    setBusy(false);
  }
});
$("toggle-info").addEventListener("click", () => {
  if (!currentRoom()) return;
  const open = document.querySelector(".inspector").classList.toggle("open");
  $("toggle-info").setAttribute("aria-expanded", String(open));
});
$("close-inspector").addEventListener("click", () => {
  document.querySelector(".inspector").classList.remove("open");
  $("toggle-info").setAttribute("aria-expanded", "false");
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.token) refresh();
});
setInterval(() => {
  if (!document.hidden && state.token) refresh();
}, 15000);
// Tokens are never accepted from or retained in a URL.
if (location.search)
  history.replaceState(null, "", location.pathname + location.hash);
if (state.token) login(state.token);
