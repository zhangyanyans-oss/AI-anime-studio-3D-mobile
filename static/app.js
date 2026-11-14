/* AI 动画工作台 · 手机版逻辑
 * 数据全部存云端 SQLite（/api/projects），浏览器只是遥控器。
 * 生成链路：头脑风暴/点子 → /api/generate-story → 分镜逐条 /api/generate-image
 *          → /api/generate-video + 轮询 /api/video-status → /api/save-video
 */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// 头脑风暴对话框 Markdown 渲染：breaks 让单个换行也生效，gfm 支持表格/删除线；
// 表格外包一层可横向滚动容器，避免小屏把气泡撑爆
marked.setOptions({ breaks: true, gfm: true });
const md = (s) =>
  DOMPurify.sanitize(marked.parse(String(s || "")))
    .replace(/<table>/g, '<div class="md-table-wrap"><table>')
    .replace(/<\/table>/g, "</table></div>");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const S = {
  code: localStorage.getItem("access_code") || "",
  role: localStorage.getItem("access_role") || "",      // admin | partition
  zone: localStorage.getItem("access_zone") || "",      // 分区显示名
  projects: [],
  cur: null,          // {id, name, data}
  tab: "story",
  assetTab: "characters",
  manage: false,
  saveTimer: null,
  dirty: false,
  pollers: {},        // boardId -> intervalId
  pollBusy: {},       // boardId -> true 表示该分镜的 pollOnce 正在异步执行中（防重入）
  chatImages: [],     // 头脑风暴待发送图片（dataURL）
  refTarget: null,    // 参考图上传目标 {kind, index}
  voiceTarget: null,  // 音色参考音频上传目标 {kind, index}
  voiceReady: null,   // 配音服务状态：null=未探测 true=在线 false=不可用
  voiceWarming: false, // 模型预热中（在线版首次部署下载模型期间）
};

/* ================= 网络层 ================= */
function xhrApi(path, body, timeoutMs) {
  // 用 XMLHttpRequest 提交：与 fetch 走浏览器内部不同代码路径。
  // 实测某些环境下 fetch 对该请求会静默挂死（不建连、不报错、不超时），XHR 可绕开。
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path, true);
    xhr.setRequestHeader("Content-Type", "application/json");
    xhr.setRequestHeader("x-access-code", S.code || "");
    xhr.timeout = timeoutMs || 180000;
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status === 401) { showLogin(); return reject(new Error("需要访问口令")); }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(data);
      reject(new Error(data.detail || `请求失败（HTTP ${xhr.status}）`));
    };
    xhr.onerror = () => reject(new Error("无法连接服务（网络错误），请确认服务已启动、地址正确"));
    xhr.ontimeout = () => reject(new Error(`请求超时（${Math.round(xhr.timeout / 1000)} 秒无响应）：请确认服务在运行；手机访问请用电脑的局域网 IP（如 192.168.x.x:8016），不能是 127.0.0.1`));
    xhr.send(JSON.stringify(body));
  });
}
async function api(path, body, method, timeoutMs) {
  method = method || (body !== undefined ? "POST" : "GET");
  const headers = { "x-access-code": S.code };
  if (body !== undefined) headers["content-type"] = "application/json";
  const opts = { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined };
  // 超时保护：请求挂死（防火墙拦截/服务退出/代理异常）时不再让界面永远停在"渲染中"
  if (timeoutMs > 0 && typeof AbortSignal !== "undefined" && AbortSignal.timeout) {
    opts.signal = AbortSignal.timeout(timeoutMs);
  }
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    if (e && (e.name === "TimeoutError" || e.name === "AbortError")) {
      throw new Error(`请求超时（${Math.round(timeoutMs / 1000)} 秒无响应）：请确认服务在运行；手机访问请用电脑的局域网 IP（如 192.168.x.x:8016），不能是 127.0.0.1`);
    }
    throw new Error(`无法连接服务（${e.message || e}）：请确认服务已启动、地址正确`);
  }
  if (resp.status === 401) { showLogin(); throw new Error("需要访问口令"); }
  let data = {};
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) {
    const err = new Error(data.detail || `请求失败（HTTP ${resp.status}）`);
    err.status = resp.status;   // 409 版本冲突等场景需要按状态码分支处理
    err.data = data;
    throw err;
  }
  return data;
}

/* ================= 遮罩 / 提示 ================= */
let toastTimer = null;
function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), isErr ? 5000 : 2600);
}
function showBusy(text, sub) {
  $("busy-text").textContent = text || "生成中…";
  $("busy-sub").textContent = sub || "";
  $("busy-mask").classList.remove("hidden");
}
function hideBusy() { $("busy-mask").classList.add("hidden"); }

/* ================= 登录 ================= */
function showLogin() {
  $("login-screen").classList.remove("hidden");
  $("home-screen").classList.add("hidden");
  $("studio-screen").classList.add("hidden");
}
async function doLogin() {
  const code = $("login-code").value.trim();
  if (!code) return;
  try {
    const r = await api("/api/login", { code });
    S.code = code;
    S.role = r.role || "partition";
    S.zone = r.zone || "";
    localStorage.setItem("access_code", code);
    localStorage.setItem("access_role", S.role);
    localStorage.setItem("access_zone", S.zone);
    $("login-screen").classList.add("hidden");
    $("login-err").classList.add("hidden");
    await loadHome();
    showZoneBadge();
    $("home-screen").classList.remove("hidden");
  } catch (e) {
    $("login-err").classList.remove("hidden");
  }
}
/* 顶栏显示当前分区身份（管理员金色，分区紫色） */
function showZoneBadge() {
  const el = $("zone-badge");
  if (!el) return;
  if (!S.code) { el.classList.add("hidden"); return; }
  el.textContent = S.role === "admin" ? "管理员" : (S.zone || "分区");
  el.classList.toggle("zone-admin", S.role === "admin");
  el.classList.remove("hidden");
}
/* 退出当前账户：清口令回到登录页（云端数据不动，换口令登录即切换分区） */
function logoutUser() {
  if (!confirm("退出当前账户？\n项目数据都保存在云端，换口令登录即可进入对应分区。")) return;
  ["access_code", "access_role", "access_zone"].forEach((k) => localStorage.removeItem(k));
  location.reload();
}
$("login-code") && $("login-code").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doLogin();
});

/* ================= 首页 ================= */
async function loadHome() {
  const r = await api("/api/projects");
  S.projects = r.projects || [];
  renderHome();
}
function renderHome() {
  const list = $("project-list");
  $("btn-manage").textContent = S.manage ? "完成" : "⋯";
  if (!S.projects.length) {
    list.innerHTML = `<div class="card pad" style="text-align:center;color:var(--muted);font-size:13.5px;">
      还没有项目<br>点上面的「新建动画项目」开始创作吧</div>`;
    return;
  }
  list.innerHTML = S.projects.map((p) => `
    <div class="project-card" onclick="openProject('${p.id}')">
      <div class="project-cover">${p.cover ? `<img src="${p.cover}" loading="lazy">` : "🎬"}</div>
      <div class="project-meta">
        <div class="project-name">${esc(p.name)}</div>
        <div class="project-sub">${p.board_count || 0} 个分镜 · ${fmtRel(p.updated_at)}${p.owner && p.owner !== S.code && p.owner_label ? ` · ${esc(p.owner_label)}` : ""}</div>
      </div>
      ${S.manage ? `<button class="proj-del" onclick="event.stopPropagation();deleteProject('${p.id}')">🗑</button>` : ""}
    </div>`).join("");
}
function toggleManage() { S.manage = !S.manage; renderHome(); }
function fmtRel(ts) {
  if (!ts) return "";
  const d = new Date(ts), now = Date.now(), diff = now - ts;
  if (diff < 60000) return "刚刚";
  if (diff < 3600000) return Math.floor(diff / 60000) + " 分钟前";
  const pad = (n) => String(n).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (diff < 86400000) return "今天 " + hm;
  return `${d.getMonth() + 1}-${d.getDate()} ${hm}`;
}
function showNewProject() {
  openSheet(`
    <div class="sheet-title">新建动画项目</div>
    <div class="sheet-sub">先起个名字，随时可以改</div>
    <input id="np-name" type="text" placeholder="例如：橘猫大侠" maxlength="30">
    <button class="btn btn-primary btn-block" onclick="createNew()">创建</button>`);
  setTimeout(() => $("np-name").focus(), 250);
}
async function createNew() {
  const name = ($("np-name").value || "").trim() || "未命名项目";
  try {
    const r = await api("/api/projects", { name, data: { name, story: null, boards: [] } });
    closeSheet();
    await openProject(r.id);
  } catch (e) { toast(e.message, true); }
}
async function deleteProject(id) {
  const p = S.projects.find((x) => x.id === id);
  if (!confirm(`确定删除项目「${p?.name || id}」？\n项目数据删除后无法恢复，项目里的图片/视频文件也将从服务器一并删除。`)) return;
  try {
    await api(`/api/projects/${id}`, undefined, "DELETE");
    toast("已删除");
    await loadHome();
  } catch (e) { toast(e.message, true); }
}

/* ================= 项目打开 / 保存 ================= */
function normalizeData(d) {
  // 兼容自愈：老版本后端曾把字段存成字符串（images:"[]" / videoUrl:"None"），会导致
  // <video src="None"> 报「视频不存在」、分镜渲染崩溃。打开项目时统一修正。
  if (!d || typeof d !== "object") return d;
  const cleanStr = (v) => (v === "None" || v === "null" || v === "" ? null : v);
  if (Array.isArray(d.boards)) d.boards = d.boards.map((b) => {
    if (!b || typeof b !== "object") return b;
    if (!Array.isArray(b.images)) {
      let imgs = b.images;
      if (typeof imgs === "string") { try { imgs = JSON.parse(imgs); } catch (_) { imgs = []; } }
      b.images = Array.isArray(imgs) ? imgs.filter((u) => typeof u === "string" && u) : [];
    }
    b.videoUrl = cleanStr(b.videoUrl);
    b.videoTaskId = cleanStr(b.videoTaskId);
    b.videoStatus = cleanStr(b.videoStatus);
    b.videoError = cleanStr(b.videoError);
    // 多版本视频列表：老数据只有 videoUrl（单条），迁移进 videos；videoUrl 继续作为
    // 「最新成片」别名保留——轮询短路（!videoUrl）、下载等旧逻辑全部依赖它，保持行为不变
    if (!Array.isArray(b.videos)) b.videos = [];
    b.videos = [...new Set(b.videos.filter((u) => typeof u === "string" && u))]; // 过滤脏值并去重（历史上轮询竞态产生过重复条目）
    if (b.videoUrl && !b.videos.includes(b.videoUrl)) b.videos.push(b.videoUrl);
    // 配音列表：[{url, voice}]，老数据无此字段自愈为空数组，脏值过滤
    if (!Array.isArray(b.dubs)) b.dubs = [];
    b.dubs = b.dubs.filter((x) => x && typeof x === "object" && typeof x.url === "string" && x.url);
    b.topView = cleanStr(b.topView);
    b.trajView = cleanStr(b.trajView);
    b.videoSubmitting = false; // 运行时标志：数据是刚从库里读出来的，旧的提交请求已不存在
    if (b.videoStatus === "running" && !b.videoTaskId) b.videoStatus = "failed";
    return b;
  });
  if (!Array.isArray(d.boards)) d.boards = [];
  return d;
}
async function openProject(id) {
  try {
    const r = await api(`/api/projects/${id}`);
    S.cur = { id: r.id, name: r.name, data: normalizeData(r.data || {}), base: r.updated_at };
    S.cur.data.boards ||= [];
    S.cur.data.name ||= r.name;
    $("studio-name").textContent = S.cur.name;
    setSaveState("");
    $("home-screen").classList.add("hidden");
    $("studio-screen").classList.remove("hidden");
    resumePollers(true); // 项目刚打开：旧的提交请求已死，按刷新口径清理
    setTab("story");
  } catch (e) { toast(e.message, true); }
}
function backHome() {
  saveNow();
  $("studio-screen").classList.add("hidden");
  $("home-screen").classList.remove("hidden");
  loadHome().catch(() => {});
}
function setSaveState(text, saving) {
  const el = $("save-state");
  el.textContent = text;
  el.className = "save-state" + (saving ? " saving" : "");
}
function touch() {
  if (!S.cur) return;
  S.dirty = true;
  setSaveState("保存中…", true);
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(saveNow, 1200);
}
async function saveNow() {
  if (!S.cur || !S.dirty) return;
  if (S.saving) { S.pendingSave = true; return; }  // 上一笔还在路上：稍后补发，避免并发保存用过期基准互相顶掉
  S.saving = true;
  clearTimeout(S.saveTimer);
  // 落库瘦身：聊天附图的 base64 dataURL 只用于当次展示，不写进数据库（体积太大）
  let payload = S.cur.data;
  try {
    payload = JSON.parse(JSON.stringify(S.cur.data));
    if (payload.draft && payload.draft.messages) {
      for (const m of payload.draft.messages) { delete m.image; delete m.images; }
    }
  } catch (_) {}
  try {
    // 带 base（打开项目/上次保存时的云端 updated_at）：云端若已被其他设备更新，
    // 服务端拒绝写入返回 409，客户端拉取最新版——过期页面永远覆盖不了新数据
    const r = await api(`/api/projects/${S.cur.id}`,
      { name: S.cur.name, data: payload, base_updated_at: S.cur.base });
    S.cur.base = r.updated_at;   // 更新版本戳，下一笔保存以它为基准
    S.dirty = false;
    setSaveState("已保存 " + new Date(r.updated_at).toTimeString().slice(0, 5));
  } catch (e) {
    if (e.status === 409) {
      // 版本冲突：线上已有更新的版本（别的设备刚保存过）→ 放弃本地过期数据，加载云端最新版
      S.dirty = false;
      setSaveState("");
      await syncFromCloud(true);
    } else {
      setSaveState("保存失败，稍后重试");
    }
  } finally {
    S.saving = false;
    if (S.dirty || S.pendingSave) { S.pendingSave = false; setTimeout(saveNow, 400); }
  }
}

/* ================= 多端实时同步 =================
   规则：任何设备打开链接 → 拉取线上最新版；编辑基于最新版；
   切回前台（手机/PC 标签页重新可见）→ 若无未保存改动，静默拉取云端最新；
   保存冲突（云端更新）→ 放弃本地过期副本，以线上最新为准。 */
async function syncFromCloud(isConflict) {
  if (!S.cur) return;
  try {
    const r = await api(`/api/projects/${S.cur.id}`);
    if (r.updated_at === S.cur.base) return;          // 没有新版本，什么都不动
    const changed = JSON.stringify(r.data) !== JSON.stringify(S.cur.data) || r.name !== S.cur.name;
    S.cur.name = r.name;
    S.cur.data = normalizeData(r.data || {});
    S.cur.data.boards ||= [];
    S.cur.data.name ||= r.name;
    S.cur.base = r.updated_at;
    $("studio-name").textContent = S.cur.name;
    resumePollers(true);
    TAB_RENDER[S.tab] && TAB_RENDER[S.tab]();          // 重渲染当前页签
    if (changed || isConflict) toast(isConflict
      ? "云端已有其他设备的更新，你刚才的修改基于旧版本已回滚，请重新修改"
      : "已同步云端最新版本");
    setSaveState("");
  } catch (_) { /* 网络抖动静默失败，下次焦点再试 */ }
}
/* 桌面端盲区修补：PC 上窗口间切换不触发 visibilitychange（标签页一直可见），
   版本戳会一直停留旧值 → 保存必被 409 拒绝、修改总被回滚（表现为“重命名跳回去”）。
   补 focus 监听 + 定期轻同步，让版本戳保持新鲜，409 只在真正的多端并发时出现。 */
function backgroundSync() {
  if (document.hidden || !S.cur || S.dirty || S.saving) return;
  if ($("studio-screen").classList.contains("hidden")) { loadHome().catch(() => {}); return; }
  syncFromCloud(false);
}
window.addEventListener("focus", backgroundSync);
setInterval(backgroundSync, 45000);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { saveNow(); return; }
  // 回到前台：有未保存改动先落库（保存自带版本校验）；否则静默拉取云端最新版
  if (S.cur) {
    if (S.dirty) saveNow();
    else syncFromCloud(false);
    resumePollers(); // 重启丢失的轮询、清理无任务 ID 的僵尸"渲染中"
  } else {
    loadHome().catch(() => {});
  }
});
/* ================= 删除同步：界面删除图片/视频时，服务器文件一并清理 ================= */
async function purgeServerFiles(urls) {
  urls = (urls || []).filter(Boolean);
  if (!urls.length) return;
  // 先把「引用已移除」落库，服务端的引用检查才不会误判文件仍在使用而拒绝删除
  try { await saveNow(); } catch (_) {}
  try { await api("/api/delete-files", { urls }); } catch (_) {} // 静默：文件清理失败不影响界面操作
}
/* ================= 底部 Tab ================= */
const TAB_RENDER = {
  story: renderStory, styles: renderStyles, assets: renderAssets, boards: renderBoards, voice: renderVoice, settings: renderSettings,
};
function setTab(t) {
  S.tab = t;
  document.querySelectorAll(".tab-page").forEach((el) => el.classList.add("hidden"));
  $("tab-" + t).classList.remove("hidden");
  document.querySelectorAll(".tabbar-item").forEach((el) =>
    el.classList.toggle("active", el.dataset.tab === t));
  $("studio-body").scrollTop = 0;
  TAB_RENDER[t] && TAB_RENDER[t]();
}

/* ================= 风格库页 ================= */
// 37 张风格选择卡（移植自 AI-anime-studio 桌面版，含预览图）
const STYLE_GALLERY = [
  { cat: "3D", name: "无风格（直出）", emoji: "🚫", img: "style-previews/neutral.jpg", desc: "不施加风格倾向，按提示词原始描述直接生成" },
  { cat: "3D", name: "3D卡通渲染", emoji: "🧸", img: "style-previews/3d_cartoon.jpg", desc: "皮克斯式3D卡通渲染，圆润造型，柔和的次表面散射，明亮饱和的色块" },
  { cat: "3D", name: "电影级3D质感", emoji: "🎬", img: "style-previews/cinematic_3d.jpg", desc: "写实电影级3D渲染，物理光照，浅景深，电影感构图" },
  { cat: "3D", name: "国风3D动画", emoji: "🏮", img: "style-previews/guofeng_3d.jpg", desc: "国产3D古风动画质感，敦煌配色，飘逸绸缎与仙侠气韵" },
  { cat: "3D", name: "黏土定格", emoji: "🧱", img: "style-previews/clay.jpg", desc: "黏土定格动画质感，保留手工捏痕，微缩摄影棚布光" },
  { cat: "3D", name: "3D厚涂插画", emoji: "🖌️", img: "style-previews/3d_thickpaint.jpg", desc: "3D厚涂质感，立体光影与手绘笔触结合，画面厚重" },
  { cat: "3D", name: "毛毡布艺", emoji: "🧶", img: "style-previews/felt.jpg", desc: "羊毛毡质感，纤维细节可见，柔和的手工温度" },
  { cat: "动漫", name: "日式赛璐璐", emoji: "📺", img: "style-previews/cel_anime.jpg", desc: "经典赛璐璐平涂，清晰线稿，色块分明的二分光影" },
  { cat: "动漫", name: "吉卜力手绘", emoji: "🌿", img: "style-previews/ghibli.jpg", desc: "吉卜力式水彩背景，温暖自然光，细腻的空气感与生活气息" },
  { cat: "动漫", name: "新海诚光影", emoji: "🌇", img: "style-previews/shinkai.jpg", desc: "新海诚式绚烂天空，超饱和晚霞与逆光，都市青春物语" },
  { cat: "动漫", name: "90年代复古日漫", emoji: "📼", img: "style-previews/retro_anime.jpg", desc: "90年代赛璐璐质感，胶片颗粒，复古高饱和配色" },
  { cat: "动漫", name: "二次元厚涂", emoji: "⚔️", img: "style-previews/thick_anime.jpg", desc: "二次元厚涂插画，精致五官刻画，丰富的色彩层次" },
  { cat: "动漫", name: "美式漫画", emoji: "💥", img: "style-previews/us_comic.jpg", desc: "美式漫画风，粗黑描边，网点阴影，强烈的动感构图" },
  { cat: "动漫", name: "少女漫画", emoji: "🌸", img: "style-previews/shoujo.jpg", desc: "少女漫画，柔和粉彩，网点与花瓣装饰，唯美氛围" },
  { cat: "动漫", name: "暗黑日漫", emoji: "🌑", img: "style-previews/dark_anime.jpg", desc: "暗黑系日漫，强对比阴影，哥特式氛围" },
  { cat: "国风", name: "水墨写意", emoji: "🖤", img: "style-previews/ink_wash.jpg", desc: "水墨晕染，大量留白，毛笔笔触，意境深远" },
  { cat: "国风", name: "青绿山水", emoji: "⛰️", img: "style-previews/blue_green.jpg", desc: "千里江山图式青绿设色，绢本底色，古典山水构图" },
  { cat: "国风", name: "工笔重彩", emoji: "🪭", img: "style-previews/gongbi.jpg", desc: "工笔重彩，匀细游丝描，矿物颜料质感" },
  { cat: "国风", name: "敦煌壁画", emoji: "🕌", img: "style-previews/dunhuang.jpg", desc: "敦煌壁画质感，斑驳墙面，飞天线描，土黄与石绿" },
  { cat: "国风", name: "剪纸皮影", emoji: "🎭", img: "style-previews/papercut.jpg", desc: "剪纸皮影风格，平面镂空，红黑对比，民间艺术质感" },
  { cat: "国风", name: "青花瓷韵", emoji: "🏺", img: "style-previews/bluewhite.jpg", desc: "青花瓷配色，缠枝纹样，白底蓝彩的东方雅致" },
  { cat: "影视", name: "电影胶片", emoji: "🎞️", img: "style-previews/film35.jpg", desc: "35mm胶片颗粒，宽画幅，柯达暖调，电影级调色" },
  { cat: "影视", name: "赛博朋克", emoji: "🌃", img: "style-previews/cyberpunk.jpg", desc: "赛博朋克，霓虹雨夜，全息广告，青紫高对比" },
  { cat: "影视", name: "末日废土", emoji: "☢️", img: "style-previews/wasteland.jpg", desc: "废土美学，沙尘色温，残破建筑与锈蚀金属" },
  { cat: "影视", name: "黑色电影", emoji: "🕶️", img: "style-previews/noir.jpg", desc: "Film Noir，高对比黑白，百叶窗光影，悬疑氛围" },
  { cat: "影视", name: "冰雪末世", emoji: "❄️", img: "style-previews/ice.jpg", desc: "冰雪末世，冷蓝低饱和，雾气与冰晶颗粒" },
  { cat: "影视", name: "蒸汽波", emoji: "🌴", img: "style-previews/vaporwave.jpg", desc: "蒸汽波美学，粉紫渐变，复古电脑图形与雕像元素" },
  { cat: "影视", name: "暗黑恐怖", emoji: "🕯️", img: "style-previews/horror.jpg", desc: "暗黑恐怖，单光源烛光，浓重阴影，低明度" },
  { cat: "影视", name: "温暖生活剧", emoji: "🏠", img: "style-previews/family_warm.jpg", desc: "温暖生活剧质感，柔光，暖黄室内色调" },
  { cat: "艺术", name: "梵高油画", emoji: "🌻", img: "style-previews/vangogh.jpg", desc: "梵高式厚涂油画，流动的短笔触，浓烈奔放的色彩" },
  { cat: "艺术", name: "水彩绘本", emoji: "🖍️", img: "style-previews/watercolor_kids.jpg", desc: "儿童绘本水彩，柔和边缘，纸张纹理，天真视角" },
  { cat: "艺术", name: "扁平插画", emoji: "🔷", img: "style-previews/flat.jpg", desc: "扁平矢量插画，几何色块，无描边，现代简洁" },
  { cat: "艺术", name: "像素艺术", emoji: "👾", img: "style-previews/pixel.jpg", desc: "像素艺术，有限调色板，16-bit复古游戏感" },
  { cat: "艺术", name: "浮世绘", emoji: "🌊", img: "style-previews/ukiyo.jpg", desc: "浮世绘木刻，平涂色块，波浪与云纹图式" },
  { cat: "艺术", name: "极简线条", emoji: "✏️", img: "style-previews/line_min.jpg", desc: "极简单线插画，大量留白，一根线条讲完故事" },
  { cat: "艺术", name: "暗黑哥特", emoji: "🦇", img: "style-previews/gothic.jpg", desc: "哥特式暗黑华丽，尖拱与蕾丝，抑郁而优雅" },
  { cat: "艺术", name: "复古印刷", emoji: "📰", img: "style-previews/retro_print.jpg", desc: "复古印刷海报，错版套色，粗糙纸纹" },
];
let styleGalleryCat = "全部";

function renderStyles() {
  // 分类筛选条
  const cats = ["全部", ...new Set(STYLE_GALLERY.map((p) => p.cat))];
  const seg = $("style-cat-seg");
  if (seg && !seg.dataset.built) {
    seg.innerHTML = cats.map((c) => `<button class="seg-item" data-k="${esc(c)}" onclick="setStyleGalleryCat('${esc(c)}')">${esc(c)}</button>`).join("");
    seg.dataset.built = "1";
  }
  renderStyleGallery();
  // oip 进阶参考区收起态；提示词库入口探测
  const list = $("styles-list");
  if (list && !list.classList.contains("hidden") && !S.stylesLoaded) loadOipCards();
  promptLibProbe();
}
function setStyleGalleryCat(c) {
  styleGalleryCat = c;
  renderStyleGallery();
}
function renderStyleGallery() {
  const grid = $("style-gallery");
  if (!grid) return;
  const seg = $("style-cat-seg");
  if (seg) seg.querySelectorAll(".seg-item").forEach((el) => el.classList.toggle("active", el.dataset.k === styleGalleryCat));
  const items = STYLE_GALLERY.filter((p) => styleGalleryCat === "全部" || p.cat === styleGalleryCat);
  grid.innerHTML = items.map((p) => `
    <div class="style-card">
      <div class="style-thumb" style="background:${esc(p.img ? "" : "#2a2d3a")}">
        ${p.img ? `<img src="/static/${esc(p.img)}" loading="lazy" alt="${esc(p.name)}" onerror="this.remove()">` : ""}
        <span class="style-emoji">${esc(p.emoji)}</span>
      </div>
      <div class="style-info">
        <b>${esc(p.name)}</b>
        <p>${esc(p.desc)}</p>
      </div>
      <div class="style-actions">
        <button class="btn btn-primary style-btn" onclick="applyStyleBible('${esc(p.name)}')">✍️ 用这套</button>
        <button class="btn btn-ghost style-btn" onclick="copyStyleDesc('${esc(p.name)}')">📋 复制</button>
      </div>
    </div>`).join("");
}
function findStylePreset(name) {
  return STYLE_GALLERY.find((x) => x.name === name);
}
function applyStyleBible(name) {
  const p = findStylePreset(name);
  if (!p) return;
  const desc = p.name === "无风格（直出）" ? "" : p.desc;
  const d = S.cur?.data;
  if (!d) {
    // 连项目都没打开（理论上 tab 只在项目内出现）：退回复制
    copyStyleDesc(name);
    return toast("还没有打开项目，已复制描述");
  }
  if (d.story) {
    // 已有剧本：直接写入画风圣经并跳回故事页看效果
    d.story.style_bible = desc;
    delete d.pending_style;
    touch();
    setTab("story");
    toast(desc ? `已把「${p.name}」写入画风圣经` : "已清空风格倾向（直出）");
    // 换风格后资产提示词（人物/场景/道具）仍是旧画风 —— 询问是否立即重写
    const kinds = ["characters", "scenes", "props"];
    const total = kinds.reduce((n, k) => n + ((d.story[k] || []).length), 0);
    if (desc && total) {
      setTimeout(() => {
        if (confirm(`检测到 ${total} 个角色/场景/道具的设定图提示词还是旧画风。\n\n是否立即按新画风重写？\n（已生成的参考图不会自动重绘，重写后重新生图即可换装新风格；你手改过的外观细节会尽量保留）`)) {
          syncAssetsToStyle(desc);
        }
      }, 600);
    }
  } else {
    // 还没有剧本：暂存，生成剧本时自动套用
    d.pending_style = { name: p.name, desc };
    touch();
    setTab("story");
    toast(desc ? `已暂存「${p.name}」，生成剧本后自动套用` : "已暂存直出模式，生成剧本后生效");
  }
}
function clearPendingStyle() {
  const d = S.cur?.data;
  if (!d || !d.pending_style) return;
  delete d.pending_style;
  touch();
  renderStory();
}
async function copyStyleDesc(name) {
  const p = findStylePreset(name);
  if (!p) return;
  const done = () => toast("已复制，可粘贴进画风圣经或分镜提示词");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(p.desc).then(done, () => fallbackCopyText(p.desc, done));
  } else {
    fallbackCopyText(p.desc, done);
  }
}
/* ---- oip 39 张要点卡：折叠区，按需加载 ---- */
function toggleOipCards() {
  const list = $("styles-list");
  const btn = $("oip-toggle-btn");
  if (!list || !btn) return;
  const opening = list.classList.contains("hidden");
  list.classList.toggle("hidden", !opening);
  btn.textContent = opening ? "收起 ▴" : "展开 ▾";
  if (opening && !S.stylesLoaded) loadOipCards();
}
async function loadOipCards() {
  const list = $("styles-list");
  if (!list) return;
  list.innerHTML = `<p class="lib-empty">加载中…</p>`;
  try {
    const r = await api("/api/style-cards", undefined, "GET", 15000);
    const cards = r.cards || [];
    if (!$("styles-list")) return;
    if (!cards.length) {
      list.innerHTML = `<p class="lib-empty">没有可用的风格卡</p>`;
    } else {
      list.innerHTML = cards.map((c) => `
        <div class="sc-item" onclick="toggleStyleCard('${esc(c.id)}')">
          <div class="sc-head"><b>${esc(c.title)}</b><span class="sc-id">${esc(c.id)}</span></div>
          <p class="sc-summary">${esc(c.summary)}</p>
          <div class="sc-detail hidden" id="sc-detail-${esc(c.id)}"></div>
        </div>`).join("");
    }
    S.stylesLoaded = true;
  } catch (e) {
    if ($("styles-list")) list.innerHTML = `<p class="lib-empty">加载失败：${esc(e.message)}（点「收起」再「展开」重试）</p>`;
  }
}

/* ================= 配音页：服务状态 + 角色音色 + 分镜台词配音 ================= */
async function voiceStatusProbe() {
  try {
    const r = await api("/api/voice/status", undefined, "GET", 8000);
    S.voiceReady = !!r.ready;
    S.voiceWarming = !r.ready && !!r.warming;
  } catch (e) {
    S.voiceReady = false;
    S.voiceWarming = false;
    console.log("[配音] 状态探测失败：", e.message);
  }
  // 状态卡在「配音」页和「设置」页各有一份，用 class 统一更新
  document.querySelectorAll(".voice-status-hint").forEach((el) => {
    el.textContent = S.voiceReady ? "已就绪 · VoxCPM 声音克隆（CPU 推理）"
      : S.voiceWarming ? "模型预热中（首次需下载约 1GB），请稍后重试"
      : "未启动 · 配音功能不可用";
  });
  document.querySelectorAll(".voice-card").forEach((card) => card.classList.toggle("voice-off", !S.voiceReady));
  // 预设音色列表（配音抽屉下拉框用），静默拉取一次
  if (!S.voicePresets) {
    try {
      const pr = await api("/api/voice/presets", undefined, "GET", 8000);
      S.voicePresets = pr.presets || [];
    } catch (e) {
      S.voicePresets = [];
    }
  }
  return S.voiceReady;
}
function renderVoice() {
  renderVoiceCharList();
  renderVoiceBoardList();
  if (S.voiceReady === null) {
    voiceStatusProbe().then(() => { renderVoiceCharList(); renderVoiceBoardList(); });
  }
}
function renderVoiceCharList() {
  const box = $("voice-char-list");
  if (!box) return;
  const chars = ((S.cur?.data.story || {}).characters) || [];
  if (!chars.length) {
    box.innerHTML = `<p class="lib-empty">还没有角色：先在「故事」页生成剧本</p>`;
    return;
  }
  box.innerHTML = chars.map((c, i) => `
    <div class="vrow">
      <div class="vrow-main">
        <b class="vrow-name">${esc(c.name)}</b>
        ${c.voice_url
          ? `<audio controls preload="metadata" src="${c.voice_url}"></audio>
          <button class="btn btn-ghost btn-act-sm" title="下载参考音频" onclick="downloadVoice(${i})">⬇</button>`
          : `<span class="vrow-miss">未设定音色</span>`}
      </div>
      <button class="btn btn-ghost btn-act-sm" title="设定音色 / 参考文本" onclick="openAssetSheet('characters',${i})">⚙️</button>
    </div>`).join("");
}
function renderVoiceBoardList() {
  const box = $("voice-board-list");
  if (!box) return;
  const boards = S.cur?.data.boards || [];
  if (!boards.length) {
    box.innerHTML = `<p class="lib-empty">还没有分镜：先在「故事」页生成剧本</p>`;
    return;
  }
  if (S.voiceReady === false) {
    box.innerHTML = S.voiceWarming
      ? `<p class="lib-empty">⏳ 模型预热中：首次部署会自动下载约 1GB 模型（几分钟），完成后即可配音</p>
      <button class="btn btn-ghost btn-block" onclick="S.voiceReady = null; renderVoice()">🔄 重新探测</button>`
      : `<p class="lib-empty">配音服务不可用：在线版请稍后重试；本地版点上方「服务说明 / 启动方法」查看如何启动</p>
      <button class="btn btn-ghost btn-block" onclick="S.voiceReady = null; renderVoice()">🔄 重新探测</button>`;
    return;
  }
  box.innerHTML = boards.map((b) => {
    const dubs = b.dubs || [];
    const latest = dubs[dubs.length - 1];
    return `
    <div class="vrow">
      <div class="vrow-main">
        <span class="vrow-no">${b.shot_number}</span>
        <div class="vrow-text">
          <div class="vrow-line">${b.dialogue ? esc(b.dialogue) : `<i style="color:var(--muted);font-size:12.5px;">无台词（点配音后在抽屉里填写）</i>`}</div>
          ${latest ? `<audio controls preload="metadata" src="${latest.url}"></audio>
          <span class="vrow-miss">最新：${esc(latest.voice || "随机音色")}${dubs.length > 1 ? ` · 共 ${dubs.length} 条` : ""}</span>` : ""}
        </div>
      </div>
      ${latest ? `<button class="btn btn-ghost" title="下载最新配音" onclick="downloadDub('${b.id}',${dubs.length - 1})">⬇</button>` : ""}
      <button class="btn btn-ghost" onclick="openDubSheet('${b.id}')">🔊 配音</button>
    </div>`;
  }).join("");
}

/* ================= 故事页 ================= */
function renderStory() {
  const d = S.cur.data;
  const hasStory = !!d.story;
  $("story-create").classList.toggle("hidden", hasStory);
  $("story-view").classList.toggle("hidden", !hasStory);
  // 预选风格提示条（还没有剧本时显示）
  const ps = $("pending-style");
  if (ps) {
    const show = !hasStory && d.pending_style;
    ps.classList.toggle("hidden", !show);
    if (show) ps.innerHTML = `🎨 已选风格：<b>${esc(d.pending_style.name)}</b>，生成剧本后自动套用 <button class="mini-edit" onclick="clearPendingStyle()">✕ 取消</button>`;
  }
  // 生成剧本后不再隐藏头脑风暴：可通过「💬 头脑风暴对话」按钮展开继续聊
  const showChat = !!(d.draft && d.draft.messages.length) && (S.showBrainstorm || !hasStory);
  $("brainstorm-wrap").classList.toggle("hidden", !showChat);
  const chatBtn = $("btn-brainstorm-toggle");
  if (chatBtn) chatBtn.classList.toggle("hidden", !hasStory);
  if (hasStory) {
    const st = d.story;
    $("story-hook").textContent = "「" + (st.hook || "…") + "」";
    $("story-synopsis").textContent = st.synopsis || "";
    $("story-style").textContent = st.style_bible || "";
    $("story-stats").innerHTML = [
      ["characters", "角色"], ["scenes", "场景"], ["props", "道具"],
    ].map(([k, label]) => `<div class="stat-chip"><div class="stat-num">${(st[k] || []).length}</div><div class="stat-label">${label}</div></div>`).join("")
      + `<div class="stat-chip"><div class="stat-num">${d.boards.length}</div><div class="stat-label">分镜</div></div>`;
  } else if (d.draft && d.draft.messages.length) {
    renderChat();
  }
}
function toggleBrainstorm() {
  const d = S.cur?.data;
  if (!d) return;
  if (!(d.draft && d.draft.messages.length)) {
    // 还没有对话记录：直接开一段新的头脑风暴
    openBrainstorm();
    return;
  }
  S.showBrainstorm = !S.showBrainstorm;
  renderStory();
  if (S.showBrainstorm) {
    renderChat();
    const list = $("chat-list");
    if (list) list.scrollTop = list.scrollHeight;
  }
}
function openIdeaInput() {
  $("idea-input-wrap").classList.remove("hidden");
  $("idea-input").focus();
}
async function genStoryFromIdea() {
  const idea = $("idea-input").value.trim();
  if (!idea) return toast("先写点什么吧", true);
  showBusy("正在创作剧本…", "生成角色、场景、分镜与全部提示词，约 1–2 分钟");
  try {
    const story = await api("/api/generate-story", { idea, style_bible: S.cur?.data?.pending_style ? S.cur.data.pending_style.desc : undefined });
    S.showBrainstorm = false;
    applyStory(story);
    hideBusy();
    toast("剧本完成！去「角色场景」出设定图，再到「分镜」逐镜生成");
  } catch (e) { hideBusy(); toast(e.message, true); }
}
function applyStory(story) {
  const d = S.cur.data;
  // 资产继承：新剧本的同名角色/场景/道具，继承旧参考图(ref_images)与设定图(image_url)。
  // 手动删除过的（delRef 从旧数组里移除）不会回来；改了名字的角色视为新角色，不带旧图。
  const prev = d.story_assets || d.story || null;
  if (prev) {
    for (const k of ["characters", "scenes", "props"]) {
      const olds = prev[k] || [], news = story[k] || [];
      for (const it of news) {
        if (!it || !it.name) continue;
        const o = olds.find((x) => x && x.name === it.name);
        if (!o) continue;
        if (o.ref_images && o.ref_images.length) it.ref_images = o.ref_images.slice();
        if (o.image_url) it.image_url = o.image_url;
      }
    }
  }
  delete d.story_assets;
  // 用户在风格库预选的风格：覆盖 AI 自拟画风，保证剧本/分镜与所选风格一致
  if (d.pending_style) {
    story.style_bible = d.pending_style.desc;
    delete d.pending_style;
  }
  d.story = story;
  d.boards = (story.storyboards || []).map((sb, i) => ({
    id: "b" + (sb.shot_number || i + 1) + "_" + Math.random().toString(36).slice(2, 7),
    shot_number: sb.shot_number || i + 1,
    description: sb.description || "",
    dialogue: sb.dialogue || "",
    duration: sb.duration || 3,
    image_prompt: sb.image_prompt || "",
    image_prompt_ai: sb.image_prompt || "",   // AI 生成原文基线：用户手改后 image_prompt_edited 置 true（P2 手改 > P3 AI 兜底）
    image_prompt_edited: false,
    video_prompt: sb.video_prompt || "",
    images: [],
    videoUrl: null,
    videoTaskId: null,
    videoStatus: null,
  }));
  delete story.storyboards;
  S.cur.name = S.cur.name === "未命名项目" ? (story.hook || S.cur.name).slice(0, 20) : S.cur.name;
  $("studio-name").textContent = S.cur.name;
  touch();
  renderStory();
}
async function genStoryFromChat() {
  const d = S.cur.data;
  if (d.story && !confirm("根据头脑风暴重新生成剧本会覆盖当前剧本与分镜进度（角色/场景/道具的参考图会继承到同名条目），确定继续吗？")) return;
  showBusy("正在创作剧本…", "把头脑风暴的成果整理成完整剧本，约 1–2 分钟");
  try {
    const story = await api("/api/generate-story", {
      idea: "根据头脑风暴对话生成",
      chat_history: (d.draft.messages || []).map((m) => ({ role: m.role, content: m.content })),
      style_bible: d.pending_style ? d.pending_style.desc : undefined,
    });
    S.showBrainstorm = false;
    applyStory(story);
    hideBusy();
    toast("剧本完成！");
  } catch (e) { hideBusy(); toast(e.message, true); }
}
async function regenStory() {
  if (!confirm("重新生成会覆盖当前剧本与全部分镜进度（角色/场景/道具的参考图与设定图会保留到新剧本的同名条目上；已生成的图片视频文件仍保留），确定吗？")) return;
  // 先把旧资产（参考图/设定图）暂存，供 applyStory 按名字继承
  const s = S.cur.data.story || {};
  S.cur.data.story_assets = {
    characters: s.characters || [], scenes: s.scenes || [], props: s.props || [],
  };
  S.cur.data.story = null;
  S.cur.data.boards = [];
  S.cur.data.draft = null;
  touch();
  renderStory();
  toast("已清空，重新开始创作");
}

/* ---------- 梗概 / 画风编辑 + 一键更新分镜 ---------- */
function editStoryField(key, label) {
  const st = S.cur?.data.story;
  if (!st) return;
  openSheet(`
    <div class="sheet-title">编辑${label}</div>
    <div class="sheet-sub">保存后立即生效；改完可点「一键更新分镜」让分镜提示词跟上</div>
    <textarea id="sf-edit" rows="9">${esc(st[key] || "")}</textarea>
    <button class="btn btn-primary btn-block" onclick="saveStoryField('${key}')">保存</button>`);
  setTimeout(() => $("sf-edit").focus(), 250);
}
function editSynopsis() { editStoryField("synopsis", "剧情梗概"); }
function editStyleBible() { editStoryField("style_bible", "画风圣经"); }
function saveStoryField(key) {
  const st = S.cur?.data.story;
  if (!st) return;
  st[key] = ($("sf-edit").value || "").trim();
  touch(); closeSheet(); renderStory();
  toast("已保存");
}
async function syncBoardsToStory() {
  const d = S.cur?.data;
  if (!d?.story) return;
  const boards = d.boards || [];
  if (!boards.length) return toast("还没有分镜可更新", true);
  if (!confirm(`将按最新的剧情梗概与画风圣经，重写全部 ${boards.length} 个分镜的生图/视频提示词。\n已生成的图片和视频不会被删除，重写后逐镜重新生成即可看到新效果。继续吗？`)) return;
  const assets = assetPayload();   // 含每个角色的服装变体，供【角色·服装】引用解析
  let ok = 0, fail = 0;
  showBusy("正在更新分镜提示词…", `0/${boards.length}`);
  for (let i = 0; i < boards.length; i++) {
    const b = boards[i];
    $("busy-sub").textContent = `${i + 1}/${boards.length} · ${fail ? "失败 " + fail + " 个" : "约每个几秒"}`;
    try {
      const r = await api("/api/regenerate-prompt", {
        type: "storyboard",
        style_bible: d.story.style_bible || "",
        synopsis: d.story.synopsis || "",
        ...assets,
        description: b.description || "",
        dialogue: b.dialogue || "",
        duration: b.duration || 3,
        old_prompt: b.image_prompt || "",
        old_video_prompt: b.video_prompt || "",  // 用户手改过的视频提示词一起带上：重写在它基础上做，不从零重来
        feedback: "按最新剧情梗概与画风圣经整体校准本镜头：剧情走向与人物行为符合新梗概；美术风格/光源/影调/氛围严格按新画风圣经重写；保留本镜头原有的叙事作用、景别与构图要点。",
      });
      const oldImg = b.image_prompt || "", oldVid = b.video_prompt || "";
      if (r.image_prompt) { b.image_prompt = keepParams(r.image_prompt, oldImg); b.image_prompt_ai = b.image_prompt; b.image_prompt_edited = false; ok++; }
      if (r.video_prompt) b.video_prompt = keepParams(r.video_prompt, oldVid);
      touch();
    } catch (e) { fail++; }
  }
  hideBusy();
  toast(fail ? `更新完成：成功 ${ok} 个，失败 ${fail} 个（稍后可再点一次重试失败的）`
             : `✅ ${ok} 个分镜提示词已更新；已生成的图片不变，逐镜重新生成即可看到新梗概/新画风`);
}

/* ---------- 换画风后重写资产（角色/场景/道具）设定图提示词 ---------- */
async function syncAssetsToStyle(styleDesc) {
  const d = S.cur?.data;
  if (!d?.story) return;
  if (!styleDesc) styleDesc = d.story.style_bible || "";
  const KINDS = [
    ["characters", "character", "角色"],
    ["scenes", "scene", "场景"],
    ["props", "prop", "道具"],
  ];
  const items = [];
  for (const [key, type, label] of KINDS) {
    for (const it of d.story[key] || []) {
      if (it && it.name) items.push({ kind: key, type, label, it });
    }
  }
  if (!items.length) return toast("还没有角色/场景/道具可重写", true);
  let ok = 0, fail = 0;
  showBusy("正在按新画风重写设定图提示词…", `0/${items.length}`);
  for (let i = 0; i < items.length; i++) {
    const { type, label, it } = items[i];
    $("busy-sub").textContent = `${i + 1}/${items.length} · ${esc(it.name)} · ${fail ? "失败 " + fail + " 个" : "约每个几秒"}`;
    try {
      const r = await api("/api/regenerate-prompt", {
        type,
        style_bible: styleDesc,
        synopsis: d.story.synopsis || "",
        name: it.name,
        description: it.description || "",
        old_prompt: it.image_prompt || "",
        feedback: "全片画风已更换：把本设定图提示词按新画风圣经重写，美术风格/色调/光影/画质描述必须符合新风格；保留名称、身份设定与原有外观锚点（发型发色/瞳色/五官/体型年龄/服装款式与颜色/随身标志物/陈设/材质），不要新增或删除关键特征。",
      });
      if (r.image_prompt) { it.image_prompt = keepParams(r.image_prompt, it.image_prompt || ""); ok++; }
      touch();
    } catch (e) { fail++; }
  }
  hideBusy();
  renderAssets?.();
  toast(fail ? `重写完成：成功 ${ok} 个，失败 ${fail} 个（可再试一次）`
             : `✅ ${ok} 个设定图提示词已换新画风；已生成的参考图不变，重新生图即可看到新风格`);
}

/* ---------- 头脑风暴 ---------- */
function openBrainstorm() {
  const d = S.cur.data;
  d.draft ||= { messages: [], ready: false };
  $("idea-input-wrap").classList.add("hidden");
  S.showBrainstorm = true; // 剧本已生成时也允许展开对话继续聊
  // 输入草稿：挂一次监听（实时保存），并还原上次未发完的内容
  if (!S._chatDraftHooked) {
    $("chat-input").addEventListener("input", (e) => saveChatDraft(e.target.value));
    S._chatDraftHooked = true;
  }
  restoreChatDraft();
  if (!d.draft.messages.length) {
    d.draft.messages.push({ role: "user", content: "我想做一个动画短片，请开始引导我。" });
    touch();
    renderStory();          // draft 已有消息 → renderStory 会显示聊天框
    sendChat(true);         // 只发 AI 请求，不再重复插消息
  } else {
    renderStory();
  }
}
function renderChat() {
  const wrap = $("brainstorm-wrap");
  const msgs = S.cur.data.draft ? S.cur.data.draft.messages : [];
  wrap.classList.toggle("hidden", !msgs.length);
  $("chat-list").innerHTML = msgs.map((m) => {
    const imgs = m.images || (m.image ? [m.image] : []);   // 兼容旧的单图消息
    return `
    <div class="chat-msg ${m.role === "user" ? "user" : "ai"}">
      <div class="bubble">
        ${m.role !== "user" ? md(m.content) : esc(m.content)}
        ${imgs.length === 1
          ? `<img src="${imgs[0]}">`
          : imgs.length ? `<div class="chat-imgs">${imgs.map((u) => `<img src="${u}">`).join("")}</div>` : ""}
      </div>
    </div>`;
  }).join("");
  $("ready-banner").classList.toggle("hidden", !(S.cur.data.draft && S.cur.data.draft.ready));
  $("chat-list").scrollTop = $("chat-list").scrollHeight;
}
async function sendChat(isFirst) {
  const d = S.cur.data.draft;
  if (!d) return;
  const input = $("chat-input");
  const dataUrls = S.chatImages.splice(0);
  const b64s = dataUrls.map((u) => u.split(",")[1]);
  let text = input.value.trim();
  let pushed = false;              // 本次是否插入了用户消息（失败时只回滚它自己）
  if (isFirst) {
    text = d.messages.length ? d.messages[d.messages.length - 1].content
                             : "我想做一个动画短片，请开始引导我。";
  } else {
    if (!text && !dataUrls.length) return;
    const msg = { role: "user", content: text || "（见图）" };
    if (dataUrls.length === 1) msg.image = dataUrls[0];
    else if (dataUrls.length > 1) msg.images = dataUrls;   // 多张图存数组
    d.messages.push(msg);
    pushed = true;
    input.value = "";
    saveChatDraft("");               // 输入草稿清空（发送成功路径）
  }
  renderChat();
  $("chat-send").disabled = true;
  try {
    const r = await api("/api/brainstorm", {
      step: d.messages.filter((m) => m.role === "user").length,
      messages: d.messages.map((m) => ({ role: m.role, content: m.content })),
      user_input: text,
      image_base64_list: b64s.length ? b64s : undefined,
    });
    d.messages.push({ role: "assistant", content: r.reply });
    if (r.status === "ready") d.ready = true;
    touch();
  } catch (e) {
    if (pushed) {
      d.messages.pop();              // 只回滚本次插入的消息
      // ★ 口述/手打的内容绝不弄丢：原文放回输入框（本地草稿同步还原），附图也还原
      input.value = text;
      saveChatDraft(text);
      if (dataUrls.length) S.chatImages.unshift(...dataUrls);
    }
    toast(`发送失败：${e.message}（原文已放回输入框，稍后再发即可）`, true);
  }
  $("chat-send").disabled = false;
  touch(); renderChat();
}
/* 聊天输入草稿：实时存 localStorage，刷新/误关/失败都不丢用户打的内容 */
function _chatDraftKey() { return `chat_draft_${S.cur ? S.cur.id : "none"}`; }
function saveChatDraft(v) { try { localStorage.setItem(_chatDraftKey(), v); } catch (_) {} }
function restoreChatDraft() {
  const input = $("chat-input");
  if (input && !input.value.trim()) {
    try { input.value = localStorage.getItem(_chatDraftKey()) || ""; } catch (_) {}
  }
}
function onChatImage(input) {
  // 支持一次选多张（相册/文件都行）；上限 9 张防误选几十张把请求撑爆
  const files = [...input.files].slice(0, 9);
  input.value = "";
  if (!files.length) return;
  let done = 0;
  for (const file of files) {
    const reader = new FileReader();
    reader.onload = () => {
      S.chatImages.push(reader.result);
      if (++done === files.length) toast(`已附 ${files.length} 张图，发送时一并传给 AI`);
    };
    reader.readAsDataURL(file);
  }
}

/* ================= 资产页（角色/场景/道具） ================= */
const KIND_LABEL = { characters: "角色", scenes: "场景", props: "道具" };
function setAssetTab(k) {
  S.assetTab = k;
  document.querySelectorAll("#asset-seg .seg-item").forEach((el) =>
    el.classList.toggle("active", el.dataset.k === k));
  renderAssets();
}
function renderAssets() {
  const k = S.assetTab;
  const items = (S.cur.data.story || {})[k] || [];
  const grid = $("asset-grid");
  if (!items.length) {
    grid.innerHTML = `<div class="card pad" style="grid-column:1/-1;text-align:center;color:var(--muted);font-size:13.5px;">
      还没有${KIND_LABEL[k]}，先生成剧本，或直接手动新增</div>`;
    grid.insertAdjacentHTML("beforeend",
      `<button class="btn btn-ghost btn-block" style="grid-column:1/-1;" onclick="addAsset()">＋ 新增${KIND_LABEL[k]}</button>`);
    return;
  }
  grid.innerHTML = items.map((it, i) => `
    <div class="asset-card" onclick="openAssetSheet('${k}',${i})">
      <div class="asset-img">
        ${it.image_url ? `<img src="${it.image_url}" loading="lazy">`
          : `<div class="asset-empty">🖼<span>点击生成设定图</span></div>`}
      </div>
      <div class="asset-name"><b>${esc(it.name)}</b><span>${esc((it.description || "").slice(0, 22))}${it.outfits && it.outfits.length ? ` · ${it.outfits.length} 套服装` : ""}</span></div>
    </div>`).join("");
  grid.insertAdjacentHTML("beforeend",
    `<button class="btn btn-ghost btn-block" style="grid-column:1/-1;" onclick="addAsset()">＋ 新增${KIND_LABEL[k]}</button>`);
}
function addAsset() {
  if (!S.cur) return;
  const k = S.assetTab;
  const st = S.cur.data.story ||= {};
  st[k] ||= [];
  st[k].push({ name: `新${KIND_LABEL[k]}`, description: "", image_prompt: "", ref_images: [] });
  touch(); renderAssets();
  toast(`已添加，点卡片编辑并生成设定图`);
}
function openAssetSheet(kind, index) {
  // 面板重开前先把当前框内内容落盘：生图/重写/删图/传参考图等操作都会重开面板，
  // 不同步的话用户手改的名称/描述/提示词会被旧值冲掉（实测：改名"新场景"不生效）
  if (S.furn && S.furn.kind === kind && S.furn.index === index && $("as-name")) syncAssetFields(kind, index);
  const it = (S.cur.data.story || {})[kind]?.[index];
  if (!it) return;
  const isChar = kind === "characters";
  S.furn = { kind, index };   // 家具布置编辑器需要知道当前正在编辑哪个场景资产
  const refs = it.ref_images || [];
  openSheet(`
    <div class="sheet-title">${esc(it.name)} <span style="font-size:12px;color:var(--muted);font-weight:400;">· ${KIND_LABEL[kind]}设定</span></div>
    ${it.image_url ? `<img class="sheet-img-preview" src="${it.image_url}" onclick="showLightbox('${it.image_url}')">` : ""}
    <label class="field-label">参考图${isChar ? "（角色参考图/真人脸，最多 9 张）" : ""}</label>
    <div class="sheet-preview-row" id="ref-row">
      ${refs.map((u, j) => `<div style="position:relative"><img src="${u}" onclick="showLightbox('${u}')">
        <button class="img-del" style="width:20px;height:20px;font-size:10px;" onclick="delRef('${kind}',${index},${j})">✕</button></div>`).join("")}
      <button class="ref-add-btn" onclick="addRef('${kind}',${index})">＋<span>上传</span></button>
    </div>
    ${isChar ? `
    <label class="field-label">音色参考（声音克隆素材，5-20 秒清晰人声）</label>
    <div class="sheet-preview-row" id="voice-row">
      ${it.voice_url ? `<div class="voice-ref-item"><audio controls preload="metadata" src="${it.voice_url}"></audio>
        <button class="img-del" style="width:20px;height:20px;font-size:10px;" onclick="delVoice('${kind}',${index})">✕</button></div>` : ""}
      <button class="ref-add-btn" onclick="addVoiceRef('${kind}',${index})">＋<span>上传音频</span></button>
    </div>
    ${it.voice_url ? `
    <label class="field-label">音色参考文本（上面音频里实际念的那句话，必须逐字对应）</label>
    <textarea id="as-voice-text" rows="2" placeholder="照抄上面音频里说的话，例：大家好，欢迎收看今天的节目。">${esc(it.voice_text || "")}</textarea>
    <p class="lib-empty" style="text-align:left;margin-top:2px;font-size:12px;">不是你要合成的台词 —— 它只是帮模型认出这段音频的音色。填错会导致克隆失败（变成随机音色 + 破音）。</p>` : ""}` : ""}
    <label class="field-label">名称</label>
    <input id="as-name" value="${esc(it.name)}">
    <label class="field-label">设定描述</label>
    <textarea id="as-desc" rows="2">${esc(it.description || "")}</textarea>
    <label class="field-label">生图提示词</label>
    <textarea id="as-prompt" rows="5">${esc(it.image_prompt || "")}</textarea>
    <div style="display:flex;align-items:center;margin:6px 0 2px;">
      <button class="btn btn-au btn-sm" onclick="analyzeAU('asset')">😊 AU 表情分析</button>
    </div>
    <div id="as-au"></div>
    ${kind === "scenes" ? furnEditorHtml(it) : ""}
    <label class="field-label">AI 重写指令（可选，如"换成短发"）</label>
    <input id="as-feedback" placeholder="留空则仅规范化提示词">
    <div style="display:flex;gap:8px;margin-top:16px;">
      <button class="btn btn-ghost" style="flex:1;" onclick="aiRewrite('${kind}',${index})">✨ AI 重写</button>
      <button class="btn btn-primary" style="flex:1;" id="as-gen" onclick="genAssetImage('${kind}',${index})">🖼 生成设定图</button>
    </div>
    <button class="btn btn-ghost btn-block" onclick="saveAsset('${kind}',${index})">保存修改</button>
    <button class="btn btn-danger btn-block" onclick="delAsset('${kind}',${index})">🗑 删除此${KIND_LABEL[kind]}</button>
    ${it.image_url ? `<button class="btn btn-danger btn-block" onclick="delAssetImage('${kind}',${index})">删除当前设定图</button>` : ""}
    ${isChar ? `
    <div class="sheet-title" style="margin-top:20px;">👗 服装 / 造型变体 <span class="sheet-sub" style="display:inline;">同一角色不同分镜可换装</span></div>
    ${(it.outfits || []).map((o, oi) => `
      <div style="border:1px solid var(--line);border-radius:12px;padding:10px;margin-bottom:8px;">
        <div class="sheet-preview-row">
          ${o.image_url
            ? `<img src="${o.image_url}" style="width:64px;height:64px;object-fit:cover;border-radius:10px;" onclick="showLightbox('${o.image_url}')">`
            : `<div style="width:64px;height:64px;border-radius:10px;background:var(--accent-soft);display:flex;align-items:center;justify-content:center;font-size:22px;">👗</div>`}
          <input id="of-name-${oi}" value="${esc(o.name)}" placeholder="服装名，如：礼服 / 便装" style="flex:1;">
        </div>
        <textarea id="of-prompt-${oi}" rows="3" placeholder="该服装的生图提示词：保留角色长相锚点，只改服装描述">${esc(o.image_prompt || "")}</textarea>
        <div style="display:flex;gap:8px;margin-top:6px;">
          <button class="btn btn-ghost btn-sm" style="flex:1;" onclick="saveOutfit('${kind}',${index},${oi})">保存</button>
          <button class="btn btn-ghost btn-sm" style="flex:1;" onclick="genOutfitImage('${kind}',${index},${oi})">🖼 生图</button>
          <button class="btn btn-danger btn-sm" onclick="delOutfit('${kind}',${index},${oi})">删除</button>
        </div>
      </div>`).join("")}
    <button class="btn btn-ghost btn-block" onclick="addOutfit('${kind}',${index})">＋ 添加服装</button>
    <p class="lib-empty" style="text-align:left;margin-top:4px;font-size:12px;">服装设定图会自动进入分镜参考池；分镜提示词里写明「穿哪套」，生图时即按该套参考。</p>` : ""}`);
}
function saveAsset(kind, index) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  if (!it) return;
  it.name = $("as-name").value.trim() || it.name;
  it.description = $("as-desc").value.trim();
  it.image_prompt = $("as-prompt").value.trim();
  const vt = $("as-voice-text");
  if (vt) it.voice_text = vt.value.trim();
  touch();
  renderAssets();
  closeSheet();
  toast("已保存");
}
/* ================= 场景家具布置（俯视平面编辑器） =================
   与 3D 导演台的 props 同一套坐标约定：0-1 归一化、上=空间深处/远景、下=靠近镜头、facing 顺时针。
   布置实时保存到场景资产的 furniture 字段；生成设定图时自动翻译成位置文字追加进提示词。 */
const FURN_PRESET = { door: { label: "门", w: 0.14, h: 0.06 }, window: { label: "窗", w: 0.2, h: 0.05 } };
function furnEditorHtml(it) {
  S.furnSel = null;
  const items = it.furniture || [];
  return `
    <div class="sheet-title" style="margin-top:18px;">🪑 家具布置 <span class="sheet-sub" style="display:inline;">俯视平面 · 拖动摆位置 · 生成设定图时自动进提示词</span></div>
    <div id="furn-map" style="position:relative;width:100%;aspect-ratio:4/3;border:1px solid var(--line);border-radius:12px;overflow:hidden;touch-action:none;user-select:none;-webkit-user-select:none;
      background-color:#f8fafc;background-image:linear-gradient(rgba(148,163,184,.22) 1px, transparent 1px),linear-gradient(90deg, rgba(148,163,184,.22) 1px, transparent 1px);background-size:20% 25%;">
      <span style="position:absolute;top:3px;left:50%;transform:translateX(-50%);font-size:10px;color:#94a3b8;pointer-events:none;">↑ 远景 · 空间深处</span>
      <span style="position:absolute;bottom:3px;left:50%;transform:translateX(-50%);font-size:10px;color:#94a3b8;pointer-events:none;">↓ 近景 · 镜头</span>
      <span style="position:absolute;top:50%;left:5px;transform:translateY(-50%);font-size:10px;color:#94a3b8;pointer-events:none;">画左</span>
      <span style="position:absolute;top:50%;right:5px;transform:translateY(-50%);font-size:10px;color:#94a3b8;pointer-events:none;">画右</span>
      ${items.map((f, fi) => furnItemHtml(f, fi)).join("")}
    </div>
    <div style="display:flex;gap:6px;margin-top:8px;align-items:center;flex-wrap:wrap;">
      <button class="btn btn-ghost btn-sm" onclick="furnAdd('door')">🚪 门</button>
      <button class="btn btn-ghost btn-sm" onclick="furnAdd('window')">🪟 窗</button>
      <button class="btn btn-ghost btn-sm" onclick="furnAdd('custom')">📦 自定义家具</button>
      <span id="furn-toolbar" style="display:flex;gap:6px;flex:1;justify-content:flex-end;align-items:center;">
        <span style="font-size:11px;color:var(--muted);">点家具选中后可旋转/改名/删除</span>
      </span>
    </div>
    <p class="lib-empty" style="text-align:left;margin-top:4px;font-size:12px;">家具布置实时保存，无需点保存按钮；生成场景设定图时自动把「位置+朝向」翻译进提示词，AI 重写也保持此布局一致。</p>`;
}
function furnItemHtml(f, fi) {
  const sel = S.furnSel === fi;
  const rot = f.facing || 0;
  return `<div id="furn-item-${fi}" onpointerdown="furnDown(event,${fi})"
    style="position:absolute;left:${(f.x ?? 0.5) * 100}%;top:${(f.y ?? 0.5) * 100}%;width:${(f.w || 0.15) * 100}%;height:${(f.h || 0.1) * 100}%;
      transform:translate(-50%,-50%) rotate(${rot}deg);background:rgba(59,130,246,.3);border:2px solid ${sel ? "#2563eb" : "#60a5fa"};
      border-radius:6px;display:flex;align-items:center;justify-content:center;touch-action:none;cursor:move;overflow:visible;z-index:2;">
    <span style="font-size:11px;color:#1e3a8a;white-space:nowrap;font-weight:600;transform:rotate(${-rot}deg);pointer-events:none;">${esc(f.label || "家具")}</span>
  </div>`;
}
function furnCur() {
  const it = (S.cur.data.story || {})[S.furn.kind]?.[S.furn.index];
  if (!it) return null;
  return it;
}
function furnToolbarHtml() {
  return `<button class="btn btn-ghost btn-sm" onclick="furnRename()">✏️ 改名</button>
    <button class="btn btn-ghost btn-sm" onclick="furnRotate()">↻ 转45°</button>
    <button class="btn btn-ghost btn-sm" onclick="furnDel()">🗑 删除</button>`;
}
function furnRender() {
  const map = $("furn-map");
  if (!map || !S.furn) return;
  const it = furnCur(); if (!it) return;
  map.querySelectorAll("[id^='furn-item-']").forEach(el => el.remove());
  map.insertAdjacentHTML("beforeend", (it.furniture || []).map((f, fi) => furnItemHtml(f, fi)).join(""));
  const tb = $("furn-toolbar");
  if (tb) tb.innerHTML = S.furnSel != null ? furnToolbarHtml()
    : `<span style="font-size:11px;color:var(--muted);">点家具选中后可旋转/改名/删除</span>`;
}
function furnDown(e, fi) {
  const it = furnCur(); if (!it) return;
  const f = (it.furniture || [])[fi]; if (!f) return;
  e.preventDefault(); e.stopPropagation();
  if (S.furnSel !== fi) { S.furnSel = fi; furnRender(); }
  const map = $("furn-map"); if (!map) return;
  const rect = map.getBoundingClientRect();
  const sx = e.clientX, sy = e.clientY, ox = f.x ?? 0.5, oy = f.y ?? 0.5;
  const move = (ev) => {
    f.x = Math.min(0.97, Math.max(0.03, ox + (ev.clientX - sx) / rect.width));
    f.y = Math.min(0.97, Math.max(0.03, oy + (ev.clientY - sy) / rect.height));
    const el = $("furn-item-" + fi);
    if (el) { el.style.left = (f.x * 100) + "%"; el.style.top = (f.y * 100) + "%"; }
  };
  const up = () => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    document.removeEventListener("pointercancel", up);
    touch();
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
  document.addEventListener("pointercancel", up);
}
function furnAdd(type) {
  const it = furnCur(); if (!it) return;
  const arr = it.furniture ||= [];
  if (arr.length >= 12) { toast("家具最多 12 件，先删掉一些", true); return; }
  let label, w, h;
  if (type === "custom") {
    const inp = prompt("家具名称（如：衣柜、木桌、屏风）：", "");
    if (!inp || !inp.trim()) return;
    label = inp.trim().slice(0, 8); w = 0.16; h = 0.1;
  } else {
    ({ label, w, h } = FURN_PRESET[type] || {});
    if (!label) return;
  }
  const n = arr.length;
  arr.push({ label, w, h, facing: 0,
    x: Math.min(0.9, Math.max(0.1, 0.3 + (n % 3) * 0.2)),
    y: Math.min(0.9, Math.max(0.1, 0.35 + Math.floor(n / 3) * 0.18)) });
  S.furnSel = arr.length - 1;
  touch(); furnRender();
}
function furnRotate() {
  const it = furnCur(); if (!it || S.furnSel == null) return;
  const f = (it.furniture || [])[S.furnSel]; if (!f) return;
  f.facing = ((f.facing || 0) + 45) % 360;
  touch(); furnRender();
}
function furnRename() {
  const it = furnCur(); if (!it || S.furnSel == null) return;
  const f = (it.furniture || [])[S.furnSel]; if (!f) return;
  const inp = prompt("修改家具名称：", f.label || "");
  if (inp && inp.trim()) { f.label = inp.trim().slice(0, 8); touch(); furnRender(); }
}
function furnDel() {
  const it = furnCur(); if (!it || S.furnSel == null) return;
  const f = (it.furniture || [])[S.furnSel]; if (!f) return;
  if (!confirm(`删除家具「${f.label || ""}」？`)) return;
  it.furniture.splice(S.furnSel, 1);
  S.furnSel = null;
  touch(); furnRender();
}
/* 家具布置 → 提示词文字块（与后端 _furn_pos_line 同一套词，保证两端一致） */
/* 家具布置 → 紧凑一行布局描述（参考池 meta / 视频环境锁定用）。
   与后端 _furn_brief 同一套词；位置/朝向措辞同 furniturePromptBlock。 */
function furnitureBrief(f) {
  if (!f || !f.length) return "";
  const xs = v => v < 0.33 ? "画左" : v > 0.67 ? "画右" : "画面中部";
  const ys = v => v < 0.33 ? "远景（空间深处）" : v > 0.67 ? "前景（靠近镜头）" : "中景";
  const fs = v => { v = ((v % 360) + 360) % 360;
    return v < 45 || v >= 315 ? "正面朝空间深处" : v < 135 ? "正面朝画右" : v < 225 ? "正面朝向镜头" : "正面朝画左"; };
  return "家具布置（大型家具与门窗的位置、朝向固定，任何镜头不得改变）："
    + f.map(p => `${p.label || "家具"}·${xs(p.x ?? 0.5)}${ys(p.y ?? 0.5)}·${fs(p.facing || 0)}`).join("；") + "。";
}
function furniturePromptBlock(f) {
  if (!f || !f.length) return "";
  const xs = v => v < 0.33 ? "画左" : v > 0.67 ? "画右" : "画面中部";
  const ys = v => v < 0.33 ? "远景（空间深处）" : v > 0.67 ? "前景（靠近镜头）" : "中景";
  const fs = v => { v = ((v % 360) + 360) % 360;
    return v < 45 || v >= 315 ? "正面朝空间深处" : v < 135 ? "正面朝画右" : v < 225 ? "正面朝向镜头" : "正面朝画左"; };
  const lines = f.map(p => `- ${p.label || "家具"}：${xs(p.x ?? 0.5)}${ys(p.y ?? 0.5)}，${fs(p.facing || 0)}`);
  return "\n\n【家具布置·硬约束】该场景的家具陈设按以下俯视平面规划摆放（规划图上方=空间深处/远景，下方=靠近镜头/前景，左=画左，右=画右）：\n"
    + lines.join("；\n")
    + "\n生成画面必须呈现上述全部家具，严格遵守其相对位置与朝向关系，不得增删家具、不得挪动位置。";
}
/* 调度图（俯视/轨迹）精确坐标 → 结构化文字硬约束。
   调度图虽作为参考图传给生图模型，但"看图估位"误差大：门窗挪位、人物站位漂移。
   这里把图纸里人物站位、门窗陈设位置朝向、机位的归一化坐标翻译成"十分位"精确描述，
   与图纸一起下发，模型按文字坐标作画，位置不再漂。坐标约定与 3D 导演台一致：
   x 0→1 = 画左→画右，y 0→1 = 空间深处→镜头跟前，facing 0=朝空间深处、顺时针。 */
/* 分镜的调度图使用模式：precise=精确（plot_data 传后端，按 2D 版机位几何翻译：
   谁入画/画面左右按机位视线换算/透视比例/门窗陈设落位，全套权威约束）；
   loose=参考（不传 plot_data，后端跳过硬约束，图纸只作大致参考可小幅移动）。默认 precise。
   注：坐标→画面落位的翻译只能按机位视角换算，不能按图纸方位直译——
   实测图纸方位直译会和机位可视范围冲突（图纸左≠画面左），模型收到两套矛盾指令后构图乱漂。 */
function stagingModeOf(b) { return b.staging_mode === "loose" ? "loose" : "precise"; }
function toggleStagingMode(bid, kind) {
  const b = board(bid);
  if (!b) return;
  b.staging_mode = stagingModeOf(b) === "precise" ? "loose" : "precise";
  touch();
  renderBoards();
  // 调度图页内切换后重开面板保持上下文；主页面无 kind 时只刷卡片
  if (kind) openDiagramSheet(bid, kind);
  toast(b.staging_mode === "precise"
    ? "已切到精确模式：按机位几何严格翻译调度图（谁入画/画面左右/远近透视/人物与织布机等陈设的前后关系）"
    : "已切到参考模式：调度图只作大致参考，允许小幅移动构图");
}
/* 资产面板内所有输入框（名称/描述/提示词/音色文本）实时同步进数据。
   之前只有「保存修改」按钮落盘，用户改完名字直接点 AI 重写或生成设定图，
   面板重渲染后名字被冲回旧值（如“新场景”）——现在任何操作前都先同步。 */
function syncAssetFields(kind, index) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  if (!it) return null;
  const nm = $("as-name");
  const ds = $("as-desc");
  const pr = $("as-prompt");
  const vt = $("as-voice-text");
  if (nm && nm.value.trim()) it.name = nm.value.trim();
  if (ds) it.description = ds.value.trim();
  if (pr) it.image_prompt = pr.value.trim() || it.image_prompt;
  if (vt) it.voice_text = vt.value.trim();
  touch();
  return it;
}
async function genAssetImage(kind, index) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  const btn = $("as-gen");
  if (!it || !btn) return;
  syncAssetFields(kind, index);   // 名称/描述/提示词先落盘，避免面板重渲染冲掉手改
  it.image_prompt = $("as-prompt").value.trim() || it.image_prompt;
  btn.classList.add("busy");
  btn.textContent = "生成中…";
  const meta = [];
  const urls = [];
  const isCharSheet = kind === "characters";
  for (const u of it.ref_images || []) {
    urls.push(u);
    meta.push({
      kind: isCharSheet ? "face" : "ref",
      name: it.name,
      // v14 融脸修复：写明这是"本人"照片，后端还会追加脸锁定指令
      description: isCharSheet
        ? `角色「${it.name}」的真人脸参考照片：照片中的人物就是这个角色的本人，生成图的面部必须与此照片高度一致`
        : "用户上传的参考图",
    });
  }
  try {
    const r = await api("/api/generate-image", {
      prompt: kind === "scenes" ? (it.image_prompt || "") + furniturePromptBlock(it.furniture) : it.image_prompt,
      reference_urls: urls.length ? urls : undefined,
      reference_meta: meta.length ? meta : undefined,
      size: "2048x2048",
    });
    it.image_url = r.image_url;
    touch(); renderAssets(); openAssetSheet(kind, index);
    if (r.warnings) toast(r.warnings, true);
    else toast("设定图已生成");
  } catch (e) { toast(e.message, true); openAssetSheet(kind, index); }
}
/* ---------- 角色服装/造型变体：同一人物多套服装，各带独立设定图 ---------- */
function addOutfit(kind, index) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  if (!it) return;
  it.outfits ||= [];
  it.outfits.push({ name: `服装${it.outfits.length + 1}`, image_prompt: "" });
  touch(); openAssetSheet(kind, index);
}
function saveOutfit(kind, index, oi) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  const o = it?.outfits?.[oi];
  if (!o) return;
  o.name = ($(`of-name-${oi}`)?.value || "").trim() || o.name;
  o.image_prompt = ($(`of-prompt-${oi}`)?.value || "").trim();
  touch(); toast("已保存");
}
function delOutfit(kind, index, oi) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  const o = it?.outfits?.[oi];
  if (!o) return;
  if (!confirm(`删除服装「${o.name}」？（分镜参考池里将不再出现这套服装）`)) return;
  it.outfits.splice(oi, 1);
  touch(); openAssetSheet(kind, index);
}
async function genOutfitImage(kind, index, oi) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  const o = it?.outfits?.[oi];
  if (!o) return;
  o.name = ($(`of-name-${oi}`)?.value || "").trim() || o.name;
  o.image_prompt = ($(`of-prompt-${oi}`)?.value || "").trim();
  if (!o.image_prompt) return toast("先填该服装的生图提示词", true);
  showBusy("正在生成服装设定图…", o.name);
  try {
    // 身份锚点：角色本体设定图（换装不换脸）+ 真人脸参考
    const urls = [], meta = [];
    if (it.image_url) {
      urls.push(it.image_url);
      meta.push({ kind: "character", name: it.name, description: `角色「${it.name}」的官方设定图（面部/发型/体型必须与此图一致，只更换服装）：${it.image_prompt || ""}` });
    }
    for (const u of it.ref_images || []) {
      urls.push(u);
      meta.push({ kind: "face", name: it.name, description: `角色「${it.name}」的真人脸参考照片，画面中该角色的面部必须与照片高度一致` });
    }
    const r = await api("/api/generate-image", {
      prompt: o.image_prompt,
      reference_urls: urls.length ? urls : undefined,
      reference_meta: meta.length ? meta : undefined,
      size: "2048x2048",
    });
    o.image_url = r.image_url;
    hideBusy(); touch(); openAssetSheet(kind, index);
    if (r.warnings) toast(r.warnings, true);
    else toast("服装设定图已生成");
  } catch (e) { hideBusy(); toast(e.message, true); }
}
async function aiRewrite(kind, index) {
  const st = S.cur.data.story;
  syncAssetFields(kind, index);   // 先同步框内名称/描述/提示词，重写后重渲染不会丢
  const it = st[kind]?.[index];
  if (!it) return;
  const feedback = ($("as-feedback").value || "").trim();
  // 修改框当前内容优先：用户改完没点「保存」直接点 AI 重写，也必须按框里最新内容重写
  const desc = (($("as-desc")?.value ?? it.description) || "").trim();
  const oldPrompt = (($("as-prompt")?.value ?? it.image_prompt) || "").trim();
  showBusy("AI 正在重写提示词…", "结合画风圣经与角色锚点");
  try {
    const r = await api("/api/regenerate-prompt", {
      type: kind === "characters" ? "character" : kind === "scenes" ? "scene" : "prop",
      name: it.name, description: desc,
      style_bible: st.style_bible || "",
      ...assetPayload(),
      furniture: kind === "scenes" ? (it.furniture || undefined) : undefined,
      old_prompt: oldPrompt, feedback: feedback || undefined,
    });
    it.description = desc;            // 框内描述一并写回，避免保存后又改回去
    it.image_prompt = keepParams(r.image_prompt || oldPrompt || it.image_prompt, oldPrompt);
    hideBusy(); touch(); openAssetSheet(kind, index);
    toast("提示词已重写，可检查后生成");
  } catch (e) { hideBusy(); toast(e.message, true); }
}
function addRef(kind, index) { S.refTarget = { kind, index }; $("ref-file-input").click(); }
/* 参考图上传分发：目标为分镜时走 onBoardRefImages，否则走资产上传 */
function onRefFilePicked(input) {
  if (S.boardRefTarget) onBoardRefImages(input);
  else onRefImages(input);
}
async function onRefImages(input) {
  // 注意：必须先把文件列表取出来再清 value——直接 input.value="" 会把已选文件一并清空，
  // 循环遍历时 files 已是空数组，上传静默失败、缩略图也不出现（v16 修复）
  const t = S.refTarget;
  S.refTarget = null;
  const files = Array.from(input.files || []);
  input.value = "";
  if (!t || !files.length) return;
  const it = (S.cur.data.story || {})[t.kind]?.[t.index];
  if (!it) return;
  it.ref_images ||= [];
  for (const file of files) {
    if (it.ref_images.length >= 9) break;
    const dataUrl = await new Promise((res) => {
      const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(file);
    });
    try {
      const r = await api("/api/upload-image", { image_base64: dataUrl, filename: file.name });
      it.ref_images.push(r.image_url);
    } catch (e) { toast(e.message, true); break; }
  }
  touch(); openAssetSheet(t.kind, t.index); renderAssets();
}
function delRef(kind, index, j) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  if (!it) return;
  it.ref_images.splice(j, 1);
  touch(); openAssetSheet(kind, index);
}
/* ---------- 分镜参考图：上传 / 删除（构图/氛围参考，生图时并入参考池） ---------- */
function addBoardRef(bid) { S.boardRefTarget = bid; $("ref-file-input").click(); }
async function onBoardRefImages(input) {
  const bid = S.boardRefTarget;
  S.boardRefTarget = null;
  const files = Array.from(input.files || []);
  input.value = "";
  if (!bid || !files.length) return;
  const b = board(bid);
  if (!b) return;
  b.ref_images ||= [];
  for (const file of files) {
    if (b.ref_images.length >= 9) break;
    const dataUrl = await new Promise((res) => {
      const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(file);
    });
    try {
      const r = await api("/api/upload-image", { image_base64: dataUrl, filename: file.name });
      b.ref_images.push(r.image_url);
    } catch (e) { toast(e.message, true); break; }
  }
  touch();
  openBoardSheet(bid);
  toast("已上传为参考图（非底图）：生图时会与调度图一起进参考池");
}
function delBoardRef(bid, j) {
  const b = board(bid);
  if (!b) return;
  b.ref_images.splice(j, 1);
  touch(); openBoardSheet(bid);
}
/* ---------- 角色音色参考：上传 / 删除（声音克隆素材，随引用清理） ---------- */
function addVoiceRef(kind, index) { S.voiceTarget = { kind, index }; $("voice-file-input").click(); }
async function onVoiceAudio(input) {
  const t = S.voiceTarget;
  S.voiceTarget = null;
  const file = (input.files || [])[0];
  input.value = "";
  if (!t || !file) return;
  const it = (S.cur.data.story || {})[t.kind]?.[t.index];
  if (!it) return;
  const dataUrl = await new Promise((res) => {
    const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(file);
  });
  showBusy("上传音色参考…");
  const old = it.voice_url;
  try {
    const r = await api("/api/upload-voice", { audio_base64: dataUrl, filename: file.name });
    it.voice_url = r.voice_url;
    touch();
    if (old && old !== r.voice_url) purgeServerFiles([old]);
    toast("音色已上传，记得填写「音色参考文本」");
  } catch (e) { toast(e.message, true); }
  hideBusy();
  openAssetSheet(t.kind, t.index);
}
function delVoice(kind, index) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  if (!it) return;
  const old = it.voice_url;
  it.voice_url = null;
  it.voice_text = "";
  touch();
  if (old) purgeServerFiles([old]);
  openAssetSheet(kind, index);
}
function delAssetImage(kind, index) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  if (!it) return;
  it.image_url = null;
  touch(); renderAssets(); openAssetSheet(kind, index);
}
function delAsset(kind, index) {
  const st = S.cur.data.story || {};
  const it = st[kind]?.[index];
  if (!it) return;
  if (!confirm(`确定删除${KIND_LABEL[kind]}「${it.name}」？\n其设定图、参考图、服装图、音色文件将从服务器一并删除，删除后不可恢复。`)) return;
  // 收集服务器上的关联文件：本体设定图 + 参考图 + 音色 + 各套服装图
  const urls = [it.image_url, it.voice_url, ...(it.ref_images || []),
    ...((it.outfits || []).map((o) => o.image_url))].filter(Boolean);
  st[kind].splice(index, 1);
  touch(); closeSheet(); renderAssets();
  purgeServerFiles(urls);   // 先落库移除引用，服务器再安全清理文件
  toast("已删除");
}

/* ================= 分镜页 ================= */
function board(id) { return S.cur.data.boards.find((b) => b.id === id); }
function healStaleVideo() {
  // 兜底自愈：手机浏览器休眠杀定时器后，"渲染中"可能永远停在原地。超时/无任务 ID 自动转失败并写明原因
  if (!S.cur) return;
  let healed = false;
  for (const b of S.cur.data.boards || []) {
    if (b.videoStatus !== "running") continue;
    if (b.videoSubmitting) {
      // 提交请求还没返回：没有任务 ID 属正常，不误判。但后端 httpx 超时 60s，
      // 超过 2 分钟仍没拿到任务 ID 说明请求已经死了（如手机休眠杀掉连接），按失败处理
      if (!(b.videoStartedAt && Date.now() - b.videoStartedAt > 120000)) continue;
      b.videoSubmitting = false;
    }
    if (!b.videoTaskId) {
      b.videoStatus = "failed";
      b.videoError = "任务没有提交成功（没有拿到任务 ID），请重试";
      healed = true;
    } else if (b.videoStartedAt && Date.now() - b.videoStartedAt > 1200000) {
      b.videoStatus = "failed";
      b.videoError = "渲染超过 20 分钟未完成，已自动停止，请重试（若平台侧其实已完成，重试会重新生成）";
      stopPoll(b.id);
      healed = true;
    }
  }
  if (healed) touch();
}
function renderBoards() {
  healStaleVideo();
  const list = $("board-list");
  const boards = S.cur.data.boards;
  const landscape = S.cur.data.orientation === "landscape";
  if (!boards.length) {
    list.innerHTML = `<div class="card pad" style="text-align:center;color:var(--muted);font-size:13.5px;">
      还没有分镜：在「故事」页生成剧本，或点上方「➕ 新增分镜」手动添加</div>`;
    return;
  }
  const genAreaStyle = landscape ? "width:200px;height:150px;" : "width:150px;height:200px;";
  list.innerHTML = boards.map((b) => {
    const genImg = b.imageBusy ? `
      <div class="board-gen-area">
        <div class="gen-progress"><div class="gen-bar"><div class="gen-bar-inner" style="width:70%;"></div></div></div>
        <div class="gen-text">正在生成画面… 约 20–40 秒</div>
      </div>`
      : b.images.length ? `
      <div class="board-imgs">
        ${b.images.map((u, j) => `<div class="board-img-item">
          <img src="${u}" loading="lazy" onclick="showLightbox('${u}')">
          <button class="img-del" onclick="delBoardImage('${b.id}',${j})">✕</button>
        </div>`).join("")}
        <div class="board-img-item" onclick="genBoardImage('${b.id}')">
          <div class="board-gen-area" style="${genAreaStyle}">＋<span style="font-size:11px;">再生成一张</span></div>
        </div>
      </div>`
      : `<div class="board-gen-area" style="${genAreaStyle}" onclick="genBoardImage('${b.id}')">
          🖼<span>点击生成分镜画面</span><span style="font-size:11px;">约 20–40 秒 · 自动携带角色/场景参考</span>
        </div>`;
    // 视频区 = 版本条（多版本横滑，宽度与分镜缩略图一致，占位比原来整宽视频小一半以上）
    //        + 状态区（提交中/渲染中/失败），两者独立显示：重新生成时旧版本仍可见
    const vidW = landscape ? 280 : 176;
    const videoStrip = (b.videos && b.videos.length) ? `
      <div class="board-videos">
        ${b.videos.map((u, vi) => `
        <div class="video-item" style="width:${vidW}px;">
          <video src="${u}" controls playsinline preload="metadata" onerror="videoLoadErr('${b.id}',${vi})"></video>
          <div class="video-item-bar">
            <span class="video-tag${vi === b.videos.length - 1 ? " newest" : ""}">${vi === b.videos.length - 1 ? "最新" : "版本" + (vi + 1)}</span>
            <span style="flex:1;"></span>
            <button class="vbtn" title="保存此版本" onclick="saveVideoToPhone('${b.id}',${vi})">⬇️</button>
            <button class="vbtn" title="删除此版本" onclick="delBoardVideo('${b.id}',${vi})">🗑</button>
          </div>
        </div>`).join("")}
      </div>` : "";
    const videoState = b.videoStatus === "running" ? `
      <div class="board-gen-area" style="height:auto;padding:16px;">
        <div class="gen-progress"><div class="gen-bar"><div class="gen-bar-inner" id="vp-${b.id}" style="width:8%;"></div></div></div>
        <div class="gen-text" id="vt-${b.id}">${b.videoSubmitting
          ? "提交任务中…（翻译调度/上传参考图，约 10–60 秒）"
          : "云端渲染中…（约 1–3 分钟）"}</div>
      </div>`
      : b.videoStatus === "failed" ? `
      <div class="board-gen-area" style="height:auto;padding:14px;color:var(--danger);position:relative;">
        生成失败，请重试${b.videoError ? `<div style="font-size:12px;margin-top:6px;opacity:.85;">原因：${esc(b.videoError)}</div>` : ""}
        <button class="vbtn" title="关闭报错" onclick="dismissVideoError('${b.id}')" style="position:absolute;top:6px;right:6px;">✕</button>
      </div>` : "";
    const videoPart = (videoStrip || videoState) ? `<div class="board-video">${videoStrip}${videoState}</div>` : "";
    // 配音条：已生成的配音（音色名 + 播放器 + 删除），紧挨视频区
    const dubStrip = (b.dubs && b.dubs.length) ? `<div class="board-dubs">
      ${b.dubs.map((d, di) => `
      <div class="dub-item">
        <span class="dub-tag">🔊 ${esc(d.voice || "随机音色")}</span>
        <audio controls preload="metadata" src="${d.url}" onerror="delDubAudio('${b.id}',${di})"></audio>
        <button class="vbtn" title="下载此配音" onclick="downloadDub('${b.id}',${di})">⬇</button>
        <button class="vbtn" title="删除此配音" onclick="delDub('${b.id}',${di})">🗑</button>
      </div>`).join("")}
    </div>` : "";
    const hasDiag = !!(b.topView || b.trajView);
    const canVideo = b.images.length > 0 && b.videoStatus !== "running";
    // 当前运镜（字段优先，老数据从正文解析）——显示在卡片上，点一下进生成弹窗
    const curMove = (b.shot_params && b.shot_params.video && b.shot_params.video["运镜"]) || readParam(b.video_prompt || "", "运镜");
    return `
    <div class="board-card" data-bid="${b.id}">
      <div class="board-head">
        <div class="drag-handle" title="按住拖动排序">⠿</div>
        <div class="shot-badge">${b.shot_number}${b.linkPrev ? ' <span title="接上一镜：以上一镜头末帧为衔接参考">🔗</span>' : ""}</div>
        <div class="board-desc">${esc(b.description)}
          ${b.dialogue ? `<div class="board-dialogue">${esc(b.dialogue)}</div>` : ""}
        </div>
        <div class="dur-chip">${b.duration}s</div>
        <div class="head-ops">
          <button class="head-op" onclick="addBoardAfter('${b.id}')" title="在此镜头后插入新分镜">＋</button>
          <button class="head-op head-op-del board-del" onclick="delBoard('${b.id}')" title="删除此分镜">－</button>
        </div>
      </div>
      <div class="board-media">${genImg}</div>
      ${videoPart}
      ${dubStrip}
      ${curMove ? `<button class="move-chip" onclick="openVideoSheet('${b.id}')" title="点一下重新选运镜生成">🎥 运镜：${esc(curMove)}</button>` : ""}
      <div class="board-actions">
        <button class="btn btn-ghost" onclick="genBoardImage('${b.id}')">🖼 生图</button>
        <button class="btn btn-ghost" onclick="addBoardRef('${b.id}')" title="直接上传图片作为本镜参考图（不作为底图，生图时与调度图一起进参考池）">📤 传图</button>
        <button class="btn btn-ghost" onclick="openDiagramSheet('${b.id}')">🗺 调度图${hasDiag ? " ✅" : ""}${stagingModeOf(b) === "precise" ? " 🎯" : " 🌫"}</button>
        <button class="btn btn-ghost" onclick="openBoardSheet('${b.id}')">✏️ 提示词</button>
        <button class="btn btn-ghost" onclick="openDubSheet('${b.id}')">🔊 配音${b.dubs && b.dubs.length ? " ✅" : ""}</button>
        <button class="btn btn-primary" ${canVideo ? "" : "disabled"} onclick="openVideoSheet('${b.id}')">🎬 视频</button>
      </div>
    </div>`;
  }).join("");
  bindBoardDnD();
}
/* ---------- 分镜管理：新增 / 删除 / 拖拽排序 ---------- */
function renumberBoards() {
  (S.cur?.data.boards || []).forEach((b, i) => { b.shot_number = i + 1; });
}
function addBoardAfter(bid) {
  if (!S.cur) return;
  const boards = S.cur.data.boards;
  const nb = {
    id: "b" + Date.now().toString(36) + Math.random().toString(36).slice(2, 5),
    shot_number: boards.length + 1,
    description: "（新分镜 · 点「✏️ 提示词」编辑描述与提示词）",
    dialogue: "", duration: 3, image_prompt: "", video_prompt: "",
    images: [], videoUrl: null, videos: [], videoTaskId: null, videoStatus: null, videoError: null,
  };
  const idx = bid ? boards.findIndex((b) => b.id === bid) : boards.length - 1;
  boards.splice(idx + 1, 0, nb);
  renumberBoards();
  // 接上一帧：新分镜默认开启衔接，并继承上一镜的手动参考图（生成时末帧自动进参考池）
  const prev = boards[idx];
  if (prev) {
    nb.linkPrev = true;
    if (prev.ref_images && prev.ref_images.length) nb.ref_images = [...prev.ref_images];
  }
  touch(); renderBoards();
  $("board-list").querySelector(`[data-bid="${nb.id}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  toast(prev && (prev.images || []).length
    ? `已新增第 ${nb.shot_number} 个分镜：已开启接上一镜（生成时以镜头 ${prev.shot_number} 末帧为衔接参考）`
    : `已新增第 ${nb.shot_number} 个分镜`);
}
function delBoard(bid) {
  const boards = S.cur?.data.boards || [];
  const idx = boards.findIndex((b) => b.id === bid);
  if (idx < 0) return;
  const b = boards[idx];
  if (!confirm(`确定删除分镜 ${b.shot_number}「${(b.description || "").slice(0, 20)}」吗？\n删除后不可恢复，已生成的图片/视频文件也将从服务器一并删除。`)) return;
  if (b.videoStatus === "running") stopPoll(b.id);
  // 收集该分镜在服务器上的全部文件，删除分镜后一并清理
  const stale = [...(b.images || []), ...(b.videos || []), b.videoUrl, b.trajView, b.topView,
    ...((b.dubs || []).map((d) => d.url))].filter(Boolean);
  boards.splice(idx, 1);
  renumberBoards();
  touch(); renderBoards();
  toast("分镜已删除");
  purgeServerFiles(stale);
}
let bDrag = null;      // {bid, card, active, startY, mode: "touch"|"mouse", tid}
let pressTimer = null; // 长按计时器（长按卡片空白处也可拖动）
function bindBoardDnD() {
  const list = $("board-list");
  if (!list) return;
  // 触屏：用原生 touch 事件（兼容性最好，绕开各家浏览器 pointer 事件差异），事件委托在 list 上
  if (!list._dndBound) {
    list._dndBound = true;
    list.addEventListener("touchstart", listTouchStart, { passive: false });
    // 鼠标（电脑调试用）：把手 pointerdown 直接拖
    list.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "mouse" || bDrag) return;
      const handle = e.target.closest(".drag-handle");
      if (!handle) return;
      const card = handle.closest(".board-card");
      if (!card || !S.cur || S.cur.data.boards.length < 2) return;
      e.preventDefault();
      beginDrag(card, "m" + e.pointerId, e.clientY, "mouse");
    });
  }
}
/* 触屏入口：
   - 按住 ⠿ 把手 → 立即进入拖拽
   - 长按卡片空白处 0.45s → 进入拖拽（按钮/图片/视频除外；启动前移动>10px 视为滚动并取消） */
function listTouchStart(e) {
  if (bDrag) return;
  if (!S.cur || S.cur.data.boards.length < 2) return;
  const touch = e.touches && e.touches[0];
  if (!touch) return;
  const card = e.target.closest(".board-card");
  if (!card) return;
  const handle = e.target.closest(".drag-handle");
  if (handle) {
    e.preventDefault();
    beginDrag(card, "t" + touch.identifier, touch.clientY, "touch");
    try { navigator.vibrate && navigator.vibrate(15); } catch (_) {}
    return;
  }
  if (e.target.closest("button,video,img,input,textarea,select,a,.board-video")) return;
  const x0 = touch.clientX, y0 = touch.clientY;
  const cancel = () => {
    clearTimeout(pressTimer); pressTimer = null;
    document.removeEventListener("touchmove", pressMove);
    document.removeEventListener("touchend", cancel);
    document.removeEventListener("touchcancel", cancel);
  };
  const pressMove = (ev) => {
    const t = ev.touches && ev.touches[0];
    if (t && (Math.abs(t.clientY - y0) > 10 || Math.abs(t.clientX - x0) > 10)) cancel();
  };
  clearTimeout(pressTimer);
  document.addEventListener("touchmove", pressMove, { passive: true });
  document.addEventListener("touchend", cancel);
  document.addEventListener("touchcancel", cancel);
  pressTimer = setTimeout(() => {
    clearTimeout(pressTimer); pressTimer = null;
    document.removeEventListener("touchmove", pressMove);
    document.removeEventListener("touchend", cancel);
    document.removeEventListener("touchcancel", cancel);
    if (bDrag) return;
    beginDrag(card, "t" + touch.identifier, y0, "touch");
    try { navigator.vibrate && navigator.vibrate(20); } catch (_) {}
  }, 450);
}
function beginDrag(card, tid, startY, mode) {
  bDrag = { bid: card.dataset.bid, card, active: false, startY, mode, tid };
  // 不能用 setPointerCapture——卡片在 DOM 里移动位置会导致捕获被释放、后续 move 收不到。
  // 统一挂 document 级监听。
  if (mode === "touch") {
    document.addEventListener("touchmove", dragTouchMove, { passive: false });
    document.addEventListener("touchend", dragTouchEnd);
    document.addEventListener("touchcancel", dragTouchEnd);
  } else {
    document.addEventListener("pointermove", dragPointerMove, { passive: false });
    document.addEventListener("pointerup", dragPointerEnd);
    document.addEventListener("pointercancel", dragPointerEnd);
  }
}
function applyDragPosition(clientY) {
  if (!bDrag.active) {
    if (Math.abs(clientY - bDrag.startY) < 10) return;
    bDrag.active = true;
    bDrag.card.classList.add("dragging");
    document.body.classList.add("board-drag-noselect");
  }
  const cards = [...$("board-list").querySelectorAll(".board-card:not(.dragging)")];
  const after = cards.filter((c) => {
    const r = c.getBoundingClientRect();
    return clientY > r.top + r.height / 2;
  }).pop();
  if (after) {
    if (after.nextElementSibling !== bDrag.card) after.after(bDrag.card);
  } else {
    $("board-list").prepend(bDrag.card);
  }
  // 拖到视口上下边缘时自动滚动（滚动容器是 #studio-body，不是 window）
  const sc = document.getElementById("studio-body");
  const vh = window.innerHeight;
  if (sc && clientY < 90) sc.scrollTop -= 16;
  else if (sc && clientY > vh - 90) sc.scrollTop += 16;
}
function dragTouchMove(e) {
  if (!bDrag || bDrag.mode !== "touch") return;
  const t = [...(e.touches || [])].find((x) => "t" + x.identifier === bDrag.tid) || (e.touches && e.touches[0]);
  if (!t) return;
  e.preventDefault(); // 拖拽中禁止页面滚动
  applyDragPosition(t.clientY);
}
function dragTouchEnd(e) {
  if (!bDrag || bDrag.mode !== "touch") return;
  document.removeEventListener("touchmove", dragTouchMove);
  document.removeEventListener("touchend", dragTouchEnd);
  document.removeEventListener("touchcancel", dragTouchEnd);
  finishDrag();
}
function dragPointerMove(e) {
  if (!bDrag || bDrag.mode !== "mouse" || bDrag.tid !== "m" + e.pointerId) return;
  applyDragPosition(e.clientY);
}
function dragPointerEnd(e) {
  if (!bDrag || bDrag.mode !== "mouse") return;
  document.removeEventListener("pointermove", dragPointerMove);
  document.removeEventListener("pointerup", dragPointerEnd);
  document.removeEventListener("pointercancel", dragPointerEnd);
  finishDrag();
}
function finishDrag() {
  if (!bDrag) return;
  const { card, active } = bDrag;
  bDrag = null;
  if (!active) return;
  card.classList.remove("dragging");
  document.body.classList.remove("board-drag-noselect");
  // 按新 DOM 顺序重排数据并重新编号
  const boards = S.cur.data.boards;
  const order = [...$("board-list").querySelectorAll(".board-card")].map((c) => c.dataset.bid);
  boards.sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
  renumberBoards();
  touch(); renderBoards();
}
// 拖拽进行中：禁止系统长按菜单（touchmove 滚动已在 dragTouchMove 里 preventDefault）
// 注：▲▼ 微调按钮已按需求移除，排序靠长按拖拽（bindBoardDnD）完成
document.addEventListener("contextmenu", (e) => { if (bDrag && bDrag.active) e.preventDefault(); });
function delBoardImage(bid, j) {
  const b = board(bid);
  if (!b) return;
  const removed = b.images[j];
  if (!confirm("删除这张图？服务器上的文件将一并删除，无法恢复。")) return;
  b.images.splice(j, 1);
  touch(); renderBoards();
  purgeServerFiles([removed]); // 服务器文件同步删除（仍被其他项目引用时会自动保留）
}
/* ---------- 画面参数下拉：景别 / 景深 / 机位 / 焦段 / 构图 / 光线（+ 运镜） ----------
   设计：选中的参数以「标签：值」形式写进提示词正文，而不是存独立字段。
   原因——写进正文后，参数跟着正文一起走，重写时也能被识别并强制保留；存字段反而容易在重写时丢。
   优先级：手动选定的参数 > AI 生成（见 keepParams）；同组切换只替换不堆叠；打开抽屉时按正文反解析回填下拉。 */
/* 选项可以是纯字符串，也可以是 {label, value}：
   label 是下拉里显示的短名，value 是真正写进提示词的文本——带上量化的画面效果，
   否则「景别：远景」这种单字值对生图模型约束太弱，人像照样画得很大。 */
const SHOT_PARAMS = [
  { label: "景别", opts: [
    { label: "大远景", value: "大远景 extreme wide shot（人物极小 约占画面高度8% 环境占90%以上）" },
    { label: "远景", value: "远景 wide shot（人物较小 约占画面高度20% 四周大量环境留白）" },
    { label: "全景", value: "全景 full shot（全身完整入画 头顶脚下留白）" },
    { label: "中景", value: "中景 medium shot（腰部以上入画）" },
    { label: "中近景", value: "中近景 medium close-up（胸部以上入画）" },
    { label: "近景", value: "近景 close shot（肩部以上入画）" },
    { label: "特写", value: "特写 close-up（面部充满画面）" },
    { label: "大特写", value: "大特写 extreme close-up（局部充满画面 如眼睛或手）" },
  ] },
  { label: "景深", opts: ["超浅景深", "浅景深", "中等景深", "深景深"] },
  { label: "机位", opts: ["平视", "低角度仰拍", "高角度俯拍", "鸟瞰", "荷兰角", "过肩", "主观视角"] },
  { label: "焦段", opts: [
    { label: "14mm 超广角", value: "14mm 超广角（强烈透视 空间拉伸 人物易显小）" },
    { label: "24mm 广角", value: "24mm 广角（视野开阔 边缘轻微变形）" },
    { label: "35mm 人文广角", value: "35mm 人文广角（接近人眼 自然不夸张）" },
    { label: "50mm 标准", value: "50mm 标准（视角平实 无透视变形）" },
    { label: "85mm 中长焦", value: "85mm 中长焦（压缩空间 背景柔化）" },
    { label: "135mm 长焦", value: "135mm 长焦（强空间压缩 背景大幅虚化）" },
  ] },
  { label: "构图", opts: ["三分法", "中心对称", "对角线", "框架式", "引导线", "前景遮挡", "大量留白"] },
  { label: "光线", opts: ["顺光", "侧光", "逆光轮廓光", "顶光", "底光", "伦勃朗光", "柔光散射", "硬光直射"] },
];
const MOVE_PARAMS = [
  { label: "运镜", opts: ["固定镜头", "缓缓推近", "缓缓拉远", "横移", "摇镜", "跟随", "环绕", "升降"] },
];
const ALL_PARAMS = SHOT_PARAMS.concat(MOVE_PARAMS);

function _paramRe(label) {
  // 分隔符含「】」，否则「【镜头细节】景别：中景」这种紧贴小节标题的写法识别不到
  return new RegExp("(^|[，,；;、。\\n】])\\s*" + label + "\\s*[:：][^，,；;、。\\n】]*", "g");
}
/** 从提示词正文读出某组已选值（没有则空串） */
function readParam(text, label) {
  const m = (text || "").match(new RegExp(label + "\\s*[:：]\\s*([^，,；;、。\\n】]*)"));
  return m ? (m[1] || "").trim() : "";
}
/** 写入/替换/删除某组参数；value 为空 = 删除该组 */
function setParam(text, label, value) {
  let t = (text || "").replace(_paramRe(label), "$1");
  // 删掉一段后清理残留分隔符：连续逗号、行首/行尾孤立的标点
  t = t.replace(/([，,；;、])\s*[，,；;、]/g, "$1")
       .replace(/^[，,；;、。\s]+/, "")
       .replace(/[，,；;、。\s]+$/g, "")
       .trim();
  if (value) t = (t ? t + "，" : "") + label + "：" + value;
  return t;
}
/** 选项归一化：支持纯字符串与 {label, value} 两种写法 */
function optLabel(o) { return typeof o === "string" ? o : o.label; }
function optValue(o) { return typeof o === "string" ? o : o.value; }
/** 把正文/字段里的值对齐到某个选项的完整 value（兼容旧数据里只存了「远景」的情况） */
function matchOptValue(group, v) {
  if (!v) return "";
  const opts = group.opts.map(optValue);
  if (opts.includes(v)) return v;
  const hit = opts.find((o) => o.startsWith(v + " ") || o.startsWith(v + "（") || o.startsWith(v + "("));
  return hit || v;   // 对不上就原样保留，别把用户的值吃掉
}
function paramGridHtml(taId, groups) {
  return `<div class="param-grid" id="pg-${taId}">${groups.map((g) => `
    <label class="param-cell"><span>${g.label}</span>
      <select onchange="onParamPick('${taId}','${g.label}',this.value)">
        <option value="">不指定</option>
        ${g.opts.map((o) => `<option value="${esc(optValue(o))}">${esc(optLabel(o))}</option>`).join("")}
      </select>
    </label>`).join("")}</div>`;
}
/** 当前抽屉里选中的参数（独立于正文保存，清空提示词框也不会丢） */
let SHEET_PARAMS = { img: {}, video: {} };
let SHEET_BID = null; // 当前打开提示词弹窗的分镜 ID：下拉选参数时实时同步进数据用
function parseParams(text, groups) {
  const out = {};
  groups.forEach((g) => { const v = readParam(text, g.label); if (v) out[g.label] = v; });
  return out;
}
/** 把参数字典写回正文（缺的补上、已有同组的替换） */
function applyParams(text, params) {
  let t = text || "";
  ALL_PARAMS.forEach((g) => { const v = params && params[g.label]; if (v) t = setParam(t, g.label, v); });
  return t;
}
/** 景别与焦段互相打架时提醒（长焦压缩空间，会把远景的人拉近显大） */
function checkParamConflict(bag) {
  const shot = bag["景别"] || "", lens = bag["焦段"] || "";
  if (!shot || !lens) return;
  if (shot.includes("远景") && (lens.startsWith("85mm") || lens.startsWith("135mm"))) {
    toast("长焦会压缩空间、把人物拉近显大；想让人小小一个，建议配 24mm 广角", true);
  }
}
function onParamPick(taId, label, value) {
  const ta = $(taId);
  const slot = taId === "bd-vidp" ? "video" : "img";
  const bag = SHEET_PARAMS[slot] || (SHEET_PARAMS[slot] = {});
  if (value) bag[label] = value; else delete bag[label];
  if (ta) ta.value = setParam(ta.value, label, value);
  refreshPromptView(taId);
  // 下拉选参数是程序改 textarea、不触发 oninput：同样实时同步，保证未点保存也能带去生成
  if (SHEET_BID && ta) livePromptSync(SHEET_BID, slot, ta.value);
  if (slot === "img") checkParamConflict(bag);
}
/** 打开抽屉后：把已选参数回填到下拉 */
function syncParamGrid(taId, groups, params) {
  const grid = $("pg-" + taId);
  if (!grid) return;
  const sels = grid.querySelectorAll("select");
  groups.forEach((g, i) => {
    if (!sels[i]) return;
    const v = (params && params[g.label]) || "";
    sels[i].value = v ? matchOptValue(g, v) : "";
    // 旧数据只存了「远景」这类短值、对不上新选项时，顺手把完整值补进字段
    if (v && sels[i].value && sels[i].value !== v && params) params[g.label] = sels[i].value;
  });
}
/** AI 重写后：你手动选定的参数一律覆盖 AI 写出来的值（手动优先，不是兜底）
    —— 旧文本里有的组，重写结果里不管 AI 写成什么，都强制改回你选的那个；
    —— 旧文本里没有的组，才让 AI 自己判断。 */
function keepParams(newText, oldText) {
  let t = newText || "";
  ALL_PARAMS.forEach((g) => {
    const mine = readParam(oldText, g.label);
    if (mine) t = setParam(t, g.label, mine);
  });
  return t;
}

function openBoardSheet(bid) {
  const b = board(bid);
  if (!b) return;
  SHEET_BID = bid;
  // 参数以独立字段为准（老数据没有该字段时，从正文里解析出来作为初值）
  const sp = b.shot_params || {};
  SHEET_PARAMS = {
    img: { ...parseParams(b.image_prompt, SHOT_PARAMS), ...(sp.img || {}) },
    video: { ...parseParams(b.video_prompt, MOVE_PARAMS), ...(sp.video || {}) },
  };
  openSheet(`
    <div class="sheet-title">镜头 ${b.shot_number} · 提示词</div>
    <div class="sheet-sub">改完记得保存；下拉里选的参数会压过 AI 重写的版本</div>
    <label class="field-label">剧情作用</label>
    <textarea id="bd-desc" rows="2">${esc(b.description)}</textarea>
    <label class="field-label">台词 / 旁白（没有留空）</label>
    <textarea id="bd-dialogue" rows="2">${esc(b.dialogue || "")}</textarea>
    <label class="field-label">时长（2–30 秒；豆包引擎 4–30 秒，AutoDL 工作流建议 ≤15 秒）</label>
    <input id="bd-dur" type="number" min="2" max="30" value="${b.duration}">
    <label style="display:flex;align-items:center;gap:8px;margin:8px 0;font-size:13.5px;">
      <input type="checkbox" id="bd-linkprev" ${b.linkPrev ? "checked" : ""}>
      🔗 接上一镜（以上一镜头最后一帧为参考，场景/人物/光线无缝衔接）
    </label>
    <div style="font-size:12px;color:var(--muted);margin:-4px 0 4px;">💡 提示词中用【角色名】【场景名】【道具名】括起来的资产，生成时会自动引用其官方设定图（含服装名，如【木兰·铠甲】）</div>
    <label class="field-label">生图提示词</label>
    ${paramGridHtml("bd-imgp", SHOT_PARAMS)}
    ${promptBoxHtml("bd-imgp", b.image_prompt, 6, bid, "img")}
    <div style="display:flex;align-items:center;gap:8px;margin:6px 0 2px;">
      <button class="btn btn-au btn-sm" onclick="analyzeAU('board')">😊 AU 表情分析</button>
      <button class="btn btn-ghost btn-sm" onclick="checkConflicts('${b.id}')">🧭 冲突检测</button>
    </div>
    <div id="bd-au"></div>
    <div id="bd-conflict"></div>
    <label class="field-label">本镜头参考图（直接作为生图参考，不作为底图；构图/氛围/运镜参考，生图时自动带上）</label>
    <div class="sheet-preview-row" id="board-ref-row">
      ${(b.ref_images || []).map((u, j) => `<div style="position:relative"><img src="${u}" onclick="showLightbox('${u}')">
        <button class="img-del" style="width:20px;height:20px;font-size:10px;" onclick="delBoardRef('${b.id}',${j})">✕</button></div>`).join("")}
      <button class="ref-add-btn" onclick="addBoardRef('${b.id}')">＋<span>上传</span></button>
    </div>
    <label class="field-label">视频提示词</label>
    ${promptBoxHtml("bd-vidp", b.video_prompt, 4, bid, "vid")}
    ${paramGridHtml("bd-vidp", MOVE_PARAMS)}
    <label class="field-label">AI 重写指令（可选）</label>
    <input id="bd-feedback" placeholder="如：镜头缓缓推近，情绪更压抑">
    <div style="display:flex;gap:8px;margin-top:16px;">
      <button class="btn btn-ghost" style="flex:1;" onclick="aiRewriteBoard('${b.id}')">✨ AI 重写</button>
      <button class="btn btn-primary" style="flex:1;" onclick="saveBoard('${b.id}')">保存</button>
    </div>`);
  syncParamGrid("bd-imgp", SHOT_PARAMS, SHEET_PARAMS.img);
  syncParamGrid("bd-vidp", MOVE_PARAMS, SHEET_PARAMS.video);
}
/** 🧭 一键检测冲突：输入框提示词（P2 手改 / P3 AI 自动生成）vs 调度图/3D 导演台坐标事实（P1，第一原则）。
    优先级：P1 调度图/3D导演台 > P2 输入框手改 > P3 AI 自动生成（必要时可舍弃）。 */
async function checkConflicts(bid) {
  const b = board(bid);
  const box = $("bd-conflict");
  if (!b || !box) return;
  const plot = b.trajViewPlot || b.topViewPlot;
  const legendHtml = `<div class="conflict-legend">⚖️ 优先级：<b>P1 调度图 / 3D 导演台</b>（第一原则） ＞ <b>P2 输入框手改</b> ＞ <b>P3 AI 自动生成</b>（可舍弃）</div>`;
  if (!plot || !(plot.camera && plot.camera.x != null)) {
    box.innerHTML = legendHtml + `<div class="conflict-item sev-info">⚪ 本镜还没有调度图 / 3D 导演台坐标数据（机位/人物未摆放），没有可比对的 P1 基准；先到调度图或 3D 导演台摆放人物与机位。</div>`;
    return;
  }
  const imgP = ($("bd-imgp")?.value ?? b.image_prompt) || "";
  const vidP = ($("bd-vidp")?.value ?? b.video_prompt) || "";
  showBusy("正在按调度图坐标检测冲突…", "P1 调度图/3D导演台 ＞ P2 手改 ＞ P3 AI 自动生成");
  let r;
  try {
    r = await api("/api/check-prompt-conflicts", { prompt: imgP, video_prompt: vidP, plot_data: plot });
  } catch (e) { hideBusy(); toast("检测失败：" + (e.message || e), true); return; }
  hideBusy();
  if (r.checked === false) {
    box.innerHTML = legendHtml + `<div class="conflict-item sev-info">⚪ ${esc(r.note || "本镜没有可比对的调度图数据。")}</div>`;
    return;
  }
  const list = r.conflicts || [];
  if (!list.length) {
    box.innerHTML = legendHtml + `<div class="conflict-ok">✅ 未发现冲突：生图/视频提示词与调度图坐标事实（入画名单、朝向、画面左右、陈设取舍）一致。</div>`;
    return;
  }
  const sevTag = { high: "🔴 必须处理", medium: "🟡 建议核对", info: "⚪ 仅提示" };
  const items = list.map((c) => `
    <div class="conflict-item sev-${c.severity}">
      <div class="ci-head"><b>${sevTag[c.severity] || "⚠️"} ${esc(c.type)}</b><span class="ci-scope">${esc(c.scope)}</span></div>
      <div class="ci-row"><span class="ci-k">冲突句</span>${esc(c.found)}</div>
      <div class="ci-row"><span class="ci-k">P1 事实</span>${esc(c.fact)}</div>
      <div class="ci-row"><span class="ci-k">建议</span>${esc(c.advice)}</div>
    </div>`).join("");
  const s = r.summary || {};
  box.innerHTML = legendHtml +
    `<div class="conflict-count">共发现 <b>${list.length}</b> 处冲突：🔴 ${s.high || 0} · 🟡 ${s.medium || 0} · ⚪ ${s.info || 0}${items}</div>`;
}
function saveBoard(bid) {
  const b = board(bid);
  if (!b) return;
  b.description = $("bd-desc").value.trim();
  b.dialogue = $("bd-dialogue").value.trim();
  b.duration = Math.min(30, Math.max(2, parseInt($("bd-dur").value) || 3));
  b.linkPrev = !!($("bd-linkprev") || {}).checked;
  // 参数字典单独存一份，同时保证正文里带着它们（生图/生视频都读正文）
  b.shot_params = { img: { ...SHEET_PARAMS.img }, video: { ...SHEET_PARAMS.video } };
  b.image_prompt = applyParams($("bd-imgp").value.trim(), SHEET_PARAMS.img);
  b.video_prompt = applyParams($("bd-vidp").value.trim(), SHEET_PARAMS.video);
  touch(); closeSheet(); renderBoards(); toast("已保存");
}
/** 提示词输入实时同步进分镜数据（不关弹窗、不打断输入）：生成时无论有没有点保存都带最新内容 */
function livePromptSync(bid, kind, v) {
  const b = board(bid);
  if (!b) return;
  const params = kind === "img" ? SHEET_PARAMS.img : SHEET_PARAMS.video;
  if (kind === "img") {
    b.image_prompt = applyParams(v, params);
    if ((b.image_prompt || "").trim() !== String(b.image_prompt_ai || "").trim()) b.image_prompt_edited = true;  // 与 AI 原文不同＝手改（P2）
  }
  else b.video_prompt = applyParams(v, params);
  b.shot_params = { img: { ...SHEET_PARAMS.img }, video: { ...SHEET_PARAMS.video } };
  touch(); // 1.2s 防抖自动落盘
}
/** 提示词框旁的 💾 保存：存草稿但不关弹窗（与底部「保存」同一套逻辑，只少关窗和重渲染） */
function saveBoardDraft(bid) {
  const b = board(bid);
  if (!b) return;
  b.description = $("bd-desc")?.value.trim() ?? b.description;
  b.dialogue = $("bd-dialogue")?.value.trim() ?? b.dialogue;
  b.duration = Math.min(30, Math.max(2, parseInt($("bd-dur")?.value) || b.duration || 3));
  b.linkPrev = !!($("bd-linkprev") || {}).checked;
  b.shot_params = { img: { ...SHEET_PARAMS.img }, video: { ...SHEET_PARAMS.video } };
  b.image_prompt = applyParams($("bd-imgp").value.trim(), SHEET_PARAMS.img);
  if ((b.image_prompt || "").trim() !== String(b.image_prompt_ai || "").trim()) b.image_prompt_edited = true;
  b.video_prompt = applyParams($("bd-vidp").value.trim(), SHEET_PARAMS.video);
  touch(); saveNow(); toast("已保存（弹窗保持打开）");
}
async function aiRewriteBoard(bid) {
  const b = board(bid);
  const st = S.cur.data.story;
  if (!b) return;
  // 修改框当前内容优先：用户改完没点「保存」直接点 AI 重写，也必须按框里最新内容重写
  const desc = (($("bd-desc")?.value ?? b.description) || "").trim();
  const dialogue = (($("bd-dialogue")?.value ?? b.dialogue) || "").trim();
  const duration = Math.min(30, Math.max(2, parseInt($("bd-dur")?.value) || b.duration || 3));
  const oldPrompt = (($("bd-imgp")?.value ?? b.image_prompt) || "").trim();
  const oldVidPrompt = (($("bd-vidp")?.value ?? b.video_prompt) || "").trim();
  // 下拉选中的参数即使正文被清空也要生效：发给 AI 前先把它们并进旧提示词
  const sendPrompt = applyParams(oldPrompt, SHEET_PARAMS.img);
  const sendVidPrompt = applyParams(oldVidPrompt, SHEET_PARAMS.video);
  showBusy("AI 正在重写提示词…", "分镜提示词 + 视频提示词一起更新");
  try {
    const r = await api("/api/regenerate-prompt", {
      type: "storyboard",
      name: "镜头 " + b.shot_number,
      description: desc, dialogue: dialogue, duration: duration,
      style_bible: (st && st.style_bible) || "",
      synopsis: (st && st.synopsis) || "",
      ...assetPayload(),
      old_prompt: sendPrompt || "", old_video_prompt: sendVidPrompt || "", feedback: ($("bd-feedback").value || "").trim() || undefined,
    });
    b.description = desc; b.dialogue = dialogue; b.duration = duration;  // 框内改动一并写回
    b.shot_params = { img: { ...SHEET_PARAMS.img }, video: { ...SHEET_PARAMS.video } };
    b.image_prompt = applyParams(keepParams(r.image_prompt || oldPrompt || b.image_prompt, oldPrompt), SHEET_PARAMS.img);
    b.image_prompt_ai = b.image_prompt; b.image_prompt_edited = false;   // AI 重写＝回到 AI 原文基线
    if (r.video_prompt) b.video_prompt = applyParams(keepParams(r.video_prompt, oldVidPrompt), SHEET_PARAMS.video);
    hideBusy(); touch(); openBoardSheet(bid);
    toast("提示词已重写");
  } catch (e) { hideBusy(); toast(e.message, true); }
}
/* ---------- AU 表情分析：从提示词提取表情/动作关键词 → FACS AU 编码（词库优先，AI 兜底） ---------- */
async function analyzeAU(target) {
  const isBoard = target === "board";
  const ta = $(isBoard ? "bd-imgp" : "as-prompt");
  const out = $(isBoard ? "bd-au" : "as-au");
  if (!ta || !out) return;
  const prompt0 = (ta.value || "").trim();
  if (!prompt0) return toast("提示词为空，无法分析", true);
  // 画风圣经会逐字复用在每条提示词里，但它只描述画风、不含表情；
  // 先整段剔除再分析，避免里面的叙事基调词（如"自嘲式幽默"）让每条都误命中同一表情
  let prompt = prompt0;
  const styleBible = (S.cur.data.story || {}).style_bible;
  if (styleBible && styleBible.length > 10) prompt = prompt.split(styleBible).join(" ").trim();
  if (!prompt) prompt = prompt0;
  out.innerHTML = `<p class="lib-empty" style="padding:8px;text-align:left;">😊 正在分析表情/动作 AU…</p>`;
  try {
    const data = await api("/api/extract-au", { prompt });
    if (!data.matches || !data.matches.length) {
      out.innerHTML = `<p class="lib-empty" style="padding:8px;text-align:left;color:var(--muted);">😊 未捕捉到表情/动作关键词</p>`;
      return;
    }
    const srcTag = data.source === "ai" ? " · AI 分析"
      : data.source === "hybrid" ? " · 词库+AI 补齐" : " · 词库";
    out.innerHTML =
      `<p style="font-size:13px;color:var(--ink);margin:2px 0 6px;">😊 AU 分析 <span style="color:var(--muted);">(${esc(data.au_string || "—")}${srcTag})</span></p>` +
      data.matches.map((m) =>
        `<span style="display:inline-block;margin:0 6px 4px 0;padding:2px 8px;background:#fef3c7;border:1px solid #fde68a;border-radius:9999px;font-size:12px;">` +
        `<b>${esc(m.au)}</b> ${esc(m.label || "")}${m.keywords && m.keywords.length ? `（命中：${esc(m.keywords.join("、"))}）` : ""}</span>`
      ).join("") +
      (data.tips && data.tips.length ? `<p style="font-size:12px;color:#92400e;margin-top:2px;">💡 ${esc(data.tips[0])}</p>` : "");
  } catch (e) {
    out.innerHTML = `<p style="font-size:12px;color:var(--danger);margin:2px 0 6px;">😊 AU 分析失败：${esc(e.message)}</p>`;
  }
}
/* 解析分镜文本里的中括号引用：【名字】或 [名字] → 资产名集合。
   生图提示词的段落标签（【画面内容】【美术风格】等）也用【】，属于固定格式而非引用，
   放在 IGNORE_TAGS 里排除；括号内允许用 ·、/、空格等分隔多个名字或带修饰（如【木兰·铠甲】） */
const BRACKET_IGNORE_TAGS = new Set([
  "画面内容", "画面", "景别", "运镜", "镜头", "机位", "台词", "对白", "旁白",
  "美术风格", "光源", "影调", "氛围", "时长", "转场", "音效", "音乐",
]);
/* 镜头参数/画面约束类括号：给模型的技术指令，不是资产名。【景别：中景】【镜头运动：缓慢推近】
   【禁止出现现代元素】【人物不得看向镜头】这类以前会被当资产名去查、找不到就报"未匹配"，
   现在整段跳过（与后端 _is_param_bracket 同一套语义）。只放"不可能是资产名"的词。 */
const BRACKET_PARAM_LABELS = new Set([
  "画面内容", "画面", "画面描述", "画面构成", "景别", "取景", "运镜", "镜头", "镜头运动",
  "镜头语言", "镜头细节", "镜头角度", "机位", "机位位置", "视角", "摄像机", "摄影机",
  "构图", "画幅", "画质", "比例", "分辨率", "尺寸",
  "台词", "对白", "旁白", "字幕", "音效", "音乐", "声音", "时长", "转场", "节奏",
  "美术风格", "风格", "画风", "画风参考", "光源", "光线", "光影", "影调", "色调",
  "色彩", "氛围", "情绪", "表情", "动作", "动作时序", "人物数量", "人数",
  "约束", "限制", "要求", "注意", "注意事项", "备注", "提示", "禁止", "避免", "参考",
]);
const BRACKET_RULE_WORDS = ["禁止", "严禁", "不得", "不能", "不可", "不要", "避免", "切勿",
  "必须", "务必", "需要", "应该", "确保", "保持", "尽量", "只有", "仅有", "只能", "只需",
  "只要", "不出现"];
function isParamBracket(text) {
  const s = String(text || "").trim();
  if (!s) return true;
  if (BRACKET_IGNORE_TAGS.has(s) || BRACKET_PARAM_LABELS.has(s)) return true;
  if (s.startsWith("待补")) return true;
  if (s.includes("：") || s.includes(":")) return true;   // 资产名不会带冒号
  if (BRACKET_RULE_WORDS.some((w) => s.includes(w))) return true;
  const SEP = " ：:·•|/-、,，.。;；";
  for (const lbl of BRACKET_PARAM_LABELS) {
    if (lbl.length < 2 || s === lbl) continue;
    if (s.startsWith(lbl) && s.length > lbl.length && SEP.includes(s[lbl.length])) return true;
    if (s.endsWith(lbl) && s.length <= lbl.length + 6) return true;
  }
  return false;
}
/* 宽松判定：只在"已确认匹配不到任何资产"之后用（如未匹配提示）——带镜头参数名的碎块
   静默忽略，不误报。此时资产已匹配失败，放宽不会误伤真实资产。 */
const DESC_SUFFIX = ["几何", "描写", "说明", "细节", "要求", "时序", "变化", "过程", "关系", "逻辑", "规范"];
// 【花木兰·落座几何，严格按此描写】：`·` 后面是这一段的"小标题/写法要求"，不是资产名，
// AI 重写常这么写，拿去资产库查就会误报"未匹配"
function looksDescriptive(text) {
  const s = String(text || "").trim();
  if (!s) return true;
  return DESC_SUFFIX.some((suf) => s.endsWith(suf));
}
function looksLikeShotParam(text) {
  const s = String(text || "").trim();
  if (!s) return true;
  if (isParamBracket(s) || looksDescriptive(s)) return true;
  for (const lbl of BRACKET_PARAM_LABELS) if (s.includes(lbl)) return true;
  return false;
}
/* 提示词高亮渲染：把【景别：中景】【镜头细节】这类镜头参数/段落标签加粗（加黑），
   资产引用（【花木兰·民女打扮】【堂屋】）做成彩色胶囊——两种中括号一眼区分，不会看混。
   判定复用 isParamBracket（与"要不要去资产库查"同一套语义），保证显示和生成行为一致。 */
function promptHighlightHtml(text) {
  const s = text || "";
  const re = /[【\[]([^\]】]{1,60})[\]】]/g;
  let out = "", i = 0, m;
  while ((m = re.exec(s))) {
    out += esc(s.slice(i, m.index));
    const inner = m[1];
    out += isParamBracket(inner)
      ? `<b class="p-tag">${esc(m[0])}</b>`
      : `<span class="p-ref">${esc(m[0])}</span>`;
    i = m.index + m[0].length;
  }
  out += esc(s.slice(i));
  return out;
}
/** 提示词框：默认显示加粗高亮版，点「✎ 编辑」切回原文编辑（textarea 始终是唯一数据源） */
function promptBoxHtml(taId, text, rows, bid, kind) {
  // 修回「打开即编辑」：textarea 直接可见可改（此前改成高亮预览+点「✎ 编辑」才能改，用户不习惯）。
  // 输入实时同步进分镜数据（touch 自动落盘）——生成图片/视频无论有没有点保存，都带最新内容。
  // 「👁 高亮预览」变成可选功能；另加 💾 保存 按钮（存草稿不关弹窗）。
  const sync = bid ? ` oninput="livePromptSync('${bid}','${kind}',this.value)"` : "";
  const saveBtn = bid ? `<button class="pbox-edit" onclick="saveBoardDraft('${bid}')">💾 保存</button>` : "";
  return `<div class="pbox" id="pbox-${taId}">
    <div class="pbox-view hidden">${promptHighlightHtml(text) || `<span class="p-empty">（还没有提示词，直接在下方输入框写一条）</span>`}</div>
    <textarea id="${taId}" rows="${rows}"${sync}>${esc(text || "")}</textarea>
    <div class="pbox-hint">
      <span>加粗＝镜头参数 · 彩色＝人物场景引用</span>
      ${saveBtn}
      <button class="pbox-edit" onclick="togglePromptEdit('${taId}')">👁 高亮预览</button>
    </div>
  </div>`;
}
function refreshPromptView(taId) {
  const box = $("pbox-" + taId), ta = $(taId);
  if (!box || !ta) return;
  const view = box.querySelector(".pbox-view");
  if (view) view.innerHTML = promptHighlightHtml(ta.value) || `<span class="p-empty">（还没有提示词，直接在输入框写一条）</span>`;
}
function togglePromptEdit(taId) {
  const box = $("pbox-" + taId), ta = $(taId);
  if (!box || !ta) return;
  const view = box.querySelector(".pbox-view");
  const btn = box.querySelector(".pbox-edit");
  const editing = ta.classList.contains("hidden");
  if (editing) {
    view.classList.add("hidden"); ta.classList.remove("hidden");
    btn.textContent = "👁 高亮预览";
    ta.focus();
  } else {
    refreshPromptView(taId);
    ta.classList.add("hidden"); view.classList.remove("hidden");
    btn.textContent = "✎ 编辑";
  }
}
/* 某个分镜的入画角色名单（与生图取参考图同一套规则：正文点名 + 中括号引用）。
   用于「接上一镜」时判断：上一镜末帧里的人本镜还写不写——写了才敢拿末帧当衔接参考，
   没写就不送，否则模型会把上一镜的人（比如躺在床上养病的父亲）原样搬进本镜画面。 */
function castOfBoard(b) {
  const st = (S.cur && S.cur.data && S.cur.data.story) || {};
  const text = [b.description, b.dialogue, b.image_prompt, b.video_prompt].filter(Boolean).join(" ");
  const out = new Set();
  for (const c of st.characters || []) {
    if (c && c.name && c.name.length >= 2 && text.includes(c.name)) out.add(c.name);
  }
  for (const parts of parseBracketGroups(b)) {
    for (const c of st.characters || []) {
      if (!c || !c.name) continue;
      const hit = parts.some((p) => p === c.name
        || (p.length >= 2 && c.name.includes(p)) || (c.name.length >= 2 && p.includes(c.name)));
      if (hit) out.add(c.name);
    }
  }
  return out;
}
/* 该角色在本镜里是不是只是背景/虚化陪衬（门帘后、景深外、躺着、远处隐约）。
   是的话不能把它的"全身站立设定图"当参考——模型会照抄立绘里的站姿和手持道具，
   于是提示词里"卧床的父亲"旁边又站出一个父亲（实测：站着的父亲＋躺着的父亲同框）。
   判定：正文里每次提到该角色都处在"虚化/门帘后/景深外/轮廓"这类语境里才算背景角色；
   只要有一句是正常出场（有动作、在画面主体位置），就当正常角色处理。 */
function isBackgroundRole(b, name) {
  if (!name || name.length < 2) return false;
  // 先剥掉所有中括号（【人物·花木兰】这类标签里带着名字，但正文常用"她"指代，
  // 不剥会把主角的台词句误判成背景句）；剥完后名字不再出现在正文里 → 判不了，保守当前景
  const raw = [b.description, b.image_prompt, b.video_prompt].filter(Boolean).join("；");
  const text = raw.replace(/[【\[]([^\]】]{1,60})[\]】]/g, " ");
  if (!text.includes(name)) return false;
  const sents = text.split(/[。；;\n]/).filter((s) => s.includes(name));
  const BG_WORDS = ["虚化", "虚焦", "景深外", "景深之外", "背景", "远处",
    "隐约", "模糊", "看不清", "不清晰", "轮廓", "余光", "画外"];
  return sents.length > 0 && sents.every((s) => BG_WORDS.some((w) => s.includes(w)));
}
function parseBracketGroups(b, onlyVid) {
  // 返回"括号分组"数组的数组：每个中括号的内容拆成若干 part（过滤段落标签/镜头约束），
  // 保留【花木兰·民女打扮】这类"角色·服装"组合结构——组内其余 part 优先匹配
  // 该角色的服装变体，而不是只查全局名单（实测教训：拆散后"民女打扮"查全局
  // 匹配不到任何资产，被误报"未匹配"）
  // onlyVid=true：只扫视频提示词（生视频用）。中括号【】是显式引用（含服装变体）；
  // 纯文本写名字的角色由 resolveAssetRefs 末尾的兜底补入主图。
  const text = onlyVid ? (b.video_prompt || "")
    : [b.description, b.dialogue, b.image_prompt, b.video_prompt].filter(Boolean).join("\n");
  const groups = [], seen = new Set();
  const re = /[【\[]([^\]】]{1,60})[\]】]/g;
  let m;
  while ((m = re.exec(text))) {
    if (isParamBracket(m[1])) continue;   // 【景别：中景】这类镜头约束整段跳过，不查资产
    const parts = m[1].split(/[·•|/、,，;；\s]+/).map((t) => t.trim())
      .filter((t) => t && !BRACKET_IGNORE_TAGS.has(t) && !isParamBracket(t));
    const key = parts.join("|");
    if (parts.length && !seen.has(key)) { seen.add(key); groups.push(parts); }
  }
  return groups;
}
/* 资产库快照：重写提示词时一起发给后端，让【花木兰·民女打扮】这类引用
   能解析到"该角色那一套服装"的真实设定，而不是回落到角色默认造型 */
function assetPayload() {
  const st = S.cur?.data?.story || {};
  const norm = (x) => ({ name: x.name, image_prompt: x.image_prompt || "", description: x.description || "" });
  return {
    characters: (st.characters || []).filter((c) => c && c.name).map((c) => ({
      name: c.name,
      image_prompt: c.image_prompt || "",
      outfits: (c.outfits || []).filter((o) => o && o.name).map(norm),
    })),
    // 场景要带 furniture：后端引用解析时把家具布置（大型家具/门窗位置朝向）写进场景锚点，
    // AI 重写分镜提示词时才不会把门窗和大件家具挪位
    scenes: (st.scenes || []).filter((s) => s && s.name).map((s) => ({
      name: s.name, image_prompt: s.image_prompt || "", description: s.description || "",
      furniture: s.furniture || undefined,
    })),
    props: (st.props || []).filter((p) => p && p.name).map(norm),
  };
}
/* 解析提示词里的中括号资产引用 → 设定图参考列表 [{url, meta}]。
   生视频用（onlyVid=true 时只扫 video_prompt）：用户在视频提示词里写
   【花木兰·民女打扮】【堂屋】【铜镜】，对应角色（含指定服装）/场景/道具
   设定图跟随入池——对齐生图的引用机制。 */
/* 视频专用的背景/虚化角色判定：只看视频提示词、按分句判定。
   与生图的 isBackgroundRole 区别：①不扫 image_prompt（那里的"家具与父亲位置参考所附图"
   这类约束句没有背景词，会打破"全部句子带背景词"的判定）；②分句更细（也按逗号拆，
   视频提示词是"0-2s，木兰捧起纲帛；2-4s，她抬眼望向门帘后父亲卧床的虚化轮廓"这种长短句）；
   ③背景词增加视频里常见的卧床/门帘后/睡着这类提示。 */
function isVideoBgRole(b, name) {
  if (!name || name.length < 2) return false;
  const text = (b.video_prompt || "").replace(/[【\[]([^\]】]{1,60})[\]】]/g, " ");
  if (!text.includes(name)) return false;
  const clauses = text.split(/[。；;，,、\n]/).filter((s) => s.includes(name));
  const BG_WORDS = ["虚化", "虚焦", "景深外", "景深之外", "背景", "远处",
    "隐约", "模糊", "看不清", "不清晰", "轮廓", "余光", "画外",
    "门帘后", "卧床", "睡在", "正睡", "睡着", "躺着", "床上"];
  return clauses.length > 0 && clauses.every((s) => BG_WORDS.some((w) => s.includes(w)));
}
function resolveAssetRefs(b, onlyVid) {
  const st = S.cur?.data?.story || {};
  const groups = parseBracketGroups(b, onlyVid);
  const out = [], seen = new Set();
  const nameHit = (p, name) => !!p && !!name &&
    (p === name || (p.length >= 2 && name.includes(p)) || (name.length >= 2 && p.includes(name)));
  for (const parts of groups) {
    for (const c of st.characters || []) {
      if (!c || !c.name || !parts.some((p) => nameHit(p, c.name))) continue;
      if (c.image_url && !seen.has(c.image_url)) {
        seen.add(c.image_url);
        out.push({ url: c.image_url, meta: { kind: "character", name: c.name } });
      }
      for (const o of c.outfits || []) {
        if (o && o.image_url && o.name && parts.some((p) => nameHit(p, o.name)) && !seen.has(o.image_url)) {
          seen.add(o.image_url);
          out.push({ url: o.image_url, meta: { kind: "character", name: `${c.name}·${o.name}` } });
        }
      }
    }
    for (const sc of st.scenes || []) {
      if (sc && sc.image_url && parts.some((p) => nameHit(p, sc.name)) && !seen.has(sc.image_url)) {
        seen.add(sc.image_url);
        out.push({ url: sc.image_url, meta: { kind: "scene", name: sc.name } });
      }
    }
    for (const p2 of st.props || []) {
      if (p2 && p2.image_url && parts.some((q) => nameHit(q, p2.name)) && !seen.has(p2.image_url)) {
        seen.add(p2.image_url);
        out.push({ url: p2.image_url, meta: { kind: "prop", name: p2.name } });
      }
    }
  }
  // 纯文本点名兜底：视频提示词里不用中括号、直接写「木兰」「花木兰」「父亲」时，
  // 角色本体设定图也要入池（实测：AI 重写的输出经常不带括号，用户写花木兰没带服装，
  // 期望的就是角色主图——只有中括号才认的规则把主图全部拦在了池外）。
  // 只补角色本体图：服装仍需【角色·服装】显式引用（纯文本猜服装容易猜错）；
  // 场景/道具不带（纯文本提及太泛，且分镜图已含环境，带场景图反而会诱发换景）。
  // 简称按角色名末两字识别（花木兰→木兰）；含禁止/不要等约束词的分句不算点名
  //（"不要出现父亲"不能当成父亲入画）；背景/虚化角色（isVideoBgRole）仍排除——
  // 带上主图会把「门帘后卧床虚化的父亲」画成站着的父亲（两个父亲事故的复发源）。
  const vidText = b.video_prompt || "";
  for (const c of st.characters || []) {
    if (!c || !c.name || !c.image_url || seen.has(c.image_url)) continue;
    const cands = [c.name, ...(c.name.length >= 3 ? [c.name.slice(-2)] : [])];   // 花木兰 → 木兰
    const clauses = vidText.split(/[。；;，,、\n]/);
    const named = cands.some((nm) => clauses.some((s) => s.includes(nm) && !BRACKET_RULE_WORDS.some((w) => s.includes(w))));
    if (!named || isVideoBgRole(b, c.name)) continue;
    seen.add(c.image_url);
    out.push({ url: c.image_url, meta: { kind: "character", name: c.name } });
  }
  // 场景纯文本点名兜底（用户要求：生图/生视频提示词提到场景就要传场景设定图，与生图流程 sceneHit 对齐）：
  // 场景全名或末两字命中视频提示词即入池；同一分句含禁止/不要等约束词不算点名
  //（"不要出现战场"不能当成战场入池）。分镜图本身已含环境，meta 写明场景图"只锁环境陈设基准"，
  // 避免模型因参考图诱发换景或把设定图里的无人物画进画面
  for (const sc of st.scenes || []) {
    if (!sc || !sc.name || !sc.image_url || seen.has(sc.image_url)) continue;
    const nm = String(sc.name);
    if (nm.length < 2) continue;
    const cands = [nm, ...(nm.length >= 3 ? [nm.slice(-2)] : [])];   // 木兰家堂屋 → 堂屋
    const clauses = vidText.split(/[。；;，,、\n]/);
    const named = cands.some((cn) => clauses.some((s) => s.includes(cn) && !BRACKET_RULE_WORDS.some((w) => s.includes(w))));
    if (!named) continue;
    seen.add(sc.image_url);
    out.push({ url: sc.image_url, meta: { kind: "scene", name: sc.name,
      description: `视频提示词提到的场景「${sc.name}」设定图：外观结构参考（陈设样式/材质/相对方位），不提供朝向角度与拍摄视角，空间几何按本镜机位与首帧构图执行${sc.image_prompt ? "。" + sc.image_prompt : ""}` } });
  }
  return out;
}
async function genBoardImage(bid) {
  const b = board(bid);
  if (!b || b.imageBusy) return;
  b.imageBusy = true;
  renderBoards();
  const landscape = S.cur.data.orientation === "landscape";
  const urls = [], meta = [];
  // 调度图是导演定稿，永远排最前（后端对 kind=diagram 强制保留不筛选）
  const diagUrl = b.trajView || b.topView;
  const diagPlot = b.trajViewPlot || b.topViewPlot;
  // 纵深调度解读（对齐桌面版）：把导演口述解读成"谁近谁远/谁在哪侧/会不会出框"的说明，
  // 单靠俯视图纸，生图模型换算不出远近层次。同一条调度口述只解读一次，缓存在分镜上。
  let diagBrief = "";
  if (diagUrl && diagPlot) {
    const cacheKey = JSON.stringify(diagPlot).slice(-160) + "|" + (b.topViewDesc || "") + "|" + (b.trajViewDesc || "");
    if (b._diagBriefKey === cacheKey && b._diagBriefText) diagBrief = b._diagBriefText;
    else {
      try {
        const r0 = await api("/api/staging-brief", {
          description: (b.image_prompt || b.description || "").slice(0, 400),
          topview_desc: (b.topViewDesc || "").trim(),
          trajectory_desc: (b.trajViewDesc || "").trim(),
          characters: shotCast(b),
          cam_summary: (b.topViewCamSummary || b.trajViewCamSummary || "").trim() || null,
        });
        if (r0 && r0.staging_brief) {
          diagBrief = r0.staging_brief;
          b._diagBriefKey = cacheKey; b._diagBriefText = diagBrief;  // 只做内存缓存，不落库
        }
      } catch (_) { /* 解读失败不影响生图 */ }
    }
  }
  const diagNote = [String(b.trajViewDesc || "").trim(), String(b.topViewDesc || "").trim()]
    .filter(Boolean).join("；");
  if (diagUrl) {
    urls.push(diagUrl);
    // description 放导演的调度口述（后端把它当"口述说明"注入）；
    // 精确模式的机位几何翻译由后端 build_plot_prompt_text（2D 版逻辑）自动完成，前端不再重复注入
    meta.push({ kind: "diagram", name: "调度图", description: (diagNote || "本图是导演定稿的俯视调度图纸：人偶棋子＝人物（标签为人名），「机位」符号＋扇形视锥＝机位与拍摄朝向，虚线箭头＝运动路线；人物位置/走位/机位/朝向以本图为准。") + (stagingModeOf(b) === "loose" ? "\n【参考模式】人物站位与门窗家具位置大致参考本图纸即可，允许根据构图需要小幅移动或微调朝向。" : "") });
  }
  // 🎬 机位预览帧：3D 导演台成片构图的实时渲染（用户要求：最后出图以预览窗所见为准）。
  // 排在调度图之后——调度图管站位/机位权威，这张管成片构图基准（画面左右/远近/朝向/遮挡）
  if (b.previewShot && b.previewShot.startsWith("data:image")) {
    urls.push(b.previewShot);
    meta.push({ kind: "shot_preview", name: "机位预览", description:
      "导演 3D 导演台的机位预览＝从真实机位渲染的本镜画面，空间几何铁律的权威依据：人物站位/画面左右/远近大小/朝向/遮挡/机位视角以此图为准。"
      + "图中彩色胶囊、方块、锥体只是站位示意，严禁画进画面；人物长相服装按角色设定图，家具门窗样式按场景设定图，画风光线按画风设定。" });
  }
  // 本镜头手动上传的参考图（构图/氛围参考），紧跟调度图之后，优先于角色/场景设定图
  for (const u of b.ref_images || []) {
    urls.push(u);
    meta.push({ kind: "ref", name: "镜头参考", description: "导演为本镜上传的参考图：机位、人物在画面里的位置与朝向、姿势、家具陈设可参考本图。但图中出现的人物不自动入画——画面最终人数与各人状态必须与提示词一致，人物长相仍以角色设定图为准。" });
  }
  // 接上一镜：上一镜头最后一帧作为衔接锚点（连续镜头：场景/人物/光线无缝延续）
  if (b.linkPrev) {
    const boards = S.cur?.data.boards || [];
    const idx = boards.findIndex((x) => x.id === bid);
    const prev = idx > 0 ? boards[idx - 1] : null;
    const prevFrame = prev ? (prev.images || []).filter(Boolean).slice(-1)[0] : null;
    // 本镜已经有你上传的参考图时，不再叠上一镜末帧：
    // 末帧里的旧人物 + 参考图里的人物 + 提示词描述的人，三份来源叠一起，
    // 模型会把同一个人画成好几个（实测：站着的父亲＋躺着的父亲同框）。
    const hasManualRef = (b.ref_images || []).length > 0;
    if (hasManualRef) {
      toast("本镜已有你上传的参考图，已跳过上一镜末帧，避免人物重复", false);
    } else if (prevFrame) {
      // 上一镜末帧里有、但本镜提示词没写到的人物（典型：上一镜躺在床上养病的父亲），
      // 不能拿末帧当参考图——模型看到参考图里有人就会照着画，文字约束拗不过参考图。
      // 这种情况宁可牺牲"无缝衔接"，也不让无关角色跑进画面。
      const prevCast = prev ? castOfBoard(prev) : new Set();
      const curCast = castOfBoard(b);
      // 主角（角色库第一个）总是默认在画面里——提示词常用"她/他"指代，名单里就不会出现名字，
      // 不能因此判成"多余人物"。只有配角（父亲/路人等）才算真正需要拦截的。
      const mainName = ((S.cur?.data.story?.characters || [])[0] || {}).name;
      const extraCast = [...prevCast].filter((n) => n !== mainName && !curCast.has(n));
      if (extraCast.length) {
        toast(`上一镜末帧里有「${extraCast.join("、")}」，本镜没写到，已跳过末帧以免把 TA 画进画面`, false);
      } else {
        urls.push(prevFrame);
        meta.push({ kind: "prev_frame", name: "上一镜末帧", description: `上一镜头（镜头${prev.shot_number}）的最后一帧画面：本镜头（镜头${b.shot_number}）紧接其后，是连续镜头——场景布局、光线色调必须与该帧无缝衔接。人物只画本镜提示词里写到的人：该帧里出现、但本镜提示词没写到的人物一律不得照搬进本镜，也不得沿用该帧里那些人的长相与衣着` });
      }
    } else if (prev) {
      toast(`上一镜（镜头 ${prev.shot_number}）还没有分镜图，本次无法衔接末帧`, true);
    }
  }
  const st = S.cur.data.story || {};
  // 中括号引用：提示词里用【名字】/[名字] 括起来的角色/场景/道具/服装，其设定图优先入池。
  // 注意：生图提示词的段落标签（【画面内容】【美术风格】等）也用【】，已在忽略名单中排除
  const bracketGroups = parseBracketGroups(b);
  const addedImgs = new Set(urls);          // 调度图/手动参考图/末帧已占用的槽位
  const refNames = [], missNames = [];
  const pickedOutfits = new Map();   // 角色名 → 本镜被明确引用的服装名集合
  // ⚠️ 本镜到底有谁入画（决定谁能进参考图池）
  // 历史 bug：兜底把角色库**全量**设定图都发给模型。角色库里有一张"生病老人"设定图，
  // 提示词一个字没提父亲，成图却凭空多出躺床上的老人——生图模型看到参考图就会照着画，
  // 光靠文字写"不要出现别人"是拗不过参考图的（和场景空镜里被塞人同一个道理）。
  // 所以这里先算出入画名单：提示词/描述/台词里点到名字的角色 + 中括号显式引用的角色。
  const shotText = [b.description, b.dialogue, b.image_prompt, b.video_prompt]
    .filter(Boolean).join(" ");
  const castNames = new Set();
  for (const c of st.characters || []) {
    if (c && c.name && c.name.length >= 2 && shotText.includes(c.name)) castNames.add(c.name);
  }
  const castFromText = castNames.size > 0;   // 名单是否来自提示词（决定能否加"只出现这些人"的硬约束）
  if (!castNames.size && (st.characters || []).some((c) => c && c.name)) {
    // 提示词里一个人名都没写（如"她坐在织机前"）：默认只带第一个角色（主角），绝不带全库
    castNames.add((st.characters || []).find((c) => c && c.name).name);
  }
  if (bracketGroups.length) {
    const pushRef = (item, kind, label, extra, descOverride) => {
      if (!item || !item.image_url || addedImgs.has(item.image_url)) return false;
      addedImgs.add(item.image_url);
      urls.push(item.image_url);
      meta.push(descOverride
        ? { kind, name: item.name, description: descOverride }
        : { kind, name: item.name, description: `提示词中以中括号引用的${label}「${item.name}」官方设定图（画面必须与此图一致）${extra ? "：" + extra : ""}${item.image_prompt || item.description || ""}` });
      return true;
    };
    // 名称模糊匹配：相等 / 长词包含短词（双向），防"民女打扮"vs"民女装"这类细差漏判
    const nameHit = (p, name) => !!p && !!name &&
      (p === name || (p.length >= 2 && name.includes(p)) || (name.length >= 2 && p.includes(name)));
    for (const parts of bracketGroups) {
      const hitNames = new Set();
      // ① 角色本体：组内任一部分命中角色名 → 本体设定图入池
      for (const c of st.characters || []) {
        if (!c.name || !parts.some((p) => nameHit(p, c.name))) continue;
        castNames.add(c.name);   // 中括号点名 = 本镜入画
        if (pushRef(c, "character", "角色")) hitNames.add(c.name);
        // ② 组内其余部分 → 该角色的服装变体（【花木兰·民女打扮】→ 花木兰的"民女打扮"服装）
        for (const o of c.outfits || []) {
          if (!o.image_url || !o.name) continue;
          if (parts.some((p) => nameHit(p, o.name))) {
            const desc = `角色「${c.name}」本镜所穿服装「${o.name}」的官方设定图：人物身上的衣服款式、颜色、材质、发饰头面、鞋履一律以本图为准，禁止换成该角色的其它服装（如铠甲/战袍），也禁止两套服装混穿；面部/发型/体型仍与「${c.name}」本体设定图一致${o.image_prompt ? "。" + o.image_prompt : ""}`;
            if (pushRef(o, "character", "服装", "", desc)) {
              if (!pickedOutfits.has(c.name)) pickedOutfits.set(c.name, new Set());
              pickedOutfits.get(c.name).add(o.name);
            }
          }
        }
      }
      // ③ 场景 / 道具：组内任一部分命中；场景附家具布置锁定（门窗/大件家具位置不得漂移）
      for (const sc of st.scenes || [])
        if (parts.some((p) => nameHit(p, sc.name))) {
          const brief = furnitureBrief(sc.furniture);
          const desc = `提示词中以中括号引用的场景「${sc.name}」官方设定图（外观结构参考：陈设样式、材质、色调与相对方位；不提供朝向角度与拍摄视角，空间几何一律按铁律/导演台数据）${sc.image_prompt || sc.description || ""}${brief}`;
          if (pushRef(sc, "scene", "场景", "", desc)) hitNames.add(sc.name);
        }
      for (const p2 of st.props || [])
        if (parts.some((q) => nameHit(q, p2.name))) {
          if (pushRef(p2, "prop", "道具")) hitNames.add(p2.name);
        }
      // ④ 未命中警告：逐 part 判定，命中过角色/服装/场景/道具的 part 不算 miss；
      //    镜头参数/画面约束（【景别：中景】等）本就查不到资产，静默跳过不提示
      for (const p of parts) {
        if (looksLikeShotParam(p)) continue;
        const matched =
          (st.characters || []).some((c) => nameHit(p, c.name) || (c.outfits || []).some((o) => nameHit(p, o.name))) ||
          (st.scenes || []).some((sc) => nameHit(p, sc.name)) ||
          (st.props || []).some((pp) => nameHit(p, pp.name));
        if (!matched) missNames.push(p);
      }
      refNames.push(...hitNames);
    }
    if (refNames.length) toast(`已引用设定图：${[...new Set(refNames)].join("、")}`);
    if (missNames.length) toast(`⚠️ ${[...new Set(missNames)].join("、")} 未匹配到角色/服装/场景/道具（检查名字，或先在资产页/角色服装里创建）`, true);
  }
  // 兜底：未被括号引用排除的设定图照常入池，但**只限本镜入画角色**（见 castNames），
  // 库里其他角色（没在本镜出现的人）一张都不发——这是"无故多出爸爸/路人"的根源
  for (const c of st.characters || []) if (c.image_url) {
    if (!castNames.has(c.name)) continue;
    const pickedNow = pickedOutfits.get(c.name);
    // 只是背景/虚化陪衬的角色（如门帘后卧床的父亲）：不能让它按"站立立绘"入画，
    // 否则画面里会同时出现"躺着的父亲"和"照立绘画出来的站着的父亲"两个人。
    const bgOnly = isBackgroundRole(b, c.name);
    if (!addedImgs.has(c.image_url)) {
      addedImgs.add(c.image_url);
      urls.push(c.image_url);
      const hasOutfits = (c.outfits || []).some((o) => o && o.image_url);
      // appearance = 角色卡提示词原文，后端只从中抽"长相+服装"分句做锚点，
      // 丢掉立绘/站姿/纯色背景/画质这类"画设定图"专用要求（否则分镜会被画成证件照）
      const ap = c.image_prompt || c.description || "";
      meta.push({ kind: "character", name: c.name, appearance: ap, ...(bgOnly
        ? { description: `角色「${c.name}」在本镜只是背景/虚化陪衬：仅取面部特征（年纪、胡须、眉眼、发色）作为参考，图里的站姿、手持道具、正面构图一律不要照搬——本镜该角色按提示词描述处理（卧床/门帘后/景深之外/模糊），画面里绝不能因此多画一个站立的人。` }
        : pickedNow && pickedNow.size
          ? { description: `角色「${c.name}」本体设定图：只用于锁定面部、发型长短、瞳色、体型年龄，本镜服装以被引用的服装设定图为准，禁止照本图衣着作画。` }
          : hasOutfits
            ? { description: `角色「${c.name}」本体设定图：只锁面部、发型、瞳色、体型年龄（同一人物，长相必须与此图一致）；服装以提示词正文的衣着描写为准。` }
            : { description: `角色「${c.name}」官方设定图：长相、发型、体型与此图一致。` }) });
    }
    // v14 融脸修复：把用户上传的真人脸照片也带上（设定图经过 AI 转绘会丢 likeness，
    // 真人照片才是"本人"的最高基准；后端按 kind=face 注入脸锁定指令）
    for (const u of c.ref_images || []) {
      if (urls.length >= 9 || addedImgs.has(u)) continue;
      addedImgs.add(u);
      urls.push(u);
      meta.push({ kind: "face", name: c.name, description: `角色「${c.name}」的真人脸参考照片，画面中该角色的面部必须与照片高度一致` });
    }
    // 服装设定图：只有**本镜点名**的那套才入池（【角色·服装】引用，或正文里写了"民女打扮"）。
    // 以前是不点名就全送，结果织布镜头把"将军造型"铠甲图一起喂给模型，容易混穿/穿回默认那套；
    // 没点名时只发本体设定图锁脸，衣服按提示词文字描述画。
    const picked = pickedOutfits.get(c.name);
    for (const o of c.outfits || []) {
      if (!o.image_url || urls.length >= 9 || addedImgs.has(o.image_url)) continue;
      const named = !!(o.name && o.name.length >= 2 && shotText.includes(o.name));
      if (!(picked && picked.has(o.name)) && !named) continue;
      addedImgs.add(o.image_url);
      urls.push(o.image_url);
      meta.push({ kind: "character", outfit: true, name: c.name, description: `角色「${c.name}」的${o.name ? `「${o.name}」` : ""}服装设定图（同一人物换装）：本镜只穿本图这套服装，严禁换成该角色的其它服装、严禁两套混穿；面部/发型/体型与本体设定图一致：${o.image_prompt || ""}` });
    }
  }
  // 场景：只送本镜真正写到的那个场景。名字按全名或尾部 2 字匹配（"木兰家堂屋" ← 正文写"堂屋"），
  // 一个都没匹配上就不送——以前是 7 张场景图（含战场、朝堂）全塞给模型，环境不乱才怪
  const sceneHit = (sc) => {
    const nm = String(sc.name || "");
    if (nm.length < 2) return false;
    return shotText.includes(nm) || (nm.length >= 3 && shotText.includes(nm.slice(-2)));
  };
  const sceneMatched = (st.scenes || []).filter((sc) => sc && sc.image_url && sceneHit(sc));
  for (const sc of sceneMatched) if (!addedImgs.has(sc.image_url)) {
    urls.push(sc.image_url);
    meta.push({ kind: "scene", name: sc.name,
      description: "场景「" + sc.name + "」设定图（外观结构参考：陈设样式/材质/相对方位；不提供朝向角度与拍摄视角，空间几何按铁律执行）"
        + (sc.image_prompt || sc.description || "") + furnitureBrief(sc.furniture) });
  }
  // 入画人物硬约束：提示词里点了名，就把名单钉死在 prompt 里（参考图已按名单过滤，这里再加一道文字闸）。
  // ⚠️ 闸的是「正文没写的无名人影」，不是正文明确要求的群演——2026-11-14 实测教训：
  // 「宫人在起舞」写进了正文，却因旧版「除名单外不得出现任何其他人」被模型一并禁画。
  const castList = [...castNames];
  const castNote = castFromText && castList.length
    ? `【入画人物锁定】本镜头设定图角色名单：${castList.join("、")}（共 ${castList.length} 人）——名单角色的长相与服装以对应参考图为准，名单里的人一个都不能少。`
      + `提示词正文明确写到的人物必须按正文画出，数量与正文一致；`
      + `正文没写到的无名人影一律不画，也不得因参考图里有人就照搬进画面。\n\n`
    : "";
  const refs = { reference_urls: urls.slice(0, 9), reference_meta: meta.slice(0, 9) };
  const sendPrompt = castNote + (b.image_prompt || "");
  try {
    const r = await api("/api/generate-image", {
      prompt: sendPrompt, ...refs,
      size: landscape ? "1920x1080" : "1080x1920",
      context: b.image_prompt,          // 空镜/影子/参考图筛选都按这个判断
      // 精确模式才传 plot_data：后端按 2D 版机位几何（谁入画/画面左右/透视/门窗落位）注入权威约束；
      // 参考模式不传，后端跳过硬约束，图纸只作大致参考
      plot_data: (diagPlot && stagingModeOf(b) === "precise") ? diagPlot : undefined,
      diagram_brief: diagBrief || undefined,
      prompt_edited: b.image_prompt_edited === true,   // P2 手改提示词 > P3 AI 兜底（后端按此注入优先级声明）
    });
    b.images.push(r.image_url);
    // 出图后把本次真正送进模型的参考图名单亮出来（角色/场景/道具/调度图/末帧）。
    // 画面里冒出一个不该出现的人时，先看这行就能判断：是参考图池带了他，
    // 还是池子里根本没带、模型自己按文字画歪了——两种情况的修法完全不同。
    const refUsed = [...new Set(meta.map((m) => m && m.name).filter(Boolean))];
    if (r.warnings) toast(r.warnings, true);
    else if (refUsed.length) toast(`参考图 ${urls.length} 张：${refUsed.join("、")}`);
  } catch (e) { toast(e.message, true); }
  b.imageBusy = false;
  touch(); renderBoards();
}
// 从分镜的生图提示词里抽取环境段落（地点/美术风格/光源/影调/氛围）。
// 视频提示词通常只写动作与镜头运动、不含环境，参考图模式对人物主体一致性权重高、
// 对环境无约束，模型会自由脑补场景（实测：公园黄昏镜头被生成成室内走廊）。
// 抽出的文本经 env_context 交给后端，追加在视频提示词末尾做「场景环境锁定」。
function buildVideoEnvContext(b) {
  const p = b.image_prompt || "";
  if (!p) return "";
  const sec = (tag) => {
    const m = p.match(new RegExp("【" + tag + "】([\\s\\S]*?)(?=【|$)"));
    return m ? m[1].trim().replace(/[；;。，,]+$/, "") : ""; // 去掉段尾多余标点，避免拼接出现"；；"
  };
  const place = (p.match(/【画面内容】([^，。；]+)/) || [])[1] || ""; // 地点短语：首句到第一个标点
  const parts = [];
  if (place) parts.push("地点：" + place.trim());
  for (const tag of ["美术风格", "光源", "影调", "氛围"]) {
    const t = sec(tag);
    if (t) parts.push(tag + "：" + t);
  }
  // 场景家具布置锁定：本镜生图/视频提示词提到的场景若在资产里摆了家具布置
  //（大型家具/门窗位置朝向），一并写进环境锁定串，防止视频生成把门窗和大件家具挪位
  const envText = p + " " + (b.video_prompt || "");
  for (const sc of ((S.cur?.data?.story || {}).scenes || [])) {
    if (!sc || !sc.furniture || !sc.furniture.length) continue;
    const nm = String(sc.name || "");
    if (nm.length < 2) continue;
    const hit = envText.includes(nm) || (nm.length >= 3 && envText.includes(nm.slice(-2)));
    if (hit) parts.push(furnitureBrief(sc.furniture));
  }
  return parts.join("；");
}
/* ---------- 生成视频前的确认弹窗：运镜放在最上面，选完直接提交 ----------
   之前「🎬 视频」一点就提交，运镜藏在「提示词」抽屉最底部，等于没有入口。 */
function openVideoSheet(bid) {
  const b = board(bid);
  if (!b) return;
  if (!b.images.length) return toast("先生成分镜图", true);
  const sp = (b.shot_params && b.shot_params.video) || {};
  const cur = sp["运镜"] || readParam(b.video_prompt, "运镜");
  const hasPrompt = !!(b.video_prompt || "").trim();
  openSheet(`
    <div class="sheet-title">镜头 ${b.shot_number} · 生成视频</div>
    <div class="sheet-sub">选运镜会写进视频提示词；不选就按现有提示词生成</div>
    <label class="field-label">视频模型</label>
    <select id="vs-model" onchange="saveVideoWorkflow(this)">
      ${(function () { const src = document.getElementById("set-workflow"); return src ? src.innerHTML : ""; })()}
    </select>
    <p class="lib-empty" style="text-align:left;">默认走 H3 多图参考工作流；选「豆包 Seedance」则用即梦 ARK Key 生成（支持垫参考视频+自动配音轨）</p>
    <label class="field-label">分辨率（豆包 Seedance 生效 · 480p≈0.7元/秒，720p≈1.5元/秒，1080p≈1.7元/秒）</label>
    <select id="vs-res" onchange="saveDoubaoRes(this)">
      <option value="480p">480p · 最快最省钱</option>
      <option value="720p">720p · 均衡</option>
      <option value="1080p">1080p · 高清（10bit）</option>
    </select>
    <label class="field-label">帧模式（豆包 Seedance 生效 · 服装不一致就选首帧）</label>
    <select id="vs-frame" onchange="saveDoubaoFrame(this)">
      <option value="reference">参考图 · 镜头自由起幅，可配调度图/设定图</option>
      <option value="first">首帧 · 视频从分镜图开始动，服装一致性最高</option>
    </select>
    <label class="field-label">运镜</label>
    <div class="param-grid" style="grid-template-columns:1fr;">
      <label class="param-cell"><select id="vs-move" onchange="onVideoMovePick()">
        <option value="">不指定</option>
        ${MOVE_PARAMS[0].opts.map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join("")}
      </select></label>
    </div>
    <label class="field-label">视频提示词${hasPrompt ? "（可直接改）" : "（还空着，建议先点「✨ AI 重写」）"}</label>
    <textarea id="vs-prompt" rows="5" placeholder="画面怎么动、镜头怎么走、主体做什么">${esc(b.video_prompt || "")}</textarea>
    ${refVideoSheetHtml(bid)}
    <div style="display:flex;gap:8px;margin-top:16px;">
      <button class="btn btn-ghost" style="flex:1;" onclick="closeSheet()">取消</button>
      <button class="btn btn-ghost" style="flex:1;" onclick="stashVideoEdit('${b.id}');closeSheet();openBoardSheet('${b.id}')">改提示词</button>
      <button class="btn btn-primary" style="flex:1.4;" onclick="confirmVideo('${b.id}')">🎬 开始生成</button>
    </div>`);
  const sel = $("vs-move");
  if (sel) sel.value = cur ? matchOptValue(MOVE_PARAMS[0], cur) : "";
  const ms = $("vs-model");
  if (ms) ms.value = getVideoWorkflow();
  const rs = $("vs-res");
  if (rs) rs.value = getDoubaoRes();
  const fs = $("vs-frame");
  if (fs) fs.value = getDoubaoFrame();
}
function getDoubaoRes() { return localStorage.getItem("wb_doubao_res") || "480p"; }
function saveDoubaoRes(el) { localStorage.setItem("wb_doubao_res", el.value); toast("豆包分辨率已切换：" + el.value); }
function getDoubaoFrame() { return localStorage.getItem("wb_doubao_frame") || "reference"; }
function saveDoubaoFrame(el) {
  localStorage.setItem("wb_doubao_frame", el.value);
  toast("帧模式已切换：" + (el.value === "first" ? "首帧（从分镜图开始动）" : "参考图"));
}
function onVideoMovePick() {
  const sel = $("vs-move"), ta = $("vs-prompt");
  if (ta) ta.value = setParam(ta.value, "运镜", sel ? sel.value : "");
}
/** 把弹窗里的运镜与提示词写回数据（正文必须带运镜：生成视频只读 video_prompt） */
function stashVideoEdit(bid) {
  const b = board(bid);
  if (!b) return;
  const sel = $("vs-move"), ta = $("vs-prompt");
  const v = sel ? sel.value : "";
  b.video_prompt = setParam(((ta && ta.value) || "").trim(), "运镜", v);
  b.shot_params = b.shot_params || { img: {}, video: {} };
  b.shot_params.video = b.shot_params.video || {};
  if (v) b.shot_params.video["运镜"] = v; else delete b.shot_params.video["运镜"];
  touch();
}
/* ---------- 垫参考视频（豆包 Seedance reference_video）：新增 / 删除，随项目云端持久化 ---------- */
function refVideosList() {
  return ((S.cur && S.cur.data && S.cur.data.refVideos) || []);
}
/** 视频弹窗里的垫参考视频区块：缩略图列表 + 上传/链接 两个入口，逐条可删 */
function refVideoSheetHtml(bid) {
  const items = refVideosList();
  const rows = items.length ? items.map((v, i) => `
    <div class="rv-item">
      <video src="${esc(v.url)}" muted preload="metadata"></video>
      <span class="rv-name" title="${esc(v.name || "")}">${esc(v.name || "参考视频")}</span>
      <button class="vbtn" title="移除此参考视频" onclick="delRefVideo(${i},'${bid}')">🗑</button>
    </div>`).join("") : `<p class="lib-empty" style="text-align:left;">还没垫参考视频（可选）：垫一段引导运镜/节奏，不会出现在成片里</p>`;
  return `
    <label class="field-label">🎞️ 垫参考视频（引导运镜/节奏，不入成片 · 最多 10 条）</label>
    <div class="rv-list">${rows}</div>
    <div style="display:flex;gap:8px;margin-top:6px;">
      <button class="btn btn-ghost" style="flex:1;" onclick="addRefVideoFile('${bid}')">➕ 上传视频</button>
      <button class="btn btn-ghost" style="flex:1;" onclick="addRefVideoLink('${bid}')">🔗 添加链接</button>
    </div>`;
}
function rvEnsureCap() {
  if (refVideosList().length < 10) return true;
  toast("最多垫 10 条参考视频，请先删除一条", true);
  return false;
}
function rvAdd(url, name) {
  const data = S.cur.data;
  if (!data.refVideos) data.refVideos = [];
  data.refVideos.push({ url, name });
  touch(); saveNow();
}
async function addRefVideoFile(bid) {
  if (!rvEnsureCap()) return;
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "video/mp4,video/webm,video/quicktime";
  inp.onchange = async () => {
    const f = inp.files && inp.files[0];
    if (!f) return;
    if (f.size > 100 * 1024 * 1024) return toast("参考视频过大（超过 100MB），请压缩后再上传", true);
    showBusy(`正在上传「${f.name}」…`);
    try {
      const dataUrl = await new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res(r.result);
        r.onerror = () => rej(new Error("读取视频文件失败"));
        r.readAsDataURL(f);
      });
      const resp = await api("/api/upload-ref-video", { video_base64: dataUrl, filename: f.name }, "POST", 120000);
      rvAdd(resp.video_url, f.name);
      hideBusy();
      toast("参考视频已添加");
      if (bid) openVideoSheet(bid); else renderBoards();
    } catch (e) {
      hideBusy();
      toast("参考视频上传失败：" + e.message, true);
    }
  };
  inp.click();
}
function addRefVideoLink(bid) {
  if (!rvEnsureCap()) return;
  const url = (prompt("输入参考视频 URL：\n· 公网可访问的 mp4 直链\n· 或本地图床地址 /static/uploads/...") || "").trim();
  if (!url) return;
  rvAdd(url, url.split("/").pop().split("?")[0] || "外部链接");
  toast("参考视频已添加");
  if (bid) openVideoSheet(bid);
}
async function delRefVideo(i, bid) {
  const items = refVideosList();
  const it = items[i];
  if (!it) return;
  if (!confirm(`移除参考视频「${it.name || it.url}」？\n（上传的本机文件会一并从服务器删除）`)) return;
  items.splice(i, 1);
  touch(); saveNow();
  const fname = String(it.url).split("/").pop().split("?")[0];
  if (fname.startsWith("refvideo_")) {
    try { await api("/api/delete-files", { urls: [it.url] }, "POST", 15000); } catch (_) {}
  }
  toast("已移除");
  if (bid) openVideoSheet(bid);
}
/** 关闭分镜卡片上的视频失败报错（不清任务，只是把红色提示条收起来） */
function dismissVideoError(bid) {
  const b = board(bid);
  if (!b) return;
  b.videoStatus = "";
  b.videoError = "";
  touch(); saveNow(); renderBoards();
}
function confirmVideo(bid) {
  const b = board(bid);
  if (!b) return;
  stashVideoEdit(bid);
  closeSheet();
  // 光有「运镜：xx」不算提示词——剥掉参数后要有真正的画面内容，否则生成出来不对
  const body = (b.video_prompt || "")
    .replace(/运镜\s*[:：][^，,；;、。\n】]*/g, "")
    .replace(/[，,；;、。\s]+/g, "")
    .trim();
  if (!body) return toast("视频提示词是空的，先点「✨ AI 重写」补上画面内容再生成", true);
  genBoardVideo(bid);
}
async function genBoardVideo(bid) {
  const b = board(bid);
  if (!b || b.videoStatus === "running") return;
  if (!b.images.length) return toast("先生成分镜图", true);
  if (!b.video_prompt) return toast("缺少视频提示词，先在「提示词」里补全", true);
  b.videoStatus = "running";
  b.videoError = null;
  b.videoUrl = null; // 重新生成：清掉上一次的成片，否则轮询被旧结果短路、界面也一直显示旧视频
  b.videoStartedAt = Date.now();
  b.videoSubmitting = true; // 提交请求还在路上（组 prompt+传图要几秒），此时没有任务 ID 属正常——自愈逻辑不得误判
  touch(); // 立即落盘 running 状态：刷新/闪退后僵尸状态能被自愈逻辑识别，而不是静默丢失
  renderBoards();
  const landscape = S.cur.data.orientation === "landscape";
  // 参考图池对齐 3D 桌面版：基础 =【本镜分镜图 + 调度图】。
  // 角色/场景设定图不默认全量进池——H3 参考模式会把参考图里的每个主体都当成"要描绘的对象"，
  // 塞进整个角色库就会把无关角色画进画面（实测：织布镜头冒出老人），塞进场景库就会换景
  // （实测：公园黄昏变室内走廊）。进池的设定图来自视频提示词：①中括号【显式引用】
  //（角色含指定服装变体，另含场景/道具）；②纯文本写了角色名 → 该角色本体主图入池
  //（背景/虚化角色除外）。后端会在提示词里注明各图"只锁外观，动作人数以提示词为准"。
  const urls = [b.images[0]];
  const vidMeta = [{ kind: "storyboard", name: "本镜分镜图" }]; // 与 urls 一一对应
  const diagUrl = b.trajView || b.topView;
  if (diagUrl && !urls.includes(diagUrl)) { urls.push(diagUrl); vidMeta.push({ kind: "diagram", name: "调度图" }); }
  const vidRefs = resolveAssetRefs(b, true);
  for (const r of vidRefs) {
    if (urls.length >= 9) break;               // 后端参考图上限 9 张
    if (urls.includes(r.url)) continue;
    urls.push(r.url);
    vidMeta.push(r.meta);
  }
  while (vidMeta.length > urls.length) vidMeta.pop();
  const vidRefNames = [...new Set(vidRefs.map((r) => (r.meta && r.meta.name) || "").filter(Boolean))];
  if (vidRefNames.length) toast(`视频参考池含设定图：${vidRefNames.join("、")}`);
  try {
    // 按所选模型自动适配提交模式：默认多图参考模式（分镜图 + 调度图一起作参考）。
    // 首尾帧工作流必须真的有两张不同的图——拿同一张图当首帧和尾帧（起点=终点），
    // 模型只会让画面呆住、或为了"回到起点"瞎编动作，这是视频崩坏的头号原因。
    // 所以只有本镜确实存在第二张不同画面（接上一镜的末帧 / 本镜第二张图）时才走首尾帧，否则降级参考模式。
    // doubao: 前缀 = 豆包 Seedance（火山方舟）：走 Ark 提交通道，始终参考模式（垫参考视频一并传入）。
    const wf = getVideoWorkflow();
    const isDoubao = (wf || "").startsWith("doubao:");
    const doubaoModel = isDoubao ? wf.slice("doubao:".length) : undefined;
    const wantsFirstLast = !isDoubao && wf === "minimax_h3_lightx2v";
    // 豆包首帧模式：视频从本镜分镜图开始动（服装/造型一致性最高），代价是调度图/设定图不进参考池
    const doubaoFirst = isDoubao && getDoubaoFrame() === "first";
    let lastUrl = null;
    if (b.linkPrev) {
      const boards = S.cur?.data.boards || [];
      const idx = boards.findIndex((x) => x.id === bid);
      const prev = idx > 0 ? boards[idx - 1] : null;
      lastUrl = prev ? ((prev.images || []).filter(Boolean).slice(-1)[0] || null) : null;
    }
    if (!lastUrl && (b.images || []).length > 1) lastUrl = b.images[1];
    const isFirstLast = wantsFirstLast && !!lastUrl && lastUrl !== b.images[0];
    if (wantsFirstLast && !isFirstLast) toast("首尾帧需要两张不同的画面，本镜只有一张：已自动改用参考模式", false);
    console.log(`[视频] ${new Date().toLocaleTimeString()} 开始提交（XHR）：engine=${isDoubao ? "doubao:" + doubaoModel : "autodl:" + (wf || "默认")}, 首尾帧=${isFirstLast}, 图=${urls.length}张`);
    const r = await xhrApi("/api/submit-video", {
      prompt: b.video_prompt,
      image_urls: urls,
      ref_meta: vidMeta,
      duration: b.duration,
      resolution: "768p",
      frame_mode: isFirstLast ? "first_last" : (doubaoFirst ? "first" : "reference"),
      first_frame_url: (isFirstLast || doubaoFirst) ? b.images[0] : undefined,
      last_frame_url: isFirstLast ? lastUrl : undefined,
      image_aspect: landscape ? 16 / 9 : 9 / 16,
      workflow_id: isDoubao ? undefined : (wf || undefined),
      video_engine: isDoubao ? "doubao" : "autodl",
      video_model: doubaoModel,
      doubao_resolution: isDoubao ? getDoubaoRes() : undefined,
      // 垫参考视频：所有引擎都传（豆包 Seedance 作 reference_video；AutoDL 工作流不接受时后端自动去掉重试）
      ref_video_urls: refVideosList().map((v) => v.url),
      // 页面链接里的 sharecode + 页面公网地址：后端把本地上传的垫视频拼成公网直链给云端拉取
      // （请求经代理转发后后端看到的 host 是内网地址，所以必须由前端显式传 origin）
      share_code: (new URLSearchParams(location.search).get("sharecode") || undefined),
      page_origin: location.origin,
      // 精确模式才传 plot_data（后端机位几何翻译）；参考模式不传，图纸只作大致参考
      plot_data: (stagingModeOf(b) === "precise") ? ((b.trajViewPlot || b.topViewPlot) || undefined) : undefined,
      env_context: buildVideoEnvContext(b), // 场景环境锁定：视频提示词没写环境时防止模型脑补室内等错误场景
    }, 180000); // 提交含 AI 翻译+传图，给 3 分钟硬超时：挂死时明确报错而不是永远"提交中"
    console.log(`[视频] ${new Date().toLocaleTimeString()} 提交成功 taskId=${r.task_id}`);
    b.videoTaskId = r.task_id;
    b.videoSubmitting = false; // 任务 ID 已到手，解除自愈豁免
    touch(); saveNow(); // 立刻落盘任务 ID，防止刷新/服务重启后丢任务卡在"渲染中"
    if (r.warning) toast(r.warning, true);
    startPoll(bid);
  } catch (e) {
    console.log(`[视频] ${new Date().toLocaleTimeString()} 提交失败: ${e.message}`);
    b.videoSubmitting = false; // 提交结束（失败），解除豁免，让真实报错原因显示出来
    b.videoStatus = "failed";
    b.videoError = e.message; // 失败原因写进卡片，不再只靠一闪而过的 toast
    toast(e.message, true);
  }
  touch(); renderBoards();
}
/* ---------- 分镜配音：台词 → VoxCPM 合成（可选角色克隆音色） ---------- */
async function openDubSheet(bid) {
  const b = board(bid);
  if (!b) return;
  if (S.voiceReady === null) await voiceStatusProbe();
  if (!S.voiceReady) { openVoiceHelp(); return; }
  const chars = (((S.cur.data.story || {}).characters) || []).filter((c) => c.voice_url);
  const presets = S.voicePresets || [];
  const hasChars = chars.length > 0;
  openSheet(`
    <div class="sheet-title">镜头 ${b.shot_number} · 配音</div>
    <label class="field-label">台词 / 旁白（会同步写回分镜）</label>
    <textarea id="dub-text" rows="3">${esc(b.dialogue || "")}</textarea>
    ${b.dialogue ? "" : `<p class="lib-empty" style="text-align:left;margin-top:4px;">分镜里还没有台词，直接在上面写一段即可</p>`}
    <label class="field-label">音色</label>
    <select id="dub-voice">
      ${hasChars ? `<optgroup label="角色克隆音色">${chars.map((c, ci) => `<option value="${ci}"${ci === 0 ? " selected" : ""}>🎭 ${esc(c.name)}${c.voice_text ? "" : "（缺参考文本 ⚠️）"}</option>`).join("")}</optgroup>` : ""}
      ${presets.length ? `<optgroup label="预设音色">${presets.map((p, pi) => `<option value="preset:${p.id}"${!hasChars && pi === 0 ? " selected" : ""}>${esc(p.label)}</option>`).join("")}</optgroup>` : ""}
      <option value=""${hasChars || presets.length ? "" : " selected"}>🎲 随机音色（音色不稳，仅供测试）</option>
    </select>
    ${hasChars ? "" : `<p class="lib-empty" style="text-align:left;margin-top:4px;">想要专属声音：到「角色场景」页打开角色设定，上传 5-20 秒清晰人声，并把这段音频里说的那句话逐字填进「音色参考文本」</p>`}
    <button class="btn btn-primary btn-block" id="dub-gen" onclick="genDub('${b.id}')">🔊 生成配音</button>
    ${(b.dubs || []).length ? `<label class="field-label">已生成的配音</label>${b.dubs.map((d, di) => `
      <div class="dub-item">
        <span class="dub-tag">🔊 ${esc(d.voice || "随机音色")}</span>
        <audio controls preload="metadata" src="${d.url}"></audio>
        <button class="vbtn" title="下载此配音" onclick="downloadDub('${b.id}',${di})">⬇</button>
        <button class="vbtn" title="删除此配音" onclick="delDub('${b.id}',${di})">🗑</button>
      </div>`).join("")}` : ""}`);
}
async function genDub(bid) {
  const b = board(bid);
  const btn = $("dub-gen");
  if (!b || !btn) return;
  const text = ($("dub-text").value || "").trim();
  if (!text) return toast("先填写台词 / 旁白", true);
  b.dialogue = text; // 顺手写回分镜，保持台词与配音一致
  const sel = $("dub-voice");
  const chars = (((S.cur.data.story || {}).characters) || []).filter((c) => c.voice_url);
  // 音色三选一："" 随机 / preset:ID 预设音色 / 数字 角色克隆音色
  const v = sel ? sel.value : "";
  const presetId = v.startsWith("preset:") ? v.slice(7) : null;
  const pick = /^\d+$/.test(v) ? chars[parseInt(v)] : null;
  btn.classList.add("busy");
  btn.textContent = "提交任务…";
  // 异步任务模式：提交立即返回 job_id，轮询结果（云端 CPU 合成，同步等待会被网关掐断）
  const submitAndPoll = async () => {
    const r = await api("/api/generate-dub", {
      text,
      voice_url: pick ? pick.voice_url : undefined,
      voice_text: pick ? (pick.voice_text || "") : undefined,
      preset_id: presetId || undefined,
    }, "POST", 30000);
    if (!r.job_id) throw new Error("提交任务失败");
    let res = null;
    for (let t = 0; t < 96; t++) {
      await new Promise((s) => setTimeout(s, 5000));
      res = await api(`/api/dub-result?job_id=${encodeURIComponent(r.job_id)}`, undefined, "GET", 15000);
      if (res.status === "done") break;
      if (res.status === "error") throw new Error(res.detail || "合成失败");
      if (t % 4 === 3) btn.textContent = `合成中… ${Math.round((t + 1) * 5 / 6) / 10} 分钟`;
    }
    if (!res || res.status !== "done") throw new Error("合成超时（约 8 分钟），请稍后重试");
    return res;
  };
  try {
    let res;
    try {
      res = await submitAndPoll();
    } catch (e) {
      // 沙箱偶发重启会有几秒服务空窗，自动重试一次
      const connErr = /ConnectError|Connection refused|还没就绪|503|fetch/i.test(e.message || "");
      if (!connErr) throw e;
      btn.textContent = "配音服务重启中，30 秒后自动重试…";
      await new Promise((s) => setTimeout(s, 30000));
      res = await submitAndPoll();
    }
    b.dubs ||= [];
    const vLabel = presetId ? ((S.voicePresets || []).find((p) => p.id === presetId) || {}).label || "预设音色" : (pick ? pick.name : "");
    b.dubs.push({ url: res.audio_url, voice: vLabel });
    touch(); renderBoards(); renderVoiceBoardList(); openDubSheet(bid);
    toast("配音已生成");
  } catch (e) {
    toast(e.message, true);
  }
  btn.classList.remove("busy");
  btn.textContent = "🔊 生成配音";
}
function delDub(bid, idx) {
  const b = board(bid);
  if (!b || !b.dubs || !b.dubs[idx]) return;
  const [gone] = b.dubs.splice(idx, 1);
  touch(); renderBoards(); renderVoiceBoardList();
  if ($("dub-text")) openDubSheet(bid); // 配音抽屉开着时刷新列表
  if (gone) purgeServerFiles([gone.url]);
}
// 下载角色参考音频（音色样本）
async function downloadVoice(ci) {
  const c = (((S.cur?.data.story || {}).characters) || [])[ci];
  if (!c?.voice_url) return toast("该角色未设定音色", true);
  try {
    const resp = await fetch(c.voice_url);
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `音色参考_${c.name || ci}.wav`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) {
    toast("下载失败，改为新标签打开", true);
    window.open(c.voice_url, "_blank");
  }
}
// 下载配音音频：blob 方式触发保存（手机浏览器 download 属性常被忽略，blob 兜底更稳）
async function downloadDub(bid, idx) {
  const b = board(bid);
  const d = b?.dubs?.[idx];
  if (!d?.url) return toast("配音不存在", true);
  try {
    const resp = await fetch(d.url);
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    const shot = b.shot_number ? `镜头${b.shot_number}` : bid;
    const voice = d.voice || "随机音色";
    const ts = new Date().toISOString().slice(5, 16).replace(/[T:]/g, "-");
    a.download = `配音_${shot}_${voice}_${ts}.wav`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  } catch (e) {
    toast("下载失败，改为新标签打开", true);
    window.open(d.url, "_blank"); // 兜底：新标签打开可长按/右键保存
  }
}
function delDubAudio(bid, idx) {
  // 音频 404（文件被清理过）时自动摘除坏条目，不整体报错
  const b = board(bid);
  if (!b || !b.dubs || !b.dubs[idx]) return;
  b.dubs.splice(idx, 1);
  touch(); renderBoards(); renderVoiceBoardList();
}

function startPoll(bid) {
  stopPoll(bid);
  S.pollers[bid] = setInterval(() => pollOnce(bid), 4000);
  pollOnce(bid);
}
function stopPoll(bid) {
  if (S.pollers[bid]) { clearInterval(S.pollers[bid]); delete S.pollers[bid]; }
}
function resumePollers(fromReload) {
  let recovered = false;
  for (const b of S.cur.data.boards || []) {
    if (fromReload) b.videoSubmitting = false; // 刷新/重开项目后旧的提交请求已死，豁免标志一并清掉
    // 状态是"渲染中"却没有任务 ID → 永远轮不到结果，标记失败让用户可重试
    if (b.videoStatus === "running" && !b.videoTaskId) {
      if (b.videoSubmitting) continue; // 回到前台（没刷新页面）：提交请求可能还在路上，等它自己成功或报错
      b.videoStatus = "failed";
      b.videoError = "任务没有提交成功（没有拿到任务 ID），请重试";
      recovered = true;
      continue;
    }
    if (b.videoTaskId && !b.videoUrl && b.videoStatus === "running") startPoll(b.id);
  }
  if (recovered) { touch(); renderBoards(); }
}
async function pollOnce(bid) {
  const b = board(bid);
  if (!b || !b.videoTaskId || b.videoUrl) return stopPoll(bid);
  // 防重入锁：轮询间隔 4 秒，而「查询状态/保存成片」可能耗时超过 4 秒（服务端有网络重试），
  // 下一 tick 会在 videoUrl 还没写上时再次进入 → 两个轮询都走到成功分支各自 push 一次，
  // 造成一次生成入列两条版本（实测 3 次生成出现 6 条）。同一分镜同时只允许一个 pollOnce 在跑。
  if (S.pollBusy[bid]) return;
  S.pollBusy[bid] = true;
  try {
    let r;
    try { r = await api(`/api/video-status/${b.videoTaskId}`); }
    catch (_) { return; } // 网络抖动下一轮继续
    const status = (r.status || "").toUpperCase();
    if (["SUCCESS", "COMPLETED", "SUCCEEDED"].includes(status) && r.video_url) {
      // 双保险：另一个竞争者可能已在我等待期间完成入列，videoUrl 有值就不再重复处理
      if (!b.videoUrl && !b.videos.includes(r.video_url)) {
        try {
          const sv = await api("/api/save-video", { task_id: b.videoTaskId });
          b.videos.push(sv.video_url); // 多版本：新成片追加进列表，旧版本保留由创作者取舍
          b.videoUrl = sv.video_url;   // 「最新成片」别名：下载/轮询短路等旧逻辑继续依赖它
          b.videoStatus = "done";
          toast(`🎬 镜头 ${b.shot_number} 成片已保存`);
        } catch (e) {
          b.videos.push(r.video_url); // 兜底：直接用云端临时链接
          b.videoUrl = r.video_url;
          b.videoStatus = "done";
          toast(e.message + "（已用临时链接展示，建议尽快保存）", true);
        }
      }
      stopPoll(bid);
      touch(); if (S.tab === "boards") renderBoards();
    } else if (["FAILED", "ERROR", "CANCELLED", "TIMEOUT"].includes(status)) {
      b.videoStatus = "failed";
      b.videoError = r.error || null; // 后端翻译好的失败原因（版权误伤/内容审核等），写进卡片可关闭提示条
      stopPoll(bid);
      toast(`镜头 ${b.shot_number} 视频生成失败`, true);
      touch(); if (S.tab === "boards") renderBoards();
    } else if (S.tab === "boards") {
      const elapsed = (Date.now() - (b.videoStartedAt || Date.now())) / 1000;
      // 活跃轮询下也做超时兜底：平台任务挂死时不再无限"渲染中"（云端负载高时渲染可达十几分钟，给足 20 分钟）
      if (elapsed > 1200) {
        b.videoStatus = "failed";
        b.videoError = "渲染超过 20 分钟未完成，已自动停止，请重试";
        stopPoll(bid);
        toast("渲染超时，已停止，请重试", true);
        touch(); renderBoards();
        return;
      }
      // 估算进度：经验值约 100 秒渲染完
      const pct = Math.min(95, Math.round((elapsed / 100) * 100) + 5);
      const bar = $("vp-" + bid), txt = $("vt-" + bid);
      if (bar) bar.style.width = pct + "%";
      if (txt) txt.textContent = `云端渲染中… ${pct}%（约 1–3 分钟）`;
    }
  } finally {
    S.pollBusy[bid] = false;
  }
}

/* ================= 设置页 ================= */
function getVideoWorkflow() {
  return localStorage.getItem("wb_video_workflow") || "";
}
function saveVideoWorkflow(sel) {
  sel = sel || document.getElementById("set-workflow");
  if (!sel) return;
  localStorage.setItem("wb_video_workflow", sel.value);
  const label = sel.selectedOptions && sel.selectedOptions[0] ? sel.selectedOptions[0].textContent : sel.value;
  toast("视频模型已切换：" + label);
}
function renderSettings() {
  $("set-workflow").value = getVideoWorkflow();
  syncOrientSeg();
  promptLibProbe(); // 探测提示词库是否就绪（失败/未配置自动隐藏入口，静默）
  voiceStatusProbe(); // 探测配音微服务（未启动时设置页显示启动说明）
}
/* ================= 配音服务（VoxCPM 声音克隆微服务） ================= */
/* voiceStatusProbe 定义在「配音页」区块（class 版，同时更新配音页与设置页的状态卡） */
function openVoiceHelp() {
  openSheet(`
    <div class="sheet-title">🔊 配音服务（VoxCPM 声音克隆）</div>
    <p style="font-size:13.5px;line-height:1.7;margin:6px 0 10px;">
      ${S.voiceReady ? "✅ 服务已就绪，可以正常生成配音。"
        : S.voiceWarming ? "⏳ 模型预热中：在线版首次部署会自动下载模型（约 1GB，几分钟），完成后即可正常配音，请稍后点「重新探测」。"
        : "⚠️ 服务不可用：在线版稍等几分钟让模型完成下载预热后重试；本地版需在电脑上启动配音微服务。"}
    </p>
    <div class="card pad" style="font-size:13px;line-height:1.9;">
      在线版：无需任何操作，发布后自动预热（模型下载 → 常驻内存）。<br>
      本地版启动方法（在项目目录 AI-anime-studio-3D-mobile 下执行）：<br>
      <code style="word-break:break-all;">C:/Users/ZYY/Downloads/VoxCPM-main/venv/Scripts/python.exe voice_server.py</code><br>
      · 默认端口 8021，改端口后需在 .env 加 VOICE_SERVER_URL=http://127.0.0.1:端口<br>
      · CPU 推理：每句台词约 10 秒 - 3 分钟（视机器而定），请耐心等待<br>
      · 声音克隆：在「角色场景」页给角色上传 5-20 秒清晰人声 + 填写参考文本
    </div>
    <button class="btn btn-ghost btn-block" onclick="closeSheet(); S.voiceReady = null; S.voiceWarming = false; voiceStatusProbe()">🔄 重新探测</button>`);
}
async function renderApiKeys() {
  const el = $("settings-masked");
  if (!el) return;
  try {
    const r = await api("/api/settings");
    const cust = r.custom || {};
    const mk = (k, v, isCustom) => {
      if (!v) return `<div><b>${k}</b>：未配置</div>`;
      const tag = isCustom ? " · <span style='color:#d97706;'>自定义</span>"
                           : " · <span style='color:#64748b;'>全局</span>";
      return `<div><b>${k}</b>：${esc(v)}${tag}</div>`;
    };
    el.innerHTML = [
      mk("DeepSeek", r.deepseek_api_key, cust.deepseek),
      mk("方舟 ARK", r.ark_api_key, cust.ark),
      mk("AutoDL 令牌", r.autodl_token, cust.autodl),
    ].join("");
  } catch (_) {}
}
async function saveSettings() {
  const body = {};
  const map = { "set-deepseek": "deepseek_api_key", "set-ark": "ark_api_key", "set-autodl": "autodl_token" };
  for (const [id, key] of Object.entries(map)) {
    const v = $(id).value.trim();
    if (v) body[key] = v;
  }
  if (!Object.keys(body).length) return toast("没有填写新密钥");
  showBusy("保存密钥…");
  try {
    await api("/api/settings", body);
    for (const id of Object.keys(map)) $(id).value = "";
    hideBusy();
    toast("密钥已保存，立即生效");
    renderApiKeys();
  } catch (e) { hideBusy(); toast(e.message, true); }
}

/* ================= API 设置页（首页 / 工作台均可进入的独立页） ================= */
let apiFrom = null; // 记录从哪个屏幕进入，返回时还原
function showApiScreen(from) {
  // 来源必须显式传入（home/studio）。不能用 S.cur 猜：回首页后 S.cur 仍保留，
  // 会导致从首页打开、关闭后却落到详情页。
  apiFrom = (from === "studio" || from === "home") ? from : "home";
  $("home-screen").classList.add("hidden");
  $("studio-screen").classList.add("hidden");
  $("api-screen").classList.remove("hidden");
  $("set-deepseek").value = ""; $("set-ark").value = ""; $("set-autodl").value = "";
  renderApiKeys();
}
function closeApiScreen() {
  $("api-screen").classList.add("hidden");
  $(apiFrom === "studio" ? "studio-screen" : "home-screen").classList.remove("hidden");
}
async function deleteCurrentProject() {
  if (!S.cur) return;
  if (!confirm(`确定删除项目「${S.cur.name}」？数据删除后无法恢复，项目里的图片/视频文件也将从服务器一并删除。`)) return;
  try {
    await api(`/api/projects/${S.cur.id}`, undefined, "DELETE");
    toast("已删除");
    backHome();
  } catch (e) { toast(e.message, true); }
}

/* ================= 抽屉 / 灯箱 ================= */
function openSheet(html) {
  $("sheet-body").innerHTML = html;
  $("sheet-mask").classList.remove("hidden");
  $("sheet").classList.remove("hidden");
}
function closeSheet(e) {
  if (e && e.target && e.target.id !== "sheet-mask") return;
  $("sheet-mask").classList.add("hidden");
  $("sheet").classList.add("hidden");
}
function showLightbox(url) {
  $("lightbox-img").src = url;
  $("lightbox").classList.remove("hidden");
}
function closeLightbox() {
  $("lightbox").classList.add("hidden");
  $("lightbox-img").src = "";
}

/* ================= 画幅方向（设置页） ================= */
function syncOrientSeg() {
  const landscape = S.cur?.data.orientation === "landscape";
  document.querySelectorAll("#orient-seg .seg-item").forEach((el) =>
    el.classList.toggle("active", (el.dataset.k === "landscape") === landscape));
}
function setOrientation(o) {
  if (!S.cur) return;
  if (S.cur.data.orientation === o) return;
  S.cur.data.orientation = o;
  touch(); syncOrientSeg();
  if (S.tab === "boards") renderBoards();
  toast(o === "landscape" ? "已切换横屏：新生成画面为 1920×1080" : "已切换竖屏：新生成画面为 1080×1920");
}

/* ================= 提示词库（Open Image Prompts 语料检索） ================= */
S.libReady = null;   // null=未探测 / true / false
S.libItems = [];     // 最近一次检索结果（复制按钮按索引取）

async function promptLibProbe() {
  if (S.libReady !== null) return S.libReady;
  try {
    const r = await api("/api/prompt-library/status", undefined, "GET", 15000);
    S.libReady = !!r.ready;
    if (!r.ready) console.log("[提示词库] 未就绪：", r.reason || "");
  } catch (e) {
    S.libReady = false;
    console.log("[提示词库] 状态探测失败：", e.message);
  }
  const card = $("prompt-lib-card");
  if (card) card.classList.toggle("hidden", !S.libReady);
  return S.libReady;
}

async function openPromptLibrary() {
  const ready = await promptLibProbe();
  if (!ready) return toast("提示词库未就绪：服务器未配置语料库（OIP_DB_PATH）", true);
  openSheet(`
    <div class="sec-title">📚 提示词库检索 <span class="sec-hint">与题目相关的真实优秀提示词，借鉴镜头感与光线写法</span></div>
    <div class="chat-input-row" style="margin-top:10px;">
      <input id="lib-q" type="text" placeholder="描述画面：主体 + 光线 + 氛围" onkeydown="if(event.key==='Enter')runPromptSearch()">
      <button class="send-btn" onclick="runPromptSearch()">➤</button>
    </div>
    <div class="lib-chips">
      <button class="lib-chip" onclick="libQuick('黄昏公园逆光人物剪影')">黄昏逆光剪影</button>
      <button class="lib-chip" onclick="libQuick('雨夜霓虹街道少女撑伞')">雨夜霓虹街道</button>
      <button class="lib-chip" onclick="libQuick('日系教室课后 少女')">日系教室</button>
      <button class="lib-chip" onclick="libQuick('古风水墨 江湖剑客')">古风剑客</button>
      <button class="lib-chip" onclick="libQuick('科幻 机甲 未来城市')">科幻机甲</button>
    </div>
    <div id="lib-results" class="lib-results"><p class="lib-empty">写清「主体 + 光线 + 氛围」效果最好；叙事句子也会自动宽松匹配</p></div>
  `);
  setTimeout(() => { const el = $("lib-q"); if (el) el.focus(); }, 120);
}

function libQuick(q) {
  const input = $("lib-q");
  if (!input) return;
  input.value = q;
  runPromptSearch();
}

async function runPromptSearch() {
  const q = ($("lib-q")?.value || "").trim();
  if (!q) return;
  const box = $("lib-results");
  if (!box) return;
  box.innerHTML = `<p class="lib-empty">检索中…</p>`;
  try {
    const r = await api("/api/prompt-search", { q, limit: 8 }, "POST", 60000);
    const items = [...(r.results || []), ...(r.related_results || [])];
    S.libItems = items;
    if (!items.length) {
      box.innerHTML = `<p class="lib-empty">没有找到匹配的参考，换个更具体的画面描述试试（写清主体+光线+氛围）</p>`;
      return;
    }
    const stats = [];
    if (typeof r.exact_match_count === "number") stats.push(`严格命中 ${r.exact_match_count}`);
    if (r.fallback && r.fallback.used && r.fallback.added) stats.push(`宽松补足 ${r.fallback.added}`);
    const statLine = stats.length ? `<p class="lib-stat">${esc(stats.join(" · "))}（共 ${items.length} 条）</p>` : "";
    box.innerHTML = statLine + items.map(libItemHtml).join("");
  } catch (e) {
    box.innerHTML = `<p class="lib-empty">检索失败：${esc(e.message)}</p>`;
  }
}

function libItemHtml(it, i) {
  const tags = (it.tags || []).slice(0, 6).map((t) => `<span class="lib-tag">${esc(t.zh || t.id)}</span>`).join("");
  const zh = it.prompt_zh || "";
  const en = it.prompt_text || "";
  const zhEsc = esc(zh);
  return `
  <div class="lib-item">
    <div class="lib-meta">#${i + 1} · ${esc(it.tool || "未知模型")} · 匹配分 ${it.score ?? "-"}${it.match_kind === "related" ? " · 相关推荐" : ""}</div>
    ${zh ? `<div class="lib-zh">${zhEsc}</div>` : ""}
    ${en && en !== zh ? `<details class="lib-en"><summary>查看原文</summary><div class="lib-en-text">${esc(en)}</div></details>` : ""}
    ${tags ? `<div class="lib-tags">${tags}</div>` : ""}
    <div class="lib-actions">
      <button class="btn btn-ghost lib-btn" onclick="copyPromptLibText(${i})">📋 复制</button>
      ${it.source_url ? `<a class="lib-link" href="${esc(it.source_url)}" target="_blank" rel="noopener">🔗 来源</a>` : ""}
    </div>
  </div>`;
}

function copyPromptLibText(i) {
  const it = S.libItems[i];
  const text = it ? (it.prompt_zh || it.prompt_text || "") : "";
  if (!text) return toast("没有可复制的内容", true);
  const done = () => toast("已复制到剪贴板");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopyText(text, done));
  } else {
    fallbackCopyText(text, done);
  }
}
function fallbackCopyText(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); }
  catch (_) { toast("复制失败，请长按文本手动复制", true); }
  ta.remove();
}

/* ================= 风格卡参考（img-gen-taste 可视化） ================= */
S.scCache = S.scCache || {};

async function openStyleCards() {
  openSheet(`
    <div class="sec-title">🎨 风格卡参考 <span class="sec-hint">10 个美术方向的要点卡；点开复制后粘贴进画风圣经或生图提示词</span></div>
    <div id="sc-list" class="sc-list"><p class="lib-empty">加载中…</p></div>
  `);
  try {
    const r = await api("/api/style-cards", undefined, "GET", 15000);
    const cards = r.cards || [];
    const list = $("sc-list");
    if (!list) return;
    if (!cards.length) {
      list.innerHTML = `<p class="lib-empty">没有可用的风格卡</p>`;
      return;
    }
    list.innerHTML = cards.map((c) => `
      <div class="sc-item" onclick="toggleStyleCard('${esc(c.id)}')">
        <div class="sc-head"><b>${esc(c.title)}</b><span class="sc-id">${esc(c.id)}</span></div>
        <p class="sc-summary">${esc(c.summary)}</p>
        <div class="sc-detail hidden" id="sc-detail-${esc(c.id)}"></div>
      </div>`).join("");
  } catch (e) {
    const list = $("sc-list");
    if (list) list.innerHTML = `<p class="lib-empty">加载失败：${esc(e.message)}</p>`;
  }
}

async function toggleStyleCard(id) {
  if (!/^[a-z0-9-]{2,40}$/.test(id)) return;
  const box = $("sc-detail-" + id);
  if (!box) return;
  if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  if (box.dataset.loaded) return;
  box.innerHTML = `<p class="lib-empty">加载中…</p>`;
  try {
    const r = await api("/api/style-cards/" + id, undefined, "GET", 15000);
    S.scCache[id] = r.content || "";
    box.innerHTML = `
      <pre class="sc-content">${esc(r.content)}</pre>
      <div class="lib-actions">
        <button class="btn btn-ghost lib-btn" onclick="copyStyleCardContent('${esc(id)}')">📋 复制要点</button>
        <span class="lib-hint">粘贴进「画风圣经」或生图提示词作参考</span>
      </div>`;
    box.dataset.loaded = "1";
  } catch (e) {
    box.innerHTML = `<p class="lib-empty">加载失败：${esc(e.message)}</p>`;
  }
}

async function copyStyleCardContent(id) {
  const text = S.scCache[id] || "";
  if (!text) return toast("内容未加载，请稍后再试", true);
  const done = () => toast("已复制，可粘贴进画风圣经或提示词");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopyText(text, done));
  } else {
    fallbackCopyText(text, done);
  }
}

/* ================= 调度图 ================= */
const DIAGRAM_PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080", "#f032e6", "#9a6324", "#000075", "#800000"];
const DIAGRAM_KINDS = { topview: "俯视调度图", trajectory: "轨迹调度图" };
function diagKey(kind, suffix) { return (kind === "trajectory" ? "traj" : "top") + "View" + suffix; }
function openDiagramSheet(bid, kind) {
  const b = board(bid);
  if (!b) return;
  kind = kind || "topview";
  const url = b[diagKey(kind, "")];
  const plot = b[diagKey(kind, "Plot")];
  openSheet(`
    <div class="sheet-title">镜头 ${b.shot_number} · 调度图</div>
    <div class="sheet-sub">AI 把你的口述解析成走位坐标，程序绘制成俯视图纸（不是 AI 生图，永远精确）；生成画面/视频时自动作为导演定稿参考</div>
    <div class="seg">
      <button class="seg-item ${kind === "topview" ? "active" : ""}" onclick="openDiagramSheet('${bid}','topview')">俯视图 · 站位机位</button>
      <button class="seg-item ${kind === "trajectory" ? "active" : ""}" onclick="openDiagramSheet('${bid}','trajectory')">轨迹图 · 运动路线</button>
    </div>
    ${url ? `<div class="diag-thumb"><img src="${url}" loading="lazy" onclick="showLightbox(this.src)"></div>` : ""}
    <div style="display:flex;gap:8px;align-items:center;margin:8px 0 0;">
      <button class="btn ${stagingModeOf(b) === "precise" ? "btn-primary" : "btn-ghost"}" style="flex:1;" onclick="toggleStagingMode('${bid}','${kind}')">${stagingModeOf(b) === "precise" ? "🎯 精确模式：机位几何严格锁定（当前）" : "🌫 参考模式：大致参考可小幅移动（当前）"}</button>
    </div>
    <label class="field-label">导演口述（站位 / 走位 / 机位，最高优先级逐条落实）</label>
    <textarea id="dg-desc" rows="4" placeholder="例如：小帅站门口左侧，小美坐桌边；机位在窗户一侧拍全景；小帅缓缓走向镜头">${esc(b[diagKey(kind, "Desc")] || "")}</textarea>
    <div style="display:flex;gap:8px;margin-top:12px;">
      <button class="btn btn-primary" style="flex:1;" onclick="genDiagram('${bid}','${kind}')">${url ? "🔄 重生图纸" : "✨ 生成图纸"}</button>
      <button class="btn btn-ghost" style="flex:1;" ${plot ? `onclick="openDirector3D('${bid}','${kind}')"` : "disabled"}>🎬 3D 导演台</button>
      ${url ? `<button class="btn btn-danger" onclick="delDiagram('${bid}','${kind}')">🗑</button>` : ""}
    </div>
    ${plot
      ? `<div class="sheet-hint">💡 3D 导演台里单指拖人物/机位/轨迹红点，松手自动重绘图纸并同步坐标</div>`
      : `<div class="sheet-hint">💡 「3D 导演台」需要先点「生成图纸」——AI 解析口述得到站位坐标后，才能在 3D 场景里拖拽调走位</div>`}
  `);
}
/* 本镜头入画角色过滤：只在镜头描述/画面提示词/视频提示词/台词/调度口述里点名的角色
   才送给调度图解析，全剧组名单一锅端会导致无关角色被画进图纸。
   兜底：一个名字都没命中（口述可能用了昵称）→ 退回全名单，由后端 AI 按口述判断。 */
function shotCast(b) {
  const st = S.cur.data.story || {};
  const all = (st.characters || []).filter((c) => c && c.name);
  const text = [b.description, b.image_prompt, b.video_prompt, b.dialogue, b.topViewDesc, b.trajViewDesc]
    .filter(Boolean).join(" ");
  const hit = all.filter((c) => text.includes(c.name));
  return (hit.length ? hit : all).map((c) => ({ name: c.name || "", description: c.description || "" }));
}

async function genDiagram(bid, kind) {
  const b = board(bid);
  if (!b) return;
  const desc = (($("dg-desc") || {}).value || "").trim();
  b[diagKey(kind, "Desc")] = desc;
  const st = S.cur.data.story || {};
  showBusy("正在绘制调度图…", "AI 解析口述 → 坐标 → 程序绘制，约 10–20 秒");
  try {
    const r = await api("/api/render-diagram", {
      kind,
      description: b.description || "",
      dialogue: b.dialogue || "",
      duration: b.duration || 3,
      video_prompt: b.video_prompt || "",
      characters: shotCast(b),
      feedback: desc || undefined,
      // 轨迹图：整个俯视 plot 作权威起点（角色站位/机位/陈设/房间尺寸全部照抄）。
      // 俯视图重生：只把现有陈设（props，含导演在 3D 导演台新增的门/窗、旋转过的朝向）
      // 和房间尺寸（room，导演调好的真实大小）作为权威数据传回，
      // 让"生成图纸"不再洗掉手动元素编辑；人物/机位仍按口述重新解析
      base_plot: kind === "trajectory"
        ? (b.topViewPlot || undefined)
        : (((b.topViewPlot || {}).props || []).length || (b.topViewPlot || {}).room
          ? { props: (b.topViewPlot || {}).props, room: (b.topViewPlot || {}).room } : undefined),
    });
    b[diagKey(kind, "")] = r.image_url;
    b[diagKey(kind, "Plot")] = r.plot || null;
    b[diagKey(kind, "CamSummary")] = r.cam_summary || "";
    touch(); hideBusy(); closeSheet(); renderBoards();
    toast(r.source === "template"
      ? "AI 解析口述失败，已按默认布局绘制——修改口述后点「重生」重试"
      : "调度图完成！生成画面时会作为导演定稿参考");
  } catch (e) { hideBusy(); toast(e.message, true); }
}
function delDiagram(bid, kind) {
  const b = board(bid);
  if (!b) return;
  if (!confirm("确定删除这张调度图？（口述会保留）")) return;
  b[diagKey(kind, "")] = null;
  b[diagKey(kind, "Plot")] = null;
  touch(); closeSheet(); renderBoards(); toast("已删除");
}

/* ================= 3D 导演台 ================= */
function openDirector3D(bid, kind) {
  const b = board(bid);
  if (!b) return;
  kind = kind || "topview";
  const plot = b[diagKey(kind, "Plot")];
  if (!plot) return toast("这个分镜还没有调度图坐标，先生成图纸", true);
  if (!window.Director3D) return toast("3D 模块加载中，请稍候 2 秒再试", true);
  S.dir3d = { bid, kind };
  $("dir3d-title").textContent = `🎬 3D 导演台 · 镜头 ${b.shot_number} · ${DIAGRAM_KINDS[kind]}`;
  closeSheet();
  $("dir3d-overlay").classList.remove("hidden");
  mountDirector(bid, kind);
}
function mountDirector(bid, kind) {
  const b = board(bid);
  if (!b || !window.Director3D) return;
  const plot = b[diagKey(kind, "Plot")];
  if (!plot) return closeDirector3D();
  window.Director3D.open(bid + ":" + kind, $("dir3d-container"), plot, DIAGRAM_PALETTE,
    (plotCopy) => dir3dCommit(bid, kind, plotCopy), kind);
}
async function dir3dCommit(bid, kind, plotCopy) {
  try {
    const r = await api("/api/rerender-diagram", { plot: plotCopy, kind });
    const b = board(bid);
    if (!b) return;
    b[diagKey(kind, "")] = r.image_url;
    b[diagKey(kind, "Plot")] = r.plot;
    b[diagKey(kind, "CamSummary")] = r.cam_summary || "";
    touch();
    mountDirector(bid, kind);  // 新坐标重建 3D 场景
    setTimeout(() => captureShotPreview(bid, kind), 350);  // 等重建后渲染一帧再导出预览
    toast("图纸已按拖拽结果重绘，坐标已同步");
  } catch (e) {
    toast("重绘失败：" + e.message + "（原坐标未改动，可重试）", true);
  }
}
/** 🎬 导出机位预览帧（成片构图）缓存到分镜：生图时作为构图基准传进参考池（出图以预览窗所见为准）。
    时机：①每次拖拽/滑条提交重绘后（新坐标重建完渲染一帧）；②关闭导演台前（布置完就走也能拿到最新帧） */
function captureShotPreview(bid, kind) {
  const b = board(bid);
  if (!b || !window.Director3D || !window.Director3D.exportPreview) return;
  try {
    const url = window.Director3D.exportPreview(bid + ":" + (kind || "topview"));
    if (url && url.startsWith("data:image")) { b.previewShot = url; touch(); }
  } catch (_) { /* 导出失败不影响使用 */ }
}
function closeDirector3D() {
  // 关闭前导出当前机位预览帧缓存到分镜（布置完没再拖拽也会留下最新构图）
  if (S.dir3d) captureShotPreview(S.dir3d.bid, S.dir3d.kind);
  window.Director3D && window.Director3D.closeAll();
  $("dir3d-overlay").classList.add("hidden");
  // 退出后关闭调度图抽屉，直接回到分镜生图页
  $("sheet-mask").classList.add("hidden");
  $("sheet").classList.add("hidden");
  S.dir3d = null;
}

/* ================= 视频保存到手机 ================= */
// 删除指定版本（idx 缺省 = 最新成片 videoUrl）。videoUrl 是「最新成片」别名：
// 被删的是它时回落到剩余最新一条；全部删光才清空生成状态字段
async function delBoardVideo(bid, idx) {
  const b = board(bid);
  if (!b) return;
  let url;
  if (idx === undefined || idx === null) url = b.videoUrl;
  else url = (b.videos || [])[idx];
  if (!url) return;
  if (!confirm(`确定删除镜头 ${b.shot_number} 的这个视频版本吗？删除后不可恢复（建议先保存），服务器文件将一并删除。`)) return;
  // 先清镜头引用（立即生效），再静默清理服务器文件
  if (Array.isArray(b.videos)) {
    const i = b.videos.indexOf(url);
    if (i >= 0) b.videos.splice(i, 1);
  }
  if (b.videoUrl === url) b.videoUrl = b.videos.length ? b.videos[b.videos.length - 1] : null;
  // 全部版本删光才清生成状态——但正在生成/提交中的任务绝不能动（用户实测事故：
  // 旧视频还在渲染新一片时被删，"生成中"卡片连同任务 ID 一起被清，云端任务就丢了）
  if (!b.videos.length && b.videoStatus !== "running" && !b.videoSubmitting) {
    b.videoStatus = null; b.videoTaskId = null; b.videoError = null; b.videoStartedAt = null;
  }
  touch(); renderBoards();
  toast("已删除");
  purgeServerFiles([url]); // 服务器文件同步删除（走引用检查，被其他项目共用时自动保留）
}
function videoLoadErr(bid, idx) {
  // <video> 加载失败：文件不存在/过期（如服务器数据被重置）。多版本下把坏条目从列表
  // 摘除即可；全部失效才转失败态给出重新生成入口。不递归触发（renderBoards 重绘后剩余条目正常）
  const b = board(bid);
  if (!b) return;
  const url = (idx === undefined || idx === null) ? b.videoUrl : (b.videos || [])[idx];
  if (!url) return;
  if (Array.isArray(b.videos)) {
    const i = b.videos.indexOf(url);
    if (i >= 0) b.videos.splice(i, 1);
  }
  if (b.videoUrl === url) b.videoUrl = b.videos.length ? b.videos[b.videos.length - 1] : null;
  if (!b.videos.length) {
    b.videoStatus = "failed";
    b.videoError = "视频文件不存在或已失效（可能已被删除或服务器数据被重置），请重新生成视频";
  }
  touch(); renderBoards();
}
async function saveVideoToPhone(bid, idx) {
  const b = board(bid);
  if (!b) return;
  const url = (idx === undefined || idx === null) ? b.videoUrl : (b.videos || [])[idx];
  if (!url) return;
  const name = `镜头${b.shot_number}_${Date.now()}.mp4`;
  toast("正在准备下载…");
  let ok = false;
  // 跨域源（豆包成片在火山 CDN）必须经同源代理转发：手机浏览器直连 fetch 被 CORS 拦，
  // 回落到"长按保存"等兜底路径会丢音轨（实测：网页播放有声、下载无声）。
  // 本站文件（/static/ 与本机代理地址）直接拉，不必绕代理。
  // 本站文件（/static/uploads 下的成片/垫视频）直接拉；其余（豆包火山 CDN 等跨域源）走代理
  const isLocal = url.startsWith("/") || url.startsWith(location.origin + "/") ||
                  (url.startsWith("http") && url.includes("/static/uploads/"));
  const dlUrl = isLocal ? url : `/api/download-video?url=${encodeURIComponent(url)}`;
  try {
    const resp = await fetch(dlUrl, { headers: { "x-access-code": S.code || "" } });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const blob = await resp.blob();
    if (blob.size < 1000) throw new Error("empty");
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objUrl;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(objUrl), 60000);
    ok = true;
  } catch (_) { /* 跨域临时链接或浏览器拦截直接下载 */ }
  if (ok) {
    toast("已触发下载：查看系统通知栏或浏览器的「下载」；没找到就再点一次，会换一种保存方式");
  } else {
    showVideoSaveSheet(b, name, url);
  }
}
function showVideoSaveSheet(b, name, url) {
  openSheet(`
    <div class="sheet-title">保存镜头 ${b.shot_number} 的视频</div>
    <div class="sheet-sub">浏览器拦截了直接下载，用下面任一方式保存</div>
    <video src="${url}" controls playsinline preload="metadata" style="width:100%;border-radius:12px;background:#000;aspect-ratio:16/9;"></video>
    <a class="btn btn-primary btn-block" style="display:block;text-align:center;margin-top:12px;" href="${url}" download="${name}" target="_blank" rel="noopener">⬇️ 再次尝试下载</a>
    <div class="sheet-hint">📱 仍不行时：长按上面的视频画面 → 选「保存视频 / 下载」；或先点按钮在新页面打开，再长按视频保存</div>
  `);
}

/* ================= 启动 ================= */
window.addEventListener("load", async () => {
  // 未配置口令的服务器直接放行；配置了口令且本地没存对 → 登录页
  try {
    const ping = await fetch("/api/projects", { headers: { "x-access-code": S.code } });
    if (ping.status === 401) { showLogin(); return; }
  } catch (_) { toast("无法连接服务器，请检查网络", true); return; }
  try {
    await loadHome();
    showZoneBadge();
    $("home-screen").classList.remove("hidden");
  } catch (e) { toast(e.message, true); }
});
