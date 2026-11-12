/* AI 动画工作台 · 手机版逻辑
 * 数据全部存云端 SQLite（/api/projects），浏览器只是遥控器。
 * 生成链路：头脑风暴/点子 → /api/generate-story → 分镜逐条 /api/generate-image
 *          → /api/generate-video + 轮询 /api/video-status → /api/save-video
 */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const md = (s) => DOMPurify.sanitize(marked.parse(String(s || "")));
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
  if (!resp.ok) throw new Error(data.detail || `请求失败（HTTP ${resp.status}）`);
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
    S.cur = { id: r.id, name: r.name, data: normalizeData(r.data || {}) };
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
  clearTimeout(S.saveTimer);
  // 落库瘦身：聊天附图的 base64 dataURL 只用于当次展示，不写进数据库（体积太大）
  let payload = S.cur.data;
  try {
    payload = JSON.parse(JSON.stringify(S.cur.data));
    if (payload.draft && payload.draft.messages) {
      for (const m of payload.draft.messages) delete m.image;
    }
  } catch (_) {}
  try {
    const r = await api(`/api/projects/${S.cur.id}`, { name: S.cur.name, data: payload });
    S.dirty = false;
    setSaveState("已保存 " + new Date(r.updated_at).toTimeString().slice(0, 5));
  } catch (e) { setSaveState("保存失败，稍后重试"); }
}
/* ================= 删除同步：界面删除图片/视频时，服务器文件一并清理 ================= */
async function purgeServerFiles(urls) {
  urls = (urls || []).filter(Boolean);
  if (!urls.length) return;
  // 先把「引用已移除」落库，服务端的引用检查才不会误判文件仍在使用而拒绝删除
  try { await saveNow(); } catch (_) {}
  try { await api("/api/delete-files", { urls }); } catch (_) {} // 静默：文件清理失败不影响界面操作
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) saveNow();
  else if (S.cur) resumePollers(); // 回到前台：重启丢失的轮询、清理无任务 ID 的僵尸"渲染中"
});

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
  const chars = (d.story.characters || []).filter((c) => c && c.name)
    .map((c) => ({ name: c.name, image_prompt: c.image_prompt || "" }));
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
        characters: chars,
        description: b.description || "",
        dialogue: b.dialogue || "",
        duration: b.duration || 3,
        old_prompt: b.image_prompt || "",
        feedback: "按最新剧情梗概与画风圣经整体校准本镜头：剧情走向与人物行为符合新梗概；美术风格/光源/影调/氛围严格按新画风圣经重写；保留本镜头原有的叙事作用、景别与构图要点。",
      });
      const oldImg = b.image_prompt || "", oldVid = b.video_prompt || "";
      if (r.image_prompt) { b.image_prompt = keepParams(r.image_prompt, oldImg); ok++; }
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
  $("chat-list").innerHTML = msgs.map((m) => `
    <div class="chat-msg ${m.role === "user" ? "user" : "ai"}">
      <div class="bubble">
        ${m.role === "ai" ? md(m.content) : esc(m.content)}
        ${m.image ? `<img src="${m.image}">` : ""}
      </div>
    </div>`).join("");
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
    if (dataUrls.length) msg.image = dataUrls[0];
    d.messages.push(msg);
    pushed = true;
    input.value = "";
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
    if (pushed) d.messages.pop();  // 只回滚本次插入的消息
    toast(e.message, true);
  }
  $("chat-send").disabled = false;
  touch(); renderChat();
}
function onChatImage(input) {
  const file = input.files[0];
  input.value = "";
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => { S.chatImages.push(reader.result); toast("已附图，发送时一并传给 AI"); };
  reader.readAsDataURL(file);
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
      还没有${KIND_LABEL[k]}，先在「故事」页生成剧本</div>`;
    return;
  }
  grid.innerHTML = items.map((it, i) => `
    <div class="asset-card" onclick="openAssetSheet('${k}',${i})">
      <div class="asset-img">
        ${it.image_url ? `<img src="${it.image_url}" loading="lazy">`
          : `<div class="asset-empty">🖼<span>点击生成设定图</span></div>`}
      </div>
      <div class="asset-name"><b>${esc(it.name)}</b><span>${esc((it.description || "").slice(0, 22))}</span></div>
    </div>`).join("");
}
function openAssetSheet(kind, index) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  if (!it) return;
  const isChar = kind === "characters";
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
    <label class="field-label">AI 重写指令（可选，如"换成短发"）</label>
    <input id="as-feedback" placeholder="留空则仅规范化提示词">
    <div style="display:flex;gap:8px;margin-top:16px;">
      <button class="btn btn-ghost" style="flex:1;" onclick="aiRewrite('${kind}',${index})">✨ AI 重写</button>
      <button class="btn btn-primary" style="flex:1;" id="as-gen" onclick="genAssetImage('${kind}',${index})">🖼 生成设定图</button>
    </div>
    <button class="btn btn-ghost btn-block" onclick="saveAsset('${kind}',${index})">保存修改</button>
    ${it.image_url ? `<button class="btn btn-danger btn-block" onclick="delAssetImage('${kind}',${index})">删除当前设定图</button>` : ""}`);
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
async function genAssetImage(kind, index) {
  const it = (S.cur.data.story || {})[kind]?.[index];
  const btn = $("as-gen");
  if (!it || !btn) return;
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
      prompt: it.image_prompt,
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
async function aiRewrite(kind, index) {
  const st = S.cur.data.story;
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
      characters: (st.characters || []).map((c) => ({ name: c.name, image_prompt: c.image_prompt })),
      old_prompt: oldPrompt, feedback: feedback || undefined,
    });
    it.description = desc;            // 框内描述一并写回，避免保存后又改回去
    it.image_prompt = keepParams(r.image_prompt || oldPrompt || it.image_prompt, oldPrompt);
    hideBusy(); touch(); openAssetSheet(kind, index);
    toast("提示词已重写，可检查后生成");
  } catch (e) { hideBusy(); toast(e.message, true); }
}
function addRef(kind, index) { S.refTarget = { kind, index }; $("ref-file-input").click(); }
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
    } else if (b.videoStartedAt && Date.now() - b.videoStartedAt > 360000) {
      b.videoStatus = "failed";
      b.videoError = "渲染超过 6 分钟未完成，已自动停止，请重试（若平台侧其实已完成，重试会重新生成）";
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
      <div class="board-gen-area" style="height:auto;padding:14px;color:var(--danger);">
        生成失败，请重试${b.videoError ? `<div style="font-size:12px;margin-top:6px;opacity:.85;">原因：${esc(b.videoError)}</div>` : ""}
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
        <div class="shot-badge">${b.shot_number}</div>
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
        <button class="btn btn-ghost" onclick="openDiagramSheet('${b.id}')">🗺 调度图${hasDiag ? " ✅" : ""}</button>
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
  touch(); renderBoards();
  $("board-list").querySelector(`[data-bid="${nb.id}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  toast(`已新增第 ${nb.shot_number} 个分镜`);
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
    <label class="field-label">时长（2–5 秒）</label>
    <input id="bd-dur" type="number" min="2" max="5" value="${b.duration}">
    <label class="field-label">生图提示词</label>
    ${paramGridHtml("bd-imgp", SHOT_PARAMS)}
    <textarea id="bd-imgp" rows="6">${esc(b.image_prompt)}</textarea>
    <label class="field-label">视频提示词</label>
    <textarea id="bd-vidp" rows="4">${esc(b.video_prompt || "")}</textarea>
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
function saveBoard(bid) {
  const b = board(bid);
  if (!b) return;
  b.description = $("bd-desc").value.trim();
  b.dialogue = $("bd-dialogue").value.trim();
  b.duration = Math.min(5, Math.max(2, parseInt($("bd-dur").value) || 3));
  // 参数字典单独存一份，同时保证正文里带着它们（生图/生视频都读正文）
  b.shot_params = { img: { ...SHEET_PARAMS.img }, video: { ...SHEET_PARAMS.video } };
  b.image_prompt = applyParams($("bd-imgp").value.trim(), SHEET_PARAMS.img);
  b.video_prompt = applyParams($("bd-vidp").value.trim(), SHEET_PARAMS.video);
  touch(); closeSheet(); renderBoards(); toast("已保存");
}
async function aiRewriteBoard(bid) {
  const b = board(bid);
  const st = S.cur.data.story;
  if (!b) return;
  // 修改框当前内容优先：用户改完没点「保存」直接点 AI 重写，也必须按框里最新内容重写
  const desc = (($("bd-desc")?.value ?? b.description) || "").trim();
  const dialogue = (($("bd-dialogue")?.value ?? b.dialogue) || "").trim();
  const duration = Math.min(5, Math.max(2, parseInt($("bd-dur")?.value) || b.duration || 3));
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
      characters: ((st && st.characters) || []).map((c) => ({ name: c.name, image_prompt: c.image_prompt })),
      old_prompt: sendPrompt || "", feedback: ($("bd-feedback").value || "").trim() || undefined,
    });
    b.description = desc; b.dialogue = dialogue; b.duration = duration;  // 框内改动一并写回
    b.shot_params = { img: { ...SHEET_PARAMS.img }, video: { ...SHEET_PARAMS.video } };
    b.image_prompt = applyParams(keepParams(r.image_prompt || oldPrompt || b.image_prompt, oldPrompt), SHEET_PARAMS.img);
    if (r.video_prompt) b.video_prompt = applyParams(keepParams(r.video_prompt, oldVidPrompt), SHEET_PARAMS.video);
    hideBusy(); touch(); openBoardSheet(bid);
    toast("提示词已重写");
  } catch (e) { hideBusy(); toast(e.message, true); }
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
  if (diagUrl) {
    urls.push(diagUrl);
    meta.push({ kind: "diagram", name: "调度图", description: "本图是导演定稿的俯视调度图纸：正上方俯视平面图，人偶棋子图标＝人物（标签为人名），「机位」符号＝摄影机位置，虚线箭头＝运动路线。人物位置/走位/机位以本图为准。" });
  }
  const st = S.cur.data.story || {};
  for (const c of st.characters || []) if (c.image_url) {
    urls.push(c.image_url);
    meta.push({ kind: "character", name: c.name, description: `官方设定图（该角色服装/发型/造型的权威基准，画面中角色必须与此图着装一致）：${c.image_prompt || c.description || ""}` });
    // v14 融脸修复：把用户上传的真人脸照片也带上（设定图经过 AI 转绘会丢 likeness，
    // 真人照片才是"本人"的最高基准；后端按 kind=face 注入脸锁定指令）
    for (const u of c.ref_images || []) {
      if (urls.length >= 9) break;
      urls.push(u);
      meta.push({ kind: "face", name: c.name, description: `角色「${c.name}」的真人脸参考照片，画面中该角色的面部必须与照片高度一致` });
    }
  }
  for (const sc of st.scenes || []) if (sc.image_url) {
    urls.push(sc.image_url);
    meta.push({ kind: "scene", name: sc.name, description: sc.image_prompt || sc.description || "" });
  }
  const refs = { reference_urls: urls.slice(0, 9), reference_meta: meta.slice(0, 9) };
  try {
    const r = await api("/api/generate-image", {
      prompt: b.image_prompt, ...refs,
      size: landscape ? "1920x1080" : "1080x1920",
      plot_data: diagPlot || undefined,
    });
    b.images.push(r.image_url);
    if (r.warnings) toast(r.warnings, true);
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
    <label class="field-label">运镜</label>
    <div class="param-grid" style="grid-template-columns:1fr;">
      <label class="param-cell"><select id="vs-move" onchange="onVideoMovePick()">
        <option value="">不指定</option>
        ${MOVE_PARAMS[0].opts.map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join("")}
      </select></label>
    </div>
    <label class="field-label">视频提示词${hasPrompt ? "（可直接改）" : "（还空着，建议先点「✨ AI 重写」）"}</label>
    <textarea id="vs-prompt" rows="5" placeholder="画面怎么动、镜头怎么走、主体做什么">${esc(b.video_prompt || "")}</textarea>
    <div style="display:flex;gap:8px;margin-top:16px;">
      <button class="btn btn-ghost" style="flex:1;" onclick="closeSheet()">取消</button>
      <button class="btn btn-ghost" style="flex:1;" onclick="stashVideoEdit('${b.id}');closeSheet();openBoardSheet('${b.id}')">改提示词</button>
      <button class="btn btn-primary" style="flex:1.4;" onclick="confirmVideo('${b.id}')">🎬 开始生成</button>
    </div>`);
  const sel = $("vs-move");
  if (sel) sel.value = cur ? matchOptValue(MOVE_PARAMS[0], cur) : "";
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
  const st = S.cur.data.story || {};
  const landscape = S.cur.data.orientation === "landscape";
  // 影子镜头（身体影子/倒影/剪影）：参考图不得带角色设定图——H3 参考模式的「主体一致性」
  // 会把设定图里的站立人物当成要描绘的主体，把影子镜头画成活人走入画面（实测翻车）。
  // 生图链路早有同类过滤（shadow_only），视频链路在此对齐；保留分镜图（影子画面的权威基准）+ 调度图 + 场景图
  const isShadowShot = /影子|倒影|剪影|人影/.test(b.video_prompt || "");
  const urls = [b.images[0]];
  const diagUrl = b.trajView || b.topView;
  if (diagUrl && urls.length < 9) urls.push(diagUrl);
  for (const c of st.characters || []) if (c.image_url && !isShadowShot && urls.length < 9) urls.push(c.image_url);
  for (const sc of st.scenes || []) if (sc.image_url && urls.length < 9) urls.push(sc.image_url);
  try {
    // 按所选模型自动适配提交模式：首尾帧工作流以分镜图为首帧+尾帧（起点=终点，画面从图出发再回到图）；
    // 其余为多图参考模式（分镜图+调度图+角色/场景图一起作参考）
    const wf = getVideoWorkflow();
    const isFirstLast = wf === "minimax_h3_lightx2v";
    console.log(`[视频] ${new Date().toLocaleTimeString()} 开始提交（XHR）：workflow=${wf || "默认"}, 首尾帧=${isFirstLast}, 图=${urls.length}张`);
    const r = await xhrApi("/api/submit-video", {
      prompt: b.video_prompt,
      image_urls: urls,
      duration: b.duration,
      resolution: "768p",
      frame_mode: isFirstLast ? "first_last" : "reference",
      first_frame_url: isFirstLast ? b.images[0] : undefined,
      last_frame_url: isFirstLast ? b.images[0] : undefined,
      image_aspect: landscape ? 16 / 9 : 9 / 16,
      workflow_id: wf || undefined,
      plot_data: (b.trajViewPlot || b.topViewPlot) || undefined,
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
      stopPoll(bid);
      toast(`镜头 ${b.shot_number} 视频生成失败`, true);
      touch(); if (S.tab === "boards") renderBoards();
    } else if (S.tab === "boards") {
      const elapsed = (Date.now() - (b.videoStartedAt || Date.now())) / 1000;
      // 活跃轮询下也做超时兜底：平台任务挂死时不再无限"渲染中"
      if (elapsed > 360) {
        b.videoStatus = "failed";
        b.videoError = "渲染超过 6 分钟未完成，已自动停止，请重试";
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
function saveVideoWorkflow() {
  localStorage.setItem("wb_video_workflow", $("set-workflow").value);
  const label = $("set-workflow").selectedOptions[0].textContent;
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
      characters: (st.characters || []).map((c) => ({ name: c.name || "", description: c.description || "" })),
      feedback: desc || undefined,
      base_plot: kind === "trajectory" && b.topViewPlot ? b.topViewPlot : undefined,
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
    toast("图纸已按拖拽结果重绘，坐标已同步");
  } catch (e) {
    toast("重绘失败：" + e.message + "（原坐标未改动，可重试）", true);
  }
}
function closeDirector3D() {
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
  if (!b.videos.length) {
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
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const blob = await resp.blob();
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
