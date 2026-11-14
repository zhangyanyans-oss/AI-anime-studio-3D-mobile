import os
import sys
import json
import re
import io
import time
import base64
import uuid
import shutil
import asyncio
import zipfile
from urllib.parse import quote, urlparse
from pathlib import Path
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException, Query, File, UploadFile, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
from pydantic import BaseModel
from openai import OpenAI
import httpx
from dotenv import load_dotenv
import au_lexicon
import oip as oip_lib  # Open Image Prompts 提示词库：语料检索 + 美术风格卡（只读，失败自动降级）

# ---------- PyInstaller 打包支持 ----------
# exe 双击运行时：切到 exe 所在目录（.env、static/uploads 等持久数据都放 exe 旁边），
# 再把打包内置的前端文件补齐到本地 static（不覆盖已有的 uploads/用户数据）
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    os.chdir(APP_DIR)
    _bundled_static = os.path.join(sys._MEIPASS, "static")
    if os.path.isdir(_bundled_static):
        for _root, _dirs, _files in os.walk(_bundled_static):
            _rel = os.path.relpath(_root, _bundled_static)
            _dst = os.path.join("static", _rel) if _rel != "." else "static"
            os.makedirs(_dst, exist_ok=True)
            for _f in _files:
                _dstf = os.path.join(_dst, _f)
                # uploads/temp 是用户持久数据，绝不覆盖；其余（index.html、
                # style-previews 等）是程序文件，随新版本 exe 覆盖更新
                _parts = {p.lower() for p in os.path.normpath(_dstf).split(os.sep)}
                if _parts & {"uploads", "temp"}:
                    continue
                try:
                    shutil.copy2(os.path.join(_root, _f), _dstf)
                except OSError as _e:
                    # 个别文件被占用/权限不足时不中断启动（下次启动会再尝试覆盖）
                    print(f"⚠ 更新 {_dstf} 失败（跳过）: {_e}")

# 只读当前目录的 .env（exe 旁边 / 项目根目录），不向上级目录搜索，避免误读别处的配置
load_dotenv(os.path.join(os.getcwd(), ".env"))

app = FastAPI(title="AI Anime Studio")

# 确保图片目录存在（uploads 为持久目录，不会被自动清理；temp 仅供临时用途）
if not os.path.exists("static/temp"):
    os.makedirs("static/temp")
if not os.path.exists("static/uploads"):
    os.makedirs("static/uploads")

# ---------- 初始化客户端 ----------
# 注意：key 缺失时用占位符，保证程序能启动（OpenAI 客户端传 None 会直接抛异常）；
# 用户之后在 ⚙️ API 设置里填入真实 key，会重建客户端并写入 .env
deepseek_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY") or "not-configured",
    base_url="https://api.deepseek.com",
)

DOUBAO_API_KEY = os.getenv("ARK_API_KEY")

# ---------- 配音服务（VoxCPM 声音克隆微服务，独立进程用 VoxCPM venv 运行 voice_server.py） ----------
VOICE_SERVER_URL = (os.getenv("VOICE_SERVER_URL") or "http://127.0.0.1:8021").rstrip("/")

# ---------- 分区自定义密钥：请求级生效 ----------
# 分区口令在 ⚙️ 设置页保存了自己的 key（存 user_settings 表）后，
# 该分区的所有生成请求（剧本/分镜/图片/视频）优先用分区 key，主口令仍用全局 key。
import contextvars
_REQ_KEYS: "contextvars.ContextVar[dict]" = contextvars.ContextVar("req_keys", default={})
_REQ_OWNER: "contextvars.ContextVar[str | None]" = contextvars.ContextVar("req_owner", default=None)

_KEY_LABELS = {
    "deepseek": "DeepSeek API Key",
    "ark": "即梦(豆包) API Key",
    "autodl": "AutoDL Token",
}

def _require_own_key(kind: str):
    """严格模式：分区口令必须使用自己保存的密钥，未保存则拒绝调用（主口令/管理员不受限）。
    防止分区用户在未配置自己 key 时消耗管理员的全局额度。"""
    if _REQ_OWNER.get():
        ov = _REQ_KEYS.get() or {}
        db_key = {"deepseek": "deepseek_api_key", "ark": "ark_api_key", "autodl": "autodl_token"}.get(kind)
        if not (db_key and ov.get(db_key)):
            label = _KEY_LABELS.get(kind, kind)
            raise HTTPException(403, f"本分区尚未配置自己的{label}：请联系管理员开通，或在 ⚙️ 设置页填入你自己的{label}后重试。")

def _eff_key(kind: str) -> str:
    """当前请求生效的密钥：分区自定义 > 全局。kind = deepseek | ark | autodl | autodl_i2v"""
    ov = _REQ_KEYS.get() or {}
    if kind == "deepseek":
        return ov.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY", "") or ""
    if kind == "ark":
        return ov.get("ark_api_key") or os.getenv("ARK_API_KEY", "") or ""
    if kind == "autodl":
        return ov.get("autodl_token") or os.getenv("AUTODL_TOKEN", "") or ""
    if kind == "autodl_i2v":
        return ov.get("autodl_i2v_token") or os.getenv("AUTODL_I2V_TOKEN", "") or ""
    return ""

_dsk_clients: dict = {}

def _dsk():
    """当前请求生效的 DeepSeek 客户端：主口令/全局 → 全局客户端；分区自定义 key → 按 key 缓存的独立客户端。
    分区未保存自己的 key 时直接拒绝（严格模式）。"""
    _require_own_key("deepseek")
    k = _eff_key("deepseek")
    try:
        if k and k == getattr(deepseek_client, "api_key", None):
            return deepseek_client
    except NameError:
        pass
    c = _dsk_clients.get(k)
    if c is None:
        c = OpenAI(api_key=k or "not-configured", base_url="https://api.deepseek.com")
        if k:
            _dsk_clients[k] = c
    return c

def _dsk_ok() -> bool:
    """当前请求是否配置了可用的 DeepSeek key（分区 key 或全局 key）。"""
    v = _eff_key("deepseek")
    return bool(v) and v != "not-configured"

def deepseek_friendly_error(e: Exception) -> str:
    """把 DeepSeek 调用异常翻译成可操作的人话（方便在别人机器上排查）。"""
    raw = str(e)
    low = raw.lower()
    if isinstance(e, HTTPException):
        return str(e.detail)  # 密钥权限类错误（分区未配置自己的 key）原样透传给前端
    if not _dsk_ok():
        return ("未配置 DeepSeek API Key，无法生成剧本/头脑风暴。请在程序(exe)同目录创建 .env 文件，"
                "写入一行：DEEPSEEK_API_KEY=sk-你的key；或打开页面右上角 ⚙️ API 设置直接粘贴 Key 并保存。")
    if "401" in raw or "authentication" in low or ("invalid" in low and "key" in low):
        return "DeepSeek API Key 无效或已过期，请检查 .env 里的 DEEPSEEK_API_KEY 是否正确。"
    if "402" in raw or "insufficient balance" in low:
        return "DeepSeek 账户余额不足，请到 platform.deepseek.com 充值后重试。"
    if "429" in raw:
        return "DeepSeek 请求过于频繁，请稍等几秒再试。"
    if isinstance(e, (ConnectionError, TimeoutError)) or "connection" in low or "timeout" in low or "getaddrinfo" in low or "ssl" in low:
        return ("无法连接 DeepSeek 服务器（api.deepseek.com）。请确认这台电脑可以上网；"
                "公司/校园网络或防火墙可能拦截了该域名，可尝试换网络。")
    return f"DeepSeek 调用失败：{raw[:200]}"

# ---------- 模型 JSON 输出的健壮解析 ----------
def extract_json_text(content: str) -> str:
    """剥掉 DeepSeek 偶尔夹带的 markdown 代码块围栏与前后杂文本，只留 JSON 本体。"""
    text = content.strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1).strip()
    else:
        # 没有围栏时，截取第一个 { 到最后一个 } 之间的部分
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    return text

def repair_json_text(text: str) -> str:
    """轻量修复常见 JSON 瑕疵：全角/智能引号、尾逗号、中文标点、字符串内的裸换行。
    重要：只在字符串**外部**替换中文冒号/逗号，避免把提示词正文里的中文标点改掉。"""
    out = []
    in_str = False
    esc = False
    for ch in text:
        if esc:
            out.append(ch); esc = False; continue
        if ch == "\\":
            out.append(ch); esc = True; continue
        if ch == '"':
            in_str = not in_str; out.append(ch); continue
        if in_str:
            if ch in ("\u201c", "\u201d"):          # 字符串里的智能双引号 → 转义
                out.append('\\"')
            elif ord(ch) < 0x20:                     # 裸换行/制表符 → 转义（最常见的解析失败原因）
                out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(ch, "\\u%04x" % ord(ch)))
            else:
                out.append(ch)
        else:
            if ch in ("\u201c", "\u201d"):
                out.append('"')
            elif ch == "\uff1a":
                out.append(":")
            elif ch == "\uff0c":
                out.append(",")
            else:
                out.append(ch)
    t = "".join(out)
    t = re.sub(r",\s*([}\]])", r"\1", t)             # 去尾逗号
    return t

def parse_model_json(content: str, finish_reason: str | None = None) -> Any:
    """尽力解析模型返回的 JSON；失败抛 ValueError，错误信息带具体原因。"""
    if finish_reason == "length":
        raise ValueError("输出长度超出上限被截断")
    text = extract_json_text(content)
    candidates = [text]
    repaired = repair_json_text(text)
    if repaired != text:
        candidates.append(repaired)
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError as e:
            last_err = e
            continue
    # 解析失败时把原始输出留档，便于事后定位（含失败原因/结尾片段）
    try:
        with open("debug_model_json.log", "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%m-%d %H:%M:%S')} 解析失败 finish_reason={finish_reason} "
                    f"len={len(content)} err={last_err}\n{content}\n")
    except Exception:
        pass
    raise ValueError(
        f"无法从模型输出中解析出 JSON（finish_reason={finish_reason}，长度 {len(content)}，"
        f"结尾：{content[-60:]!r}）"
    )

def deepseek_json(response) -> Any:
    """从 DeepSeek 响应对象中解析 JSON，失败抛 ValueError（带原因）。"""
    choice = response.choices[0]
    return parse_model_json(choice.message.content or "", getattr(choice, "finish_reason", None))

DOUBAO_BASE = "https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_MODEL = os.getenv("DOUBAO_MODEL", "doubao-seedream-5-0-flash-260915")
# 豆包 Seedance 视频生成（火山方舟 contents/generations/tasks）：与生图共用 ARK_API_KEY
DOUBAO_VIDEO_MODEL = os.getenv("DOUBAO_VIDEO_MODEL", "doubao-seedance-2-5-260628")

AUTODL_TOKEN = os.getenv("AUTODL_TOKEN")
AUTODL_I2V_TOKEN = os.getenv("AUTODL_I2V_TOKEN", "")  # 可选：首尾帧应用(U02)独立令牌，留空则用 AUTODL_TOKEN
AUTODL_BASE = "https://autodl.art"
AUTODL_WORKFLOW = os.getenv("AUTODL_WORKFLOW_ID", "minimax_h3_zm_u24")

# ---------- 数据模型 ----------
class StoryRequest(BaseModel):
    idea: str
    image_base64: str | None = None
    chat_history: List[Dict[str, str]] | None = None
    style_bible: str | None = None  # 用户在「风格库」预选的风格描述，生成时注入；空串表示直出

class ImageRequest(BaseModel):
    prompt: str
    reference_urls: list[str] | None = None
    reference_meta: list[Dict[str, Any]] | None = None  # 与 reference_urls 一一对应：{kind, name, description}
    context: str | None = None  # 生成上下文（分镜/场景描述），用于筛选相关参考
    size: str = "2048x2048"
    diagram_brief: str | None = None  # 调度图纵深解读（人物近/中/远景层次），由 /api/staging-brief 生成
    plot_data: dict | None = None  # 调度图精准坐标数据（/api/render-diagram 返回的 plot），翻译成自然语言注入生图

class VideoRequest(BaseModel):
    prompt: str
    image_urls: list[str] = []
    ref_meta: List[Dict[str, Any]] | None = None  # 与 image_urls 一一对应：{kind,name}，用于把角色/场景/道具设定图的作用注入提示词
    duration: int = 5
    resolution: str = "768p"
    workflow_id: str | None = None
    frame_mode: str = "reference"  # reference=参考图 / first_frame=首帧 / last_frame=尾帧 / first_last=首尾帧过渡
    first_frame_url: str | None = None  # 首帧模式下作为视频第一帧的图
    last_frame_url: str | None = None   # 尾帧模式下作为视频最后一帧的图
    image_aspect: float | None = None   # 首张参考图的宽/高比（前端计算传入），用于自动选横竖屏档位；未传时后端 Pillow 本地实测兜底
    plot_data: dict | None = None       # 调度图坐标数据（轨迹图优先），翻译成自然语言运动轨迹指令注入视频提示词
    env_context: str | None = None      # 场景环境锁定（前端从分镜生图提示词抽取的地点/风格/光线段落）：视频提示词往往只写动作，不写环境，模型会自由脑补场景（实测跑成室内）
    video_engine: str = "autodl"        # autodl=AutoDL ComfyUI 工作流（默认） / doubao=豆包 Seedance（火山方舟 Ark）
    video_model: str | None = None      # doubao 引擎的 Seedance 模型 ID（如 doubao-seedance-2-5-260628），空则用全局默认
    doubao_resolution: str | None = None  # doubao 引擎输出分辨率：480p（≈0.7元/秒）/ 720p（≈1.5元/秒）/ 1080p（10bit，≈1.7元/秒）
    page_origin: str | None = None      # 前端页面地址（location.origin）：经代理转发时后端请求头的 host 是内网地址，不可信
    ref_video_urls: list[str] = []      # 垫参考视频（Seedance reference_video）：引导运镜/构图/节奏，不入成片
    share_code: str | None = None       # 页面链接里的 sharecode：把本地上传的垫视频拼成公网直链给 Ark/AutoDL 拉取

class BrainstormRequest(BaseModel):
    step: int
    messages: List[Dict[str, Any]]
    user_input: str
    image_base64: str | None = None
    image_base64_list: List[str] | None = None   # 多图附件（与 image_base64 合并，image_base64 排最前）

class RegeneratePromptRequest(BaseModel):
    type: str  # character / scene / storyboard
    name: str | None = None
    description: str | None = None
    dialogue: str | None = None            # 分镜用
    duration: int = 3                      # 分镜用
    style_bible: str | None = None         # 全片画风圣经
    synopsis: str | None = None            # 剧情梗概
    characters: List[Dict[str, Any]] | None = None  # [{name, image_prompt, outfits:[{name, image_prompt}]}] 角色外观锚点（含多套服装）
    scenes: List[Dict[str, Any]] | None = None      # [{name, image_prompt, description}] 场景设定（解析【场景】引用用）
    props: List[Dict[str, Any]] | None = None       # [{name, image_prompt, description}] 道具设定
    old_prompt: str | None = None          # 旧提示词（在其基础上重写）
    old_video_prompt: str | None = None    # 旧视频提示词（用户手改过的版本；视频提示词在它基础上重写，不从零重来）
    feedback: str | None = None            # 用户的新要求（最高优先级）
    furniture: List[Dict[str, Any]] | None = None  # 场景用：家具布置 [{label, x, y, w, h, facing}]（0-1 归一化，上=空间深处）

# ---------- 0.9 Open Image Prompts 提示词库：检索参考 + 美术风格卡 ----------
def _oip_reference_block(query: str, *, limit: int = 3) -> str:
    """检索 2.3 万条真实提示词语料，格式化为可注入 LLM 的参考块。
    任何失败（库缺失/检索异常）都返回空串，绝不拖垮主生成流程。"""
    query = str(query or "").strip()
    if not query:
        return ""
    try:
        brief = oip_lib.reference_brief(query, limit=limit)
    except Exception as exc:
        print(f"[oip] 参考检索降级：{exc}")
        return ""
    if not brief:
        return ""
    lines = []
    for item in brief["references"][:limit]:
        zh = item.get("prompt_zh") or item.get("prompt_text") or ""
        if zh:
            lines.append(f"· （{item.get('tool') or '未知模型'}）{zh}")
    if not lines:
        return ""
    return (
        "\n【提示词库参考】以下是提示词库里与本题相关的真实优秀提示词节选（来源可追溯）。"
        "只借鉴它们的镜头感、构图、光线与质感写法，禁止照抄其中的具体人物/地点/剧情设定：\n"
        + "\n".join(lines)
    )


def _oip_style_card_block(*texts: str | None) -> str:
    """按画风圣经/剧情挑选一张美术方向风格卡（img-gen-taste），注入美术指导。"""
    try:
        name = oip_lib.pick_card(*texts)
        excerpt = oip_lib.card_excerpt(name)
    except Exception as exc:
        print(f"[oip] 风格卡降级：{exc}")
        return ""
    if not excerpt:
        return ""
    return f"\n【美术方向参考·{name}】以下是该题材方向的美术要点卡，撰写画面描述时参考其构图/光线/质感要点：\n{excerpt}"

# ---------- 1. 头脑风暴接口 (完整版) ----------
BRAINSTORM_SYSTEM_PROMPT = """你是一个专业的动画编剧与头脑风暴助手。
你的任务是通过一步一步的提问，引导用户完善一个短篇动画的故事构思。
你需要用对话的形式，一次只问1-2个问题，不要一次性抛出所有问题。

引导流程（请循序渐进）：
1. 第一步：询问用户脑海里最核心的画面或氛围是什么（比如：一个场景、一种情绪）。
2. 第二步：引导确立主角（是谁？长什么样？有什么特征？）。
3. 第三步：挖掘冲突/目的（主角想要什么？遇到了什么阻碍？）。
4. 第四步：确立结局（高潮是什么？最后的画面定格在哪？）。

规则：
- 如果用户发了图片，请仔细观察图片内容，并基于图片中的元素（如角色长相、场景风格、色彩）进行提问和引导。
- 如果用户提供的要素还不够（比如没定下结局），请继续用简短、有启发性的问题引导。
- 当用户提供的核心要素（画面、主角、冲突、结局）基本齐全，或者用户明确表示“够了/帮我写吧”时，你需要在回复的最后加上特殊标记 `[READY_TO_GENERATE]`，并用一段连贯的文字总结出完整的故事。
"""

@app.post("/api/brainstorm")
async def brainstorm(req: BrainstormRequest):
    if not _dsk_ok():
        raise HTTPException(400, "未配置 DeepSeek API Key。请在程序(exe)同目录创建 .env 文件写入 DEEPSEEK_API_KEY=sk-你的key，"
                                 "或打开页面右上角 ⚙️ API 设置粘贴 Key 并保存后重试。")
    messages = [{"role": "system", "content": BRAINSTORM_SYSTEM_PROMPT}]
    for msg in req.messages:
        messages.append(msg)
    
    all_images = list(req.image_base64_list or [])
    if req.image_base64:
        all_images.insert(0, req.image_base64)   # 兼容旧前端/粘贴单图路径，排最前
    if req.user_input or all_images:
        content = []
        if req.user_input:
            content.append({"type": "text", "text": req.user_input})
        for b64 in all_images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })
        
        if len(content) == 1 and content[0]["type"] == "text":
            messages.append({"role": "user", "content": req.user_input})
        else:
            messages.append({"role": "user", "content": content})

    try:
        response = _dsk().chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            max_tokens=1024,
            temperature=0.7,
        )
        reply = response.choices[0].message.content
        
        if "[READY_TO_GENERATE]" in reply:
            clean_reply = reply.replace("[READY_TO_GENERATE]", "").strip()
            return {"status": "ready", "reply": clean_reply, "story_idea": clean_reply}
        else:
            return {"status": "asking", "reply": reply}
    except Exception as e:
        raise HTTPException(e.status_code if isinstance(e, HTTPException) else 500, deepseek_friendly_error(e))

# ---------- 2. 生成剧本、角色、分镜 (工业级提示词模板 v2) ----------
STORY_SYSTEM_PROMPT = """你是一位资深的 AI 动画短片导演兼提示词工程师，精通为文生图模型（豆包 seedream）和图生视频模型（MiniMax-H3）撰写工业级提示词。
用户会提供一个故事想法或头脑风暴对话记录，可能附带一张参考图（若附带，请沿用其画风、色彩与角色形象，并提炼进 style_bible）。

【输出格式】严格输出以下 JSON（键名不得改动，不要 markdown 代码块，不要任何 JSON 之外的文字）：
{
  "style_bible": "全片统一画风圣经：一段 50-80 字的固定画风描述（媒介类型+画风流派+主色调+光影基调+画质关键词）。后续每条分镜 image_prompt 都必须以它开头逐字复用，这是全片画风一致性的核心。",
  "hook": "一句话钩子，用于吸引观众",
  "synopsis": "剧情梗概，200字以内",
  "characters": [
    {"name": "角色名",
     "description": "角色设定：年龄、身份、性格，一句话",
     "image_prompt": "角色设定图提示词，固定写法：style_bible 原文 + 「单人全身立绘、完整入画（头顶到脚踝全部可见）、正面或四分之三侧面、自然站姿目视镜头、简洁纯色浅背景、柔和均匀打光」+ 逐一写死可复用的外观锚点：发型发色、瞳色、五官特征、体型年龄、服装款式与颜色、随身标志物。60-120 字。此图将作为该角色在所有分镜中的参考图，因此禁止出现场景、动作戏、其他人物。"}
  ],
  "scenes": [
    {"name": "场景名",
     "description": "场景的功能与氛围，一句话",
     "image_prompt": "场景设定图提示词：style_bible 原文 + 大远景空镜（画面中无人物）+ 空间布局与 2-3 个标志性陈设 + 时间天气 + 光线方向。60-120 字。此图将作为场景参考图。"}
  ],
  "props": [
    {"name": "道具名",
     "description": "道具在剧情中的作用与意义，一句话",
     "image_prompt": "道具设定图提示词，固定写法：style_bible 原文 + 「单件道具特写陈列图，画面中无人物，浅色纯净背景，柔和均匀布光，可附比例参照」+ 材质、颜色、形态结构、新旧磨损细节、标志性特征。40-80 字。此图将作为该道具在分镜中的参考图，禁止出现场景环境或无关物体堆砌。"}
  ],
  "storyboards": [
    {"shot_number": 1,
     "description": "本镜头的剧情作用，一句话",
     "dialogue": "该镜头的台词或旁白，没有则输出空字符串",
     "duration": 3,
     "image_prompt": "分镜图提示词（文生图用），必须按结构化模板撰写：单行输出，小节用【】标题依次排列，每个小节必须写，禁止漏节。【画面内容】在(地点)，(谁)正在(做什么)，画面正在发生(具体事件)，角色逐字复用其 image_prompt 的外貌服装锚点，写明表情与动作瞬间；【镜头细节】焦段(如35mm/85mm)、构图(主体居中/对称/前景遮挡)、景别(远/全/中/近/特)、机位(固定/低角度/俯拍/仰拍)；【美术风格】style_bible 原文；【光源】光源类型(顶光/逆光/侧光/蝴蝶光/伦勃朗光/硬光/柔光/自然光等)及光如何影响场景；【氛围】观众的情绪感受(开心/孤独/紧张/充满希望等)；【影调】冷暖色调、饱和度、明暗关系；【约束】正向表达：画面无文字水印分格、动作自然符合物理规律、本镜头需锁定的要点。上下文不足以确定的内容写【待补：说明】占位留给创作者修改，禁止编造关键设定。",
     "video_prompt": "图生视频提示词，单行输出：【动作时序】首先…然后…最后…(总时长=duration秒)；【镜头运动】推/拉/摇/移/环绕/跟随/固定机位；【声音】仅当剧情有明确声音需求时注明，没有就省略此节。禁止引入分镜图中不存在的新人物/物体/场景，禁止“突然”“闪现”“切换到”等跳变词，不复述静态外观。"}
  ]
}

【导演规则】
1. storyboards 数量 8-16 个，按剧情节奏编排；第一个镜头必须能在 3 秒内抓住眼球（悬念/冲击画面/反差）。
2. 景别要有剪辑感：相邻镜头避免连续使用相同景别，重点情绪用特写强调，转场处可用大远景。
3. 同一场景内的相邻镜头，时间、天气、光线必须连续一致；跨场景才允许变化。
4. duration 取 2 到 15 的整数：静态对话 2-4 秒，常规动作镜头 3-8 秒，重大场面（大战/蒙太奇/长情绪戏）可到 10-15 秒；按内容节奏自然分配，不要全部拉满。
5. dialogue 短而口语化，单镜头不超过两句；没有台词就留空字符串，不要硬凑。
6. 所有分镜 image_prompt 中同一角色的外观描述，必须与该角色 image_prompt 逐字相同——这是参考图机制下保持角色一致性的关键。
7. 全片画风、色调必须统一，由 style_bible 锁定；不得中途变换风格。
8. 素材引用（参考图/音频/视频）与全局设定（资产库）是创作者的输入，不由你生成；不要虚构素材，相关需求写进【约束】或【待补】。
9. 每条分镜 image_prompt 必须包含全部【】小节且单行输出；不确定的小节写【待补：说明】，让创作者修改后重写。
10. 输出必须是可直接 json.loads 的合法 JSON：双引号、无注释、无尾逗号，字符串内不要出现真实换行。
11. props 数量 0-6 个：只挑对剧情有实际作用的道具（信物、钥匙、武器、信件等能推动剧情或值得特写的），普通背景陈设不要列入；没有就输出空数组。"""

@app.post("/api/generate-story")
async def generate_story(req: StoryRequest):
    if not _dsk_ok():
        raise HTTPException(400, "未配置 DeepSeek API Key。请在程序(exe)同目录创建 .env 文件写入 DEEPSEEK_API_KEY=sk-你的key，"
                                 "或打开页面右上角 ⚙️ API 设置粘贴 Key 并保存后重试。")
    try:
        user_content = []
        
        prompt_text = ""
        if req.chat_history:
            prompt_text += "以下是用户的头脑风暴对话记录，请基于此生成剧本：\n"
            for msg in req.chat_history:
                role = "用户" if msg["role"] == "user" else "AI助手"
                prompt_text += f"{role}: {msg['content']}\n"
        elif req.idea:
            prompt_text += f"故事想法：{req.idea}\n"

        # 用户预选风格（风格库「用这套」暂存的描述）：最高优先级注入，剧本与全部提示词按此风格生成
        if req.style_bible is not None:
            if req.style_bible.strip():
                prompt_text += f"\n【画风圣经（用户已锁定，必须严格执行）】{req.style_bible.strip()}\n剧本的 style_bible 字段原样输出该描述，所有分镜 image_prompt 必须符合该画风。\n"
            else:
                prompt_text += "\n【画风：用户选择直出，不要施加任何风格倾向，剧本的 style_bible 字段输出空字符串。】\n"

        # 注入提示词库参考：语料检索（few-shot 参考）+ 风格卡（美术方向）。失败自动降级为空串。
        _idea_text = req.idea or " ".join(
            str((m or {}).get("content") or "") for m in (req.chat_history or []) if isinstance(m, dict)
        )
        prompt_text += _oip_reference_block(_idea_text, limit=3)
        prompt_text += _oip_style_card_block(_idea_text)

        if prompt_text:
            user_content.append({"type": "text", "text": prompt_text})
        if req.image_base64:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{req.image_base64}"}
            })
            
        if len(user_content) == 1 and user_content[0]["type"] == "text":
            final_content = user_content[0]["text"]
        else:
            final_content = user_content

        last_err = "未知错误"
        for attempt in range(2):  # 解析失败自动重试 1 次
            response = _dsk().chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": STORY_SYSTEM_PROMPT},
                    {"role": "user", "content": final_content},
                ],
                response_format={"type": "json_object"},
                max_tokens=16384,
            )
            choice = response.choices[0]
            content = choice.message.content or ""
            if not content.strip():
                last_err = "DeepSeek 返回空内容"
                print(f"[generate-story] 第 {attempt + 1} 次输出为空，{'重试' if attempt == 0 else '放弃'}")
                continue
            try:
                return parse_model_json(content, getattr(choice, "finish_reason", None))
            except ValueError as e:
                last_err = str(e)
                print(f"[generate-story] 第 {attempt + 1} 次输出解析失败：{last_err}")
        raise HTTPException(
            500,
            f"DeepSeek 连续 2 次返回的内容都无法解析成 JSON（{last_err}）。"
            "请再试一次；若反复出现，多半是剧本太长导致输出被截断，可缩短故事想法后重试。"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(e.status_code if isinstance(e, HTTPException) else 500, deepseek_friendly_error(e))

# ---------- 2.5 单条提示词重生成（人物 / 场景 / 分镜） ----------
# 界面下拉里用户手选的摄影参数（与前端 static/app.js 的 SHOT_PARAMS / MOVE_PARAMS 保持一致）。
# 这些是硬约束：模型改写正文时很容易把它们揉成散文（"景别：远景"→"景别近景特写"），
# 所以先提取出来当硬约束喂给模型，再从返回文本里清掉变体、重新拼成标准参数块。
SHOT_PARAM_OPTS = {
    "景别": ["大远景", "远景", "全景", "中景", "中近景", "近景", "特写", "大特写"],
    "景深": ["超浅景深", "浅景深", "中等景深", "深景深"],
    "机位": ["平视", "低角度仰拍", "高角度俯拍", "鸟瞰", "荷兰角", "过肩", "主观视角"],
    # 焦段：长名与「35mm」这类短写都列上——模型改写时两种写法都可能出现
    "焦段": ["14mm 超广角", "24mm 广角", "35mm 人文广角", "35mm", "50mm 标准", "50mm",
             "85mm 中长焦", "85mm", "135mm 长焦", "135mm"],
    "构图": ["三分法", "中心对称", "对角线", "框架式", "引导线", "前景遮挡", "大量留白"],
    "光线": ["顺光", "侧光", "逆光轮廓光", "顶光", "底光", "伦勃朗光", "柔光散射", "硬光直射"],
    "运镜": ["固定镜头", "缓缓推近", "缓缓拉远", "横移", "摇镜", "跟随", "环绕", "升降"],
}
# 这些组在提示词里天然是短促的标签式表述，可以容忍「无冒号」写法一并清理；
# 光线/运镜则只清带冒号的标准写法，避免误伤「侧光从窗缝斜射下来」这类正文描述。
_TIGHT_PARAM_LABELS = ("景别", "景深", "机位", "焦段", "构图")
_SEP = "，,；;、。\n】"  # 含「】」，紧贴小节标题的参数写法（【镜头细节】景别：中景）也要能识别


def _param_opts_alt(label: str) -> str:
    opts = sorted(SHOT_PARAM_OPTS[label], key=len, reverse=True)  # 长词优先，避免「近景」吃掉「中近景」
    return "|".join(re.escape(o) for o in opts)


def _clean_param_text(text: str) -> str:
    t = re.sub(r"([，,；;、])\s*[，,；;、]+", r"\1", text or "")
    t = re.sub(r"[，,；;、]+\s*(?=[。])", "", t)      # 「，。」→「。」
    t = re.sub(r"([。])\s*[，,；;、]+", r"\1", t)      # 「。，」→「。」
    t = re.sub(r"】\s*[，,；;、]+", "】", t)           # 「【镜头细节】，机位…」→ 去掉小节标题后的孤立逗号
    t = re.sub(r"^[，,；;、。\s]+", "", t)
    t = re.sub(r"[，,；;、。\s]+$", "", t)
    return t.strip()


def extract_shot_params(text: str) -> Dict[str, str]:
    """从提示词正文里读出用户在下拉中选定的参数（标准写法「标签：值」）。"""
    out: Dict[str, str] = {}
    src = text or ""
    for label in SHOT_PARAM_OPTS:
        m = re.search(rf"{re.escape(label)}\s*[:：]\s*([^{_SEP}]+)", src)
        if m:
            v = m.group(1).strip()
            if v:
                out[label] = v
    return out


def strip_shot_params(text: str, labels) -> str:
    """清掉文本里指定各组的参数表述（含模型改写成的无冒号变体）。"""
    t = text or ""
    for label in labels:
        lb = re.escape(label)
        # ① 标准写法「标签：值」（值可能被模型换成不存在的词，一并清）
        t = re.sub(rf"(^|[{_SEP}])\s*{lb}\s*[:：][^{_SEP}]*", r"\1", t)
        # ② 无冒号但紧跟合法选项值：「景别近景特写」「景别为远景」
        t = re.sub(rf"(^|[{_SEP}])\s*{lb}\s*(?:为|是)?\s*(?:{_param_opts_alt(label)})[^{_SEP}]*", r"\1", t)
        # ③ 标签式变体：形如「机位略低仰拍」「构图主体居中带前景树根遮挡」这种以标签开头、
        #    后面不再出现分隔符的短陈述（模型最爱在【约束】里重复一遍）。只对景别/景深/机位/焦段/构图生效，
        #    光线/运镜不参与，避免误伤「侧光从窗缝斜射下来」这类正文描写。
        if label in _TIGHT_PARAM_LABELS:
            budget = 12 if label in ("景别", "机位", "构图") else 8
            t = re.sub(
                rf"(^|[{_SEP}])\s*{lb}\s*(?:为|是)?\s*[^{_SEP}]{{1,{budget}}}(?=[{_SEP}]|$)",
                r"\1", t,
            )
    return _clean_param_text(t)


def _param_block(params: Dict[str, str], labels) -> str:
    """按固定顺序拼出标准参数块「景别：远景，机位：高角度仰拍」。"""
    pairs = [f"{lb}：{params[lb]}" for lb in SHOT_PARAM_OPTS if lb in labels and lb in params]
    return "，".join(pairs)


REGEN_SYSTEM_PROMPT = """你是 AI 动画工作台的提示词重写引擎，负责把一条不够精细的生图提示词重写成工业级提示词。

【画风圣经】用户会提供全片统一的画风描述（style_bible），重写后的 image_prompt 和 video_prompt 都必须以它的原文开头，保证全片画风一致。

【四类提示词的固定规范】
1. 角色（character）：角色设定图。style_bible 原文 + 「单人全身立绘、完整入画（头顶到脚踝全部可见）、正面或四分之三侧面、自然站姿目视镜头、简洁纯色浅背景、柔和均匀打光」+ 逐一写死可复用的外观锚点：发型发色、瞳色、五官特征、体型年龄、服装款式与颜色、随身标志物。60-120 字。禁止出现场景、动作戏、其他人物。
2. 场景（scene）：场景设定空镜。style_bible 原文 + 大远景空镜（画面中无人物）+ 空间布局与 2-3 个标志性陈设 + 时间天气 + 光线方向。60-120 字。严禁出现任何人物或人影。
3. 道具（prop）：道具设定图。style_bible 原文 + 「单件道具特写陈列图，画面中无人物，浅色纯净背景，柔和均匀布光，可附比例参照」+ 材质、颜色、形态结构、新旧磨损细节、标志性特征。40-80 字。禁止出现场景环境或无关物体堆砌。
4. 分镜（storyboard）：结构化模板，单行输出，小节用【】标题依次排列，每个小节必须写：【画面内容】在(地点)，(谁)正在(做什么)，画面正在发生(具体事件)，出现的角色逐字复用其角色锚点的外貌描述（发型/五官/体型等；服装按「中括号引用解析」执行），写明表情与动作瞬间；【镜头细节】焦段(如35mm/85mm)、构图(主体居中/对称/前景遮挡)、景别(远/全/中/近/特)、机位(固定/低角度/俯拍/仰拍)；【美术风格】style_bible 原文；【光源】光源类型(顶光/逆光/侧光/蝴蝶光/伦勃朗光/硬光/柔光/自然光等)及光如何影响场景；【氛围】观众的情绪感受；【影调】冷暖色调、饱和度、明暗关系；【约束】正向表达：无文字水印分格、动作自然符合物理规律、本镜头需锁定的要点。上下文不足写【待补：说明】，禁止编造。

【硬性禁止】画面出现任何文字、字幕、水印、边框、分格、多格拼图；禁止抽象词（如“幸福”“回忆”“命运”）；禁止把台词写进画面。

【video_prompt 规范】仅分镜类型需要输出，单行输出，以画风圣经原文开头，后接：【动作时序】首先…然后…最后…(总时长=该镜头持续秒数)；【镜头运动】推/拉/摇/移/环绕/跟随/固定机位；【声音】仅当有明确声音需求时注明，没有就省略。画面起点即该分镜图。硬性禁止：引入分镜图中不存在的新人物/物体/场景；禁止“突然”“闪现”“切换到”等跳变词；不要复述静态外观。

【用户修改要求】旧提示词是用户当前工作框内的内容，是最高优先级的内容来源：其中用户亲笔写下的具体细节（人物年龄、体型、五官、服装、随身物、动作、表情、地点、道具、光源、氛围、风格等）必须逐字保留，严禁替换、省略、泛化或"换种说法"。重写只允许做三件事：①把内容套进对应类型的结构化模板并分节；②补全模板要求但用户没写的维度（不确定的写【待补：说明】，禁止编造）；③语言规范化。若角色锚点与用户描述冲突，一律以用户描述为准；若旧提示词很短且明显是一句修改指令（如"换成雨夜"）而非完整提示词，将其整体视为用户的新要求，结合上下文重写出完整的结构化提示词。
【中括号资产引用必须原样保留】旧提示词（及设定描述、修改要求）里用【名字】或[名字]中括号括起来的内容，若是角色/场景/道具/服装的名字（如【木兰·铠甲】【村庄】【木剑】），是导演对资产设定图的显式引用指令：重写后的提示词必须原样保留每一个这样的引用——保持中括号形式与名字逐字不变，可自然融入对应描述句中，严禁删除、改名、丢掉括号或换成别的说法。注意【画面内容】【美术风格】【镜头细节】等固定段落标签不是资产引用，不受本条约束。
【中括号服装引用＝换装指令，必须按那套服装写】中括号引用若形如【角色·服装名】或【服装名】（如【花木兰·民女打扮】【民女打扮】），是导演指定"该角色本镜头穿这套衣服"的换装指令，不是随便写的标签：
① 画面里的服装款式、颜色、材质、发式头饰、鞋履、随身小物，必须按用户消息「中括号引用解析结果」里那套服装的设定原文去写（那是该套服装的权威外观）；
② 严禁沿用角色锚点里的默认服装（不能因为她本体锚点写着铠甲/战袍，就把民女打扮写成铠甲），严禁两套服装元素混穿；
③ 面部、发型长度、瞳色、体型、年龄仍按角色锚点，与服装不冲突；
④ 该组中括号本身要逐字保留在提示词里（前端靠它挂该套服装的参考图）；
⑤ 若引用解析结果标注"没有对应资产"，则按用户写的字面意思写，并在【约束】里注明服装以文字为准。
【描述框与提示词框冲突裁决】设定描述和旧提示词都是用户修改框里的最新手改内容；两者在基础设定（年龄/性别/身份/发型/体型/服装）上冲突时，以设定描述为准，把旧提示词里的过时版本改写成描述版本（例：描述写"26岁女性"而旧提示词写"16岁少女"，必须重写为26岁女性），这不算违背逐字保留原则——逐字保留只适用于与最新设定不冲突的细节；旧提示词里有而描述没提的细节（动作、构图、随身物等）仍然逐字保留。
【画风圣经优先级】画风圣经是硬约束，优先级高于旧提示词里的画风描述：重写后的 image_prompt 与 video_prompt 都必须以画风圣经原文开头；若旧提示词（含视频提示词）中的风格措辞与画风圣经冲突，以圣经为准更新（其余用户细节仍按上一条逐字保留）。用户没提供画风圣经时才按旧提示词原有风格写。
【追加的指令要落实】用户在框内追加或修改的指令式短句（如"空镜头""不要人物""去掉XX""换成雨夜""加一面镜子"）是用户新要求：必须把它落实为规范中对应位置的具体描述（例如"空镜头"落实为"大远景空镜（画面中无人物）"，并保证全文没有任何人物痕迹），而不是把原句原样缀在提示词结尾；落实完成后提示词里不得残留指令原文。判断依据：该短句与提示词其余部分风格割裂、位置突兀（多为结尾），或描述的是"要改什么"而不是"画面里有什么"。

【用户指定摄影参数是硬约束】用户消息里若给出「已指定的摄影参数」，这些值优先级最高：在【镜头细节】/【光源】等小节里必须**逐字使用这些词**（指定"景别=远景"就写"景别远景"，指定"机位=高角度仰拍"就写"机位高角度仰拍"），严禁换成近义词、近似的自造说法（不得写成"近景特写""略低仰拍"）、严禁拆分或合并；提示词其他位置也不得出现与这些参数矛盾的景别/景深/机位/焦段/构图/光线描述。指定的参数不出现在旧提示词里，你仍要主动把它们写进对应小节。
【景别决定构图，必须落到画面描述里】景别不是标签，是要真画出来的构图。重写时【画面内容】**开头第一句先交代景别与主体在画面中的比例**（例："大远景，人物只是废墟中的一个小点"），再按景别分配主体与环境的篇幅：景别为**大远景**时，画面以大环境/远景全貌为主体，人物缩得很小（约画面高度不到10%，可能只是剪影或背影），环境尺度、天空、地貌、建筑体量要写足，**严禁描写面部五官/表情/服装细节**（写了就会把人物画大），人物只需交代位置与姿态；**远景**时人物较小（约画面高度20%），四周必须留出大量环境，同样不写五官细节；**全景**才是全身完整入画，从此档起才可以写表情与服装。若焦段与景别冲突（如远景配85mm以上长焦会压缩空间、把人物拉近显大），以景别为准仍要写出人物在画面中的小比例，仅在【镜头细节】保留指定焦段数值。
【待补占位规则】若旧提示词含有【待补：xx】，而用户的新要求没有给出对应信息，则原样保留该【待补：xx】（可以写得更具体），不要替用户拍板编造；用户在新要求里明确了的内容，必须替换掉对应的【待补】。
【素材与资产】素材引用（参考图/音频/视频）与全局设定（资产库）是创作者的输入，不由你生成、不要虚构；相关需求写进【约束】或【待补】。

【输出】严格输出 JSON：{"image_prompt": "...", "video_prompt": "..."}。非分镜类型 video_prompt 输出空字符串。不要 markdown 代码块，不要任何 JSON 之外的文字。"""

# ---------- 中括号资产引用：提取与保真（与前端 parseBracketRefs 同一套语义） ----------
_BRACKET_RE = re.compile(r"[【\[]([^\]】]{1,60})[】\]]")
# 描述性短语结尾词：AI 重写常输出【角色·落座几何，严格按此描写】这类"角色+小标题"，
# 后半截不是资产名，不该拿去资产库查、也不该报未匹配
_DESC_SUFFIX = ("几何", "描写", "说明", "细节", "要求", "时序", "变化", "过程", "关系", "逻辑", "规范")

# 固定段落标签（不是资产引用），与前端 BRACKET_IGNORE_TAGS 对齐并补充模板标签
_BRACKET_IGNORE_TAGS = {
    "画面内容", "画面", "景别", "运镜", "镜头", "机位", "台词", "对白", "旁白",
    "美术风格", "光源", "影调", "氛围", "时长", "转场", "音效", "音乐",
    "镜头细节", "动作时序", "镜头运动", "声音", "约束", "镜头细节",
}

# 镜头参数 / 画面约束类括号：这些是给模型的技术指令，不是资产名，不该拿去资产库查找，
# 也不该报"未匹配"。典型写法：【景别：中景】【镜头运动：缓慢推近】【光线：暖黄侧光】
# 【禁止出现现代元素】【人物不得看向镜头】——含冒号、或以参数名开头、或带指令动词。
# 注意：这里只放"不可能是角色/道具/场景名"的词，别把"人物""场景""道具"这类通用词放进来。
_BRACKET_PARAM_LABELS = {
    "画面内容", "画面", "画面描述", "画面构成", "景别", "取景", "运镜", "镜头", "镜头运动",
    "镜头语言", "镜头细节", "镜头角度", "机位", "机位位置", "视角", "摄像机", "摄影机",
    "构图", "画幅", "画质", "比例", "分辨率", "尺寸",
    "台词", "对白", "旁白", "字幕", "音效", "音乐", "声音", "时长", "转场", "节奏",
    "美术风格", "风格", "画风", "画风参考", "光源", "光线", "光影", "影调", "色调",
    "色彩", "氛围", "情绪", "表情", "动作", "动作时序", "人物数量", "人数",
    "约束", "限制", "要求", "注意", "注意事项", "备注", "提示", "禁止", "避免", "参考",
}
# 指令/约束动词：括号内容带这些词基本是拍法要求，不是资产名
_BRACKET_RULE_WORDS = (
    "禁止", "严禁", "不得", "不能", "不可", "不要", "避免", "切勿", "必须", "务必",
    "需要", "应该", "确保", "保持", "尽量", "只有", "仅有", "只能", "只需", "只要",
    "不出现",
)


def _is_param_bracket(text: str) -> bool:
    """判断一段中括号内容是否是"镜头参数/画面约束"（不是资产引用）。

    命中任一条件即视为约束，整段跳过、不查找、不提示：
      1. 固定段落标签 / 参数名标签
      2. 含中英文冒号的"标签：取值"结构（资产名不会带冒号）
      3. 含禁止/必须/不得这类指令动词
      4. 以参数名开头或结尾（如"景别近景""机位正前方"）
      5. 【待补：xx】占位符
    """
    s = (text or "").strip()
    if not s:
        return True
    if s in _BRACKET_IGNORE_TAGS or s in _BRACKET_PARAM_LABELS:
        return True
    if s.startswith("待补"):
        return True
    if "：" in s or ":" in s:
        return True
    if any(w in s for w in _BRACKET_RULE_WORDS):
        return True
    # "构图 人物居左三分线"：参数名打头且紧跟分隔符 → 约束（不限长度）
    # "人物居左三分线构图"：以参数名收尾且整段不长 → 约束
    # 注意不能用"参数名打头且整段短"这条：道具名"参考书"会被误杀
    for lbl in _BRACKET_PARAM_LABELS:
        if len(lbl) < 2 or s == lbl:
            continue
        if s.startswith(lbl) and len(s) > len(lbl) and s[len(lbl)] in " ：:·•|/-、,，.。;；":
            return True
        if s.endswith(lbl) and len(s) <= len(lbl) + 6:
            return True
    return False


def _looks_descriptive(part: str) -> bool:
    """【花木兰·落座几何，严格按此描写】这类括号：`·` 后面跟的是这一段的"小标题/写法要求"，
    不是资产名。AI 重写常这么写，若拿去资产库查就会误报"未匹配"。按结尾词判定。"""
    s = (part or "").strip()
    if not s:
        return True
    return any(s.endswith(suf) for suf in _DESC_SUFFIX)


def _looks_like_shot_param(text: str) -> bool:
    """宽松判定：只在"这个 part 已经确认匹配不到任何资产"之后才用。
    资产库查不到、又带镜头参数名/指令词的，基本就是【景别近景】这类约束碎块，
    静默忽略比报"未匹配"友好（此处放宽不会误伤真实资产，因为资产已匹配失败）。"""
    s = (text or "").strip()
    if not s:
        return True
    if _is_param_bracket(s) or _looks_descriptive(s):
        return True
    if any(lbl in s for lbl in _BRACKET_PARAM_LABELS):
        return True
    return False


def extract_bracket_asset_names(*texts) -> list:
    """从多段文本里提取【名字】/[名字] 括起来的资产名。
    段落标签、【景别：中景】这类镜头参数/画面约束、待补占位都不算引用。"""
    out, seen = [], set()
    for text in texts:
        if not text:
            continue
        for m in _BRACKET_RE.findall(str(text)):
            if _is_param_bracket(m):
                continue
            for part in re.split(r"[·•|/、,，;；\s]+", m):
                t = part.strip()
                if not t or t in _BRACKET_IGNORE_TAGS or t in seen or t.startswith("待补"):
                    continue
                seen.add(t)
                out.append(t)
    return out


def _bracket_groups(*texts) -> list:
    """提取中括号引用并按组拆 part（【花木兰·民女打扮】→ ["花木兰","民女打扮"]）。
    与前端 parseBracketGroups 同一套语义：段落标签、镜头参数/画面约束、待补占位都不算引用。"""
    groups, seen = [], set()
    for text in texts:
        if not text:
            continue
        for m in _BRACKET_RE.findall(str(text)):
            if _is_param_bracket(m):   # 整段是镜头约束（含冒号/指令词/参数名）→ 整段跳过
                continue
            parts = [p.strip() for p in re.split(r"[·•|/、,，;；\s]+", m)]
            parts = [p for p in parts
                     if p and p not in _BRACKET_IGNORE_TAGS and not p.startswith("待补")
                     and not _is_param_bracket(p)]
            key = "|".join(parts)
            if parts and key not in seen:
                seen.add(key)
                groups.append((m.strip(), parts))
    return groups


def _name_hit(part: str, name: str) -> bool:
    """引用名与资产名的模糊匹配：相等，或长词包含短词（双向），≥2 字才算。
    防「民女打扮」vs「民女装」这类书写细差漏判。"""
    if not part or not name:
        return False
    return (part == name
            or (len(part) >= 2 and part in name)
            or (len(name) >= 2 and name in part))


def resolve_bracket_groups(texts, characters=None, scenes=None, props=None) -> list:
    """把中括号引用解析到具体资产（角色本体 / 该角色的某套服装 / 场景 / 道具）。

    关键点：【角色·服装】必须落到该角色的**那一套服装**上——组内先认角色，
    其余 part 优先在该角色的 outfits 里匹配，最后才回退到全局场景/道具名单。
    """
    out = []
    chars = [c for c in (characters or []) if isinstance(c, dict) and c.get("name")]
    scs = [s for s in (scenes or []) if isinstance(s, dict) and s.get("name")]
    prs = [p for p in (props or []) if isinstance(p, dict) and p.get("name")]
    for bracket, parts in _bracket_groups(*texts):
        item = {"bracket": bracket, "parts": parts,
                "character": None, "outfit": None, "scenes": [], "props": [], "unresolved": []}
        consumed = set()
        # ① 角色 + 该角色的服装变体
        for c in chars:
            cname = str(c.get("name") or "")
            hit_idx = [i for i, p in enumerate(parts) if _name_hit(p, cname)]
            if not hit_idx:
                continue
            consumed.update(hit_idx)
            item["character"] = {"name": cname, "appearance": str(c.get("image_prompt") or "")}
            for o in c.get("outfits") or []:
                if not isinstance(o, dict):
                    continue
                oname = str(o.get("name") or "")
                if not oname:
                    continue
                o_idx = [i for i, p in enumerate(parts) if i not in hit_idx and _name_hit(p, oname)]
                if o_idx:
                    item["outfit"] = {
                        "name": oname, "owner": cname,
                        "appearance": str(o.get("image_prompt") or o.get("description") or ""),
                    }
                    consumed.update(o_idx)
                    break
        # ② 场景 / 道具（未被角色组吃掉的部分）
        leftover = [p for i, p in enumerate(parts) if i not in consumed]
        for s in scs:
            sname = str(s.get("name") or "")
            if any(_name_hit(p, sname) for p in leftover):
                s_appear = str(s.get("image_prompt") or s.get("description") or "")
                # 场景在资产里摆了家具布置（大型家具/门窗位置朝向）→ 追加进引用说明，
                # 重写分镜提示词时必须照此布局写环境，防止门/窗/大件家具在镜头间漂移
                _fb = _furn_brief(s.get("furniture"))
                if _fb:
                    s_appear = (s_appear + "。" if s_appear else "") + _fb
                item["scenes"].append({"name": sname,
                                       "appearance": s_appear})
                consumed.update(i for i, p in enumerate(parts) if _name_hit(p, sname))
        for pr in prs:
            pname = str(pr.get("name") or "")
            if any(_name_hit(p, pname) for p in leftover):
                item["props"].append({"name": pname,
                                      "appearance": str(pr.get("image_prompt") or pr.get("description") or "")})
                consumed.update(i for i, p in enumerate(parts) if _name_hit(p, pname))
        # 兜底：拆出来的碎块里仍有约束味儿（如"中景，平视"被逗号拆散）的一律不算未匹配
        item["unresolved"] = [p for i, p in enumerate(parts)
                              if i not in consumed and not _looks_like_shot_param(p)]
        out.append(item)
    return out


_OUTFIT_WORDS = ("身穿", "身着", "穿着", "换上", "披着", "头戴", "腰佩", "服饰", "服装",
                 "铠甲", "盔甲", "战袍", "袍", "裙", "袄", "衫", "披风", "腰带", "靴", "鞋", "布巾", "头巾")


def _strip_outfit_phrases(anchor: str) -> str:
    """从角色锚点里剔除"服装类"分句。本镜已用【角色·服装】指定服装时，
    锚点里的盔甲/袍服描写会跟被引用服装打架（实测会把民女打扮画回铠甲），故剔除。"""
    if not anchor:
        return ""
    clauses = re.split(r"[，,。；;、]", anchor)
    keep = [c for c in clauses if c.strip() and not any(w in c for w in _OUTFIT_WORDS)]
    return "，".join(keep)


def _bracket_ref_block(refs) -> str:
    """把解析结果写成给重写模型的权威说明块（没有可解析引用时返回空串）。"""
    lines = []
    for r in refs:
        ch, of = r.get("character"), r.get("outfit")
        if of:
            face = _strip_outfit_phrases(ch.get("appearance") or "") if ch else ""
            lines.append(
                f"- 【{r['bracket']}】＝角色「{of['owner']}」的服装「{of['name']}」（换装引用：本镜头该角色就穿这套）。"
                f"服装/发饰/鞋履/随身小物必须按这套服装的设定写：{of['appearance'] or '（该套服装暂无文字描写，按服装名与角色锚点合理推断，不得编造与名字不符的服饰）'}。"
                "严禁改写成该角色的默认服装（如铠甲/战袍/婚服），也严禁两套服装混穿；"
                f"面部/发型/瞳色/体型仍按角色锚点{'：' + face if face else ''}"
            )
        elif ch:
            face = _strip_outfit_phrases(ch.get("appearance") or "")
            if r.get("unresolved"):
                lines.append(
                    f"- 【{r['bracket']}】＝角色「{ch['name']}」＋资产库里还没建的造型「{'、'.join(r['unresolved'])}」："
                    f"面部/发型/瞳色/体型按角色锚点{'（' + face + '）' if face else ''}；"
                    f"服装按「{'、'.join(r['unresolved'])}」这个名称的字面含义描写（这是导演指定的换装/状态），"
                    "不得回落成角色锚点里的默认服装，并在【约束】注明该造型还没有设定图、以文字为准。"
                )
            else:
                lines.append(
                    f"- 【{r['bracket']}】＝角色「{ch['name']}」：外形/发型/瞳色/体型/服装按角色锚点写"
                    f"{'：' + (ch.get('appearance') or '') if ch.get('appearance') else '（无锚点文字）'}。"
                )
        for s in r.get("scenes") or []:
            lines.append(
                f"- 【{r['bracket']}】＝场景「{s['name']}」：地点、空间结构、陈设、光线按该场景设定写"
                f"{'：' + s['appearance'] if s.get('appearance') else '（无设定文字，按场景名写环境，不得编造人物）'}。"
            )
        for p in r.get("props") or []:
            lines.append(
                f"- 【{r['bracket']}】＝道具「{p['name']}」：材质、颜色、形态、磨损按该道具设定写"
                f"{'：' + p['appearance'] if p.get('appearance') else '（无设定文字，按道具名写外形）'}。"
            )
        if r.get("unresolved") and not ch:
            lines.append(
                f"- 【{r['bracket']}】里「{'、'.join(r['unresolved'])}」在资产库里没有对应角色/服装/场景/道具："
                "中括号原样保留（前端会据此提示补建设定），外形按文字与常识写，不得编造具体设定。"
            )
    if not lines:
        return ""
    return "中括号引用解析结果（权威，重写时必须照此写外观；括号名逐字保留）：\n" + "\n".join(lines)


def _furn_pos_line(p: dict) -> str:
    """家具布置条目 → 位置描述文字（与前端 furniturePromptBlock 同一套词，保证 2D 图纸/3D 导演台/提示词三方一致）。"""
    try:
        x = min(1.0, max(0.0, float(p.get("x") if p.get("x") is not None else 0.5)))
        y = min(1.0, max(0.0, float(p.get("y") if p.get("y") is not None else 0.5)))
    except (TypeError, ValueError):
        x, y = 0.5, 0.5
    xs = "画左" if x < 0.33 else ("画右" if x > 0.67 else "画面中部")
    ys = "远景（空间深处）" if y < 0.33 else ("前景（靠近镜头）" if y > 0.67 else "中景")
    try:
        fv = float(p.get("facing") or 0) % 360
    except (TypeError, ValueError):
        fv = 0
    if fv < 45 or fv >= 315:
        fw = "正面朝空间深处"
    elif fv < 135:
        fw = "正面朝画右"
    elif fv < 225:
        fw = "正面朝向镜头"
    else:
        fw = "正面朝画左"
    return f"{str(p.get('label') or '家具')[:8]}：{xs}{ys}，{fw}"

def _furn_brief(furniture) -> str:
    """场景家具布置 → 一句话布局描述（供提示词重写/翻译时锁定大型家具与门窗位置朝向）。
    与前端 furnitureBrief 同一套词；位置/朝向措辞同 _furn_pos_line。"""
    items = [p for p in (furniture or []) if isinstance(p, dict)]
    if not items:
        return ""
    return "家具布置（大型家具与门窗的位置、朝向固定，任何镜头不得改变）：" + "；".join(_furn_pos_line(p) for p in items)

@app.post("/api/regenerate-prompt")
async def regenerate_prompt(req: RegeneratePromptRequest):
    if not _dsk_ok():
        raise HTTPException(400, "未配置 DeepSeek API Key，无法重新生成提示词。")
    t = (req.type or "").lower()
    if t not in ("character", "scene", "prop", "storyboard"):
        raise HTTPException(400, "type 必须是 character / scene / prop / storyboard")

    type_label = {"character": "角色设定图", "scene": "场景设定空镜", "prop": "道具设定图", "storyboard": "分镜图"}[t]
    # 界面下拉手选的参数：先提取出来，作为硬约束单独下发，并从旧提示词里摘掉
    picked = extract_shot_params(req.old_prompt)
    img_picked = {k: v for k, v in picked.items() if k != "运镜"}
    # 运镜参数：图片提示词里没选时，从旧视频提示词里补提（用户常只在视频框选运镜）
    old_vid_src = (req.old_video_prompt or "").strip()
    vid_picked = {"运镜": picked["运镜"]} if picked.get("运镜") else {}
    if not vid_picked and old_vid_src:
        _vpick = extract_shot_params(old_vid_src)
        if _vpick.get("运镜"):
            vid_picked = {"运镜": _vpick["运镜"]}
    old_prompt_clean = strip_shot_params(req.old_prompt, list(picked)) if picked else (req.old_prompt or "")
    # 视频提示词同样要剥掉手选运镜（它会作为硬约束单独下发，重写结果里再统一拼回）
    old_vid_clean = strip_shot_params(old_vid_src, list(vid_picked)) if (vid_picked and old_vid_src) else old_vid_src
    user_lines = []
    if req.style_bible:
        user_lines.append(f"画风圣经（重写后必须以其原文开头）：{req.style_bible}")
    if req.synopsis:
        user_lines.append(f"剧情梗概：{req.synopsis}")
    # 中括号引用解析：把【花木兰·民女打扮】这类引用落到具体资产上，
    # 让重写按"被引用的那套服装/那个场景"的真实设定写外观，而不是回落到角色默认造型
    bracket_refs = resolve_bracket_groups(
        [old_prompt_clean, req.old_prompt, req.description, req.feedback],
        characters=req.characters, scenes=req.scenes, props=req.props,
    )
    # 本镜实际被引用的服装（角色名 → 服装名集合）：用于把角色锚点里的服装描写剔除，
    # 否则"花木兰：…身穿青灰铠甲…"会和【花木兰·民女打扮】的布裙描写互相打架
    picked_by_char: Dict[str, set] = {}
    for _r in bracket_refs:
        _of = _r.get("outfit")
        if _of:
            picked_by_char.setdefault(_of["owner"], set()).add(_of["name"])
    if t == "storyboard" and req.characters:
        def _anchor_of(c):
            name = str(c.get("name") or "")
            txt = str(c.get("image_prompt") or "")
            outs = [str(o.get("name") or "") for o in (c.get("outfits") or [])
                    if isinstance(o, dict) and o.get("name")]
            if name in picked_by_char:
                txt = _strip_outfit_phrases(txt) or "（面部/发型/体型见角色设定图）"
                txt += f"〔本镜换装：{'、'.join(sorted(picked_by_char[name]))}——服装按该套设定写，锚点里的服装描写本镜不用〕"
            if outs:
                txt += f"〔该角色另有服装变体：{'、'.join(outs)}〕"
            return txt
        anchors = "；".join(
            f"{str(c.get('name') or '')}：{_anchor_of(c)}" for c in req.characters
        )
        user_lines.append(
            f"角色外观锚点（导演在角色卡上最终确定的最新外貌，权威基准）：{anchors}\n"
            "  重写规则：用户当前提示词里与锚点矛盾的过时外貌描述（性别/发型/体格/五官等）必须更新为锚点版本，不得原样保留；"
            "**服装不按角色锚点默认套装写**：凡提示词用中括号引用了某个服装变体（如【花木兰·民女打扮】），"
            "本镜头服装一律按下方「中括号引用解析结果」里那套服装的设定写；只有提示词没写服装引用、"
            "也没写与锚点不同的服装时，才用角色锚点里的服装。"
        )
    if bracket_refs:
        blk = _bracket_ref_block(bracket_refs)
        if blk:
            user_lines.append(blk)
    user_lines.append(f"要重写的提示词类型：{type_label}")
    if req.name:
        user_lines.append(f"名称：{req.name}")
    if req.description:
        if t == "storyboard":
            user_lines.append(f"剧情作用（导演在修改框里最新手改的剧情意图；与旧提示词冲突时以本条为准更新画面内容）：{req.description}")
        else:
            user_lines.append(f"设定描述（用户在设定框里最新手改的权威设定；与旧提示词冲突时以本条为准：年龄/性别/身份/发型/体型/服装等基础设定必须改写成与描述一致，不得保留过时版本）：{req.description}")
    if t == "storyboard":
        user_lines.append(f"台词（仅用于理解剧情，禁止写进画面）：{req.dialogue or '（无）'}")
        user_lines.append(f"该镜头时长：{req.duration} 秒")
    if old_prompt_clean:
        user_lines.append(f"用户当前版本的提示词（其中与最新设定不冲突的细节必须逐字保留；基础设定与设定描述冲突时按描述改写）：{old_prompt_clean}")
    if t == "storyboard" and old_vid_clean:
        user_lines.append(
            "用户当前版本的视频提示词（导演手改过的版本，是重写视频提示词的基础，严禁忽略后从零重写）：" + old_vid_clean + "\n"
            "  视频提示词重写规则：①用户亲笔写的动作时序、镜头运动、声音细节与最新设定不冲突的必须逐字保留，严禁替换、省略或换种说法；"
            "②只允许：套进标准单行结构、补全缺失维度、落实下方新要求；"
            "③与设定描述/角色锚点冲突的过时细节按设定改写。"
        )
    if img_picked or vid_picked:
        lines = [f"{k}={v}" for k, v in {**img_picked, **vid_picked}.items()]
        user_lines.append(
            "用户已在界面下拉中手动指定以下摄影参数（最高优先级硬约束，必须逐字使用、不得改写）："
            + "；".join(lines)
            + "。要求：①生图提示词里必须出现这些参数的原词（可写在【镜头细节】【光源】等小节，写法如「景别远景」「机位高角度仰拍」）；"
            "②严禁用近义词或自造说法替换（不得把「远景」写成「近景特写」，不得把「高角度仰拍」写成「略低仰拍」）；"
            "③提示词任何位置都不得出现与这些参数冲突的其他景别/景深/机位/焦段/构图/光线描述。"
            + ("；④运镜参数只用于 video_prompt 的【镜头运动】小节。" if vid_picked else "")
        )
    if req.feedback:
        user_lines.append(f"用户的新要求（最高优先级，必须落实）：{req.feedback}")
    # 场景家具布置：导演在场景资产里摆的俯视平面布局（与 3D 导演台同一套坐标约定），
    # 重写时空间布局必须与此一致；生成时前端会自动把结构化清单追加进提示词，重写结果里不要重复罗列
    if t == "scene" and req.furniture:
        _fl = [_furn_pos_line(p) for p in req.furniture if isinstance(p, dict)]
        _fl = [x for x in _fl if x]
        if _fl:
            user_lines.append(
                "场景已有家具布置（俯视平面规划：上=空间深处/远景，下=靠近镜头/前景，左=画左，右=画右；"
                "生成设定图时系统会自动把该清单追加进提示词）：\n- " + "\n- ".join(_fl) +
                "\n  要求：重写提示词中的空间布局描述必须与上表一致，禁止另编家具位置或增删家具；不要在提示词里重复罗列这份清单。"
            )

    # 提示词库参考：语料检索 + 风格卡（失败自动降级，不影响重写主流程）
    _ref_query = " ".join(
        str(x or "").strip() for x in (req.description, req.old_prompt, req.feedback, req.synopsis) if x
    )
    _ref_block = _oip_reference_block(_ref_query, limit=3)
    if _ref_block:
        user_lines.append(_ref_block)
    _card_block = _oip_style_card_block(req.style_bible, req.synopsis, req.description, req.feedback)
    if _card_block:
        user_lines.append(_card_block)

    try:
        messages = [
            {"role": "system", "content": REGEN_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(user_lines)},
        ]
        data = None
        last_err = None
        for attempt in range(2):  # 解析失败时补一句提醒重试一次，比直接报错友好
            response = _dsk().chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=3000,
                temperature=0.8 if attempt == 0 else 0.3,
            )
            try:
                data = deepseek_json(response)
                break
            except ValueError as e:
                last_err = e
                print(f"[regenerate-prompt] 第 {attempt + 1} 次解析失败：{e}")
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": "(上一次输出不是合法 JSON)"},
                        {"role": "user", "content": "上一次输出不是合法 JSON（可能被截断或含未转义字符）。"
                                                    "请重新输出，严格只输出一个 JSON 对象："
                                                    '{"image_prompt": "...", "video_prompt": "..."}，'
                                                    "不要 markdown 代码块、不要换行、不要任何额外文字。"},
                    ]
        if data is None:
            raise HTTPException(500, f"重新生成失败：模型输出不是合法 JSON，请重试（{last_err}）")
        image_prompt = str(data.get("image_prompt") or "").strip()
        if not image_prompt:
            raise HTTPException(500, "重新生成失败：模型未返回提示词，请重试")
        video_prompt = str(data.get("video_prompt") or "").strip() if t == "storyboard" else ""
        # 画风设定开头保底：系统提示词已要求，但模型偶尔漏带——这里确定性补上，
        # 保证重写后的视频提示词永远以画风圣经原文开头（用户明确要求）
        if video_prompt and (req.style_bible or "").strip() and not video_prompt.strip().startswith(req.style_bible.strip()[:20]):
            video_prompt = req.style_bible.strip() + "。" + video_prompt
        # 回落保障：模型若把用户手选的参数改写成散文，统一清掉变体后重新拼成标准参数块，
        # 保证「手动选择 > AI 生成」这条优先级在任何情况下都成立。
        if img_picked:
            body = strip_shot_params(image_prompt, list(img_picked))
            blk = _param_block(img_picked, SHOT_PARAM_OPTS.keys())
            image_prompt = (body + "，" + blk) if body else blk
        if vid_picked and video_prompt:
            body = strip_shot_params(video_prompt, ["运镜"])
            blk = "运镜：" + vid_picked["运镜"]
            video_prompt = (body + "，" + blk) if body else blk
        # 中括号资产引用保真兜底：模型偶尔仍会把【木兰·铠甲】【村庄】这类引用改写掉，
        # 而前端生图就靠这些括号名匹配资产设定图——逐个核对，丢了的原样补回。
        if t == "storyboard":
            user_refs = extract_bracket_asset_names(old_prompt_clean, old_vid_clean, req.description, req.feedback)
            if user_refs:
                # 生图提示词和视频提示词分别核对：视频参考池靠视频提示词里的【角色·服装】引用
                # 挂设定图（用户实测教训：视频提示词丢了服装引用 → 参考池缺图 → 服装跑偏），
                # 所以两边丢了的引用都要各自补回，不能只补生图侧。
                miss_img = [nm for nm in user_refs if nm not in image_prompt]
                if miss_img:
                    image_prompt = image_prompt.rstrip("。 ") + "。资产引用：" + "".join(f"【{nm}】" for nm in miss_img)
                miss_vid = [nm for nm in user_refs if (not video_prompt or nm not in video_prompt)]
                if miss_vid:
                    video_prompt = video_prompt.rstrip("。 ") + "。资产引用：" + "".join(f"【{nm}】" for nm in miss_vid)
                if miss_img or miss_vid:
                    print(f"[regenerate-prompt] 补回丢失的中括号引用：生图={miss_img} 视频={miss_vid}")
        log_prompt("重生成提示词", image_prompt, f"type={t}")
        return {"image_prompt": image_prompt, "video_prompt": video_prompt}
    except ValueError as e:
        print(f"[regenerate-prompt] 输出解析失败：{e}")
        raise HTTPException(500, "DeepSeek 返回的不是合法 JSON，请重试")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(e.status_code if isinstance(e, HTTPException) else 500, deepseek_friendly_error(e))

# ---------- 2.55 提示词库检索 API（前端「提示词库」面板 + 状态探针） ----------
class PromptSearchRequest(BaseModel):
    q: str
    limit: int = 8

@app.get("/api/prompt-library/status")
async def prompt_library_status():
    """语料库就绪状态（未配置 OIP_DB_PATH 时 ready=false，前端隐藏检索面板入口）。"""
    return oip_lib.corpus_status()

@app.post("/api/prompt-search")
async def prompt_search(req: PromptSearchRequest):
    """中英双语提示词检索：自然语言 → v2 视觉标签 → 语料打分排序，结果可追溯来源。"""
    q = (req.q or "").strip()[:200]
    if not q:
        raise HTTPException(400, "请输入搜索内容，例如：黄昏公园逆光人物剪影 / neon alley portrait")
    try:
        payload = oip_lib.search_corpus(q, limit=max(1, min(int(req.limit or 8), 20)), max_prompt_chars=1200)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(503, f"提示词库暂不可用：{e}")
    return oip_lib.slim_results(payload, zh_chars=800, prompt_chars=600)

@app.get("/api/style-cards")
async def style_cards():
    """美术方向风格卡列表（img-gen-taste 移植版）：id + 标题 + 一句话摘要。"""
    return {"cards": oip_lib.card_list()}

@app.get("/api/style-cards/{name}")
async def style_card_detail(name: str):
    """单张风格卡完整要点。name 限字母/连字符，防路径穿越。"""
    if not re.fullmatch(r"[a-z0-9-]{2,40}", name):
        raise HTTPException(400, "非法卡片名")
    card = oip_lib.card_full(name)
    if not card:
        raise HTTPException(404, "风格卡不存在")
    return card

# ---------- 2.6 调度参考图提示词：俯视调度图 / 轨迹调度图（供分镜生成视频时给 AI 参考） ----------
class TopViewRequest(BaseModel):
    kind: str = "topview"  # topview=俯视调度图 / trajectory=轨迹调度图
    description: str = ""
    dialogue: str | None = None
    duration: int = 3
    video_prompt: str | None = None
    scene_name: str | None = None
    scene_description: str | None = None
    characters: List[Dict[str, Any]] | None = None
    style_bible: str | None = None
    old_prompt: str | None = None
    feedback: str | None = None
    base_plot: Dict[str, Any] | None = None  # 现有俯视调度图坐标（轨迹图以此为权威起点，不自由发挥）

TOPVIEW_SYSTEM_PROMPT = """你是动画导演的调度图纸绘制员。任务：为「俯视调度图」撰写文生图提示词。最终图像是一张【导演工作图纸】——从正上方垂直向下看的平面布置图（top-down floor plan），不是一幅场景画。

【image_prompt 撰写要求】单行输出，依次包含：
1. 图型声明（必须放最前、语气绝对）：纯正上方垂直俯视的平面调度图纸（top-down floor plan，正交投影、无透视、无景深），白底极简图纸风格，像建筑平面图/桌游沙盘顶视图；禁止画成立体透视场景，禁止动漫画面，禁止真实人物立绘。
2. 平面布局：按场景描述用简化几何形状（线框/浅色色块）画出地面边界与主要陈设的俯视投影（桌、床、门、窗、围栏、楼梯等），可加简短名称标注；不做细节刻画。
3. 人物棋子：只为本镜头实际入画的角色画小型俯视人偶棋子图标（圆头+肩形剪影）并配角色名文字标签——名单中未被镜头描述/口述提及的角色严禁画出（无关棋子会污染图纸）；图标位置即人物站位，用小箭头表示朝向；剧情中有移动的，用带箭头虚线画出运动路线。
4. 摄影机：用「机位」符号标出摄影机位置，并在符号处画一个扇形/梯形视锥表示拍摄朝向与视野范围；机位有移动的用箭头标注起止。
5. 文字约束：图上文字只允许角色名/陈设名/「机位」等简体中文短标签，禁止长句、字幕、水印。

【用户口述与修改要求】若提供「用户的新要求」，那是导演的明确调度指令（位置关系、朝向、机位或运动轨迹），必须逐条严格执行并完整体现在图中，与镜头描述冲突时以口述为准；旧提示词仅作基础参考。未提供口述时，按镜头描述自行编排合理调度。
【输出】严格输出 JSON：{"image_prompt": "..."}，不要 markdown，不要任何其他文字。"""

TRAJECTORY_SYSTEM_PROMPT = """你是动画导演的动作设计与运镜顾问。任务：为「轨迹调度图」撰写文生图提示词。最终图像是一张【导演工作图纸】——正上方垂直俯视的平面运动轨迹图（与俯视调度图同一视角、同一坐标系，可叠合使用），不是一幅场景画。

【image_prompt 撰写要求】单行输出，依次包含：
1. 图型声明（必须放最前、语气绝对）：纯正上方垂直俯视的运动轨迹图纸（top-down floor plan，正交投影、无透视、无景深），白底极简图纸风格，彩色轨迹线醒目；禁止画成立体透视场景，禁止动漫画面，禁止真实人物立绘。
2. 平面底图：用简化几何形状（线框/浅色色块）勾勒场景地面边界与关键陈设的俯视投影（桌、门、楼梯等）并标注名称，不做细节刻画。
3. 角色轨迹：只为本镜头实际入画的角色画轨迹（名单中未被镜头描述/口述提及的角色严禁画出），每个入画角色用不同颜色，从起点到终点画一条连续的轨迹线（实线箭头），轨迹上用时间刻度小圆点标出关键节点（如 0 秒起点、中途转折、终点），节点旁标注动作短语（如“起跑”“转身”“急停”）；起点终点画人偶棋子图标+角色名标签。
4. 镜头轨迹：摄影机位置画「机位」符号+扇形视锥表示拍摄朝向，机位运动（推/拉/摇/移/跟）用黑色虚线箭头单独画出，标注方向与起止，与彩色角色轨迹区分。
5. 速度与节奏：在轨迹线上标注快慢（如"慢走""冲刺""急停"）。
6. 文字约束：图上文字只允许角色名/动作/秒数/「机位」等简体中文短标签，禁止长句、字幕、水印。

【用户口述与修改要求】若提供「用户的新要求」，那是导演的明确调度指令（位置关系、朝向、机位或运动轨迹），必须逐条严格执行并完整体现在图中，与镜头描述冲突时以口述为准；旧提示词仅作基础参考。未提供口述时，按镜头描述自行编排合理调度。
【输出】严格输出 JSON：{"image_prompt": "..."}，不要 markdown，不要任何其他文字。"""

def _fallback_diagram_prompt(req: TopViewRequest) -> str:
    chars = "、".join(str((c or {}).get("name") or "").strip() for c in (req.characters or []) if (c or {}).get("name"))
    parts = []
    if (req.kind or "").lower() == "trajectory":
        parts.append("纯正上方垂直俯视的运动轨迹图纸（top-down floor plan，正交投影、无透视、无景深），白底极简图纸风格，彩色轨迹线醒目；禁止立体透视场景，禁止动漫画面，禁止真实人物立绘")
        parts.append("平面底图：简化几何线框勾勒场景地面边界与关键陈设的俯视投影")
        parts.append(f"角色运动轨迹（每人一种颜色，起点到终点实线箭头，时间刻度圆点标注关键动作节点，起终点画人偶棋子图标+名字标签）：{chars or '按画面描述标注'}")
        parts.append("摄影机位置画「机位」符号+扇形视锥，机位运动用黑色虚线箭头单独画出并标注方向与起止")
        parts.append(f"本镜头运动内容：{req.video_prompt or req.description[:150]}")
    else:
        parts.append("纯正上方垂直俯视的平面调度图纸（top-down floor plan，正交投影、无透视、无景深），白底极简图纸风格，像建筑平面图；禁止立体透视场景，禁止动漫画面，禁止真实人物立绘")
        if req.scene_description:
            parts.append(f"平面布局（简化几何线框/浅色块画陈设俯视投影并标注名称）：{req.scene_description[:120]}")
        parts.append(f"本镜头调度：{req.description[:150]}")
        parts.append(f"出场角色（俯视人偶棋子图标+名字标签，标明站位与朝向，移动路线用虚线箭头）：{chars or '按画面描述标注'}")
        parts.append("用「机位」符号标出摄影机位置，并画扇形视锥表示拍摄朝向与视野")
    parts.append("图上文字仅限角色名/陈设名/动作/秒数/「机位」的简体中文短标签，禁止长句与水印")
    return "；".join(p for p in parts if p)

@app.post("/api/diagram-prompt")
async def diagram_prompt(req: TopViewRequest):
    """俯视调度图 / 轨迹调度图 的文生图提示词编排。DeepSeek 失败时用本地模板兜底，保证功能永远可用。"""
    kind = (req.kind or "topview").lower()
    system_prompt = TRAJECTORY_SYSTEM_PROMPT if kind == "trajectory" else TOPVIEW_SYSTEM_PROMPT
    user_lines = []
    # 注意：调度图是导演工作图纸，不套用 style_bible 成片画风（避免被带成透视场景画）
    if req.scene_name:
        user_lines.append(f"所在场景：{req.scene_name}")
    if req.scene_description:
        user_lines.append(f"场景描述：{req.scene_description}")
    user_lines.append(f"镜头画面描述：{req.description or '（无）'}")
    user_lines.append(f"台词（仅供理解剧情，不要成为图上文字）：{req.dialogue or '（无）'}")
    user_lines.append(f"镜头时长：{req.duration} 秒")
    if req.video_prompt:
        user_lines.append(f"视频运动描述：{req.video_prompt}")
    if req.characters:
        chars = "；".join(f"{str(c.get('name') or '')}：{str(c.get('description') or '')[:60]}" for c in req.characters if c.get("name"))
        if chars:
            user_lines.append(f"出场角色：{chars}")
    if req.old_prompt:
        user_lines.append(f"旧提示词（在其基础上重写）：{req.old_prompt}")
    if req.feedback:
        user_lines.append(f"用户的新要求（最高优先级）：{req.feedback}")

    try:
        response = _dsk().chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "\n".join(user_lines)},
            ],
            response_format={"type": "json_object"},
            max_tokens=1000,
            temperature=0.6,
        )
        data = deepseek_json(response)
        prompt = str(data.get("image_prompt") or "").strip()
        if prompt:
            log_prompt(f"调度图提示词({kind})", prompt)
            return {"image_prompt": prompt, "source": "ai"}
    except Exception as e:
        print(f"[diagram-prompt] DeepSeek 生成失败（{kind}），用模板兜底: {type(e).__name__}: {e}")
    fb = _fallback_diagram_prompt(req)
    log_prompt(f"调度图提示词({kind}, 模板兜底)", fb)
    return {"image_prompt": fb, "source": "template"}

# ---------- 2.62 调度图纸（新方案）：DeepSeek 解析口述为坐标数据，PIL 程序化绘制平面图纸 ----------
# AI 生图画"俯视图"总会跑偏成透视场景照片，故改为：AI 只负责把口述转成结构化坐标，图纸由代码精确绘制。

DIAGRAM_PLOT_PROMPT = """你是调度图纸数据员。把导演的调度口述与镜头描述解析为俯视平面图纸的结构化坐标数据（图纸由程序绘制，你只产出数据）。
【坐标系】x：0=图纸左，1=图纸右；y：0=图纸上方（空间远端），1=图纸下方（空间近端）。角度：0=朝图纸上方，90=朝右，180=朝下方，270=朝左，顺时针，单位度。
【输出格式】严格只输出 JSON，不要 markdown，不要其他文字：
{
  "props": [{"label":"名称","shape":"rect","x":0.5,"y":0.5,"w":0.2,"h":0.15,"facing":0}],
  "characters": [{"name":"角色名","x":0.3,"y":0.6,"facing":0,"path":[[0.3,0.6],[0.3,0.4]],"stops":[{"t":"0秒","action":"起跑"},{"t":"3秒","action":"急停"}]}],
  "camera": {"x":0.5,"y":0.95,"facing":0,"fov":55,"path":[],"movement":"固定机位"},
  "note": "一句话备注，可为空字符串"
}
【规则】
1. 导演口述是最高优先级，逐条落实为坐标；口述中的「画左/画右/画上方/画下方」直接对应图纸左/右/上/下。
2. 【「镜头」＝「机位」语义（极重要）】口述里的「镜头」「摄影机」「机位」一律指 camera 机位符号所在的那个坐标点（机位的 x,y），绝不是"图纸下方""图纸上方""画面外观众方向"或"空间近端"：
   - 「走向镜头／走向机位／走到镜头前／冲向镜头／走近摄影机／来到镜头前」：该角色 path 终点必须落在机位附近（距机位约 0.05~0.15，不与机位精确重叠），path 就是起点→机位的两点一线（最多加 1 个轻微顺路拐点，整体仍近似直线），并把 facing 改为面向机位；终点不得留在原地、也不得偏向图纸下/上方。
   - 「远离镜头／退向画面纵深／走向远处」：沿"起点→机位"连线的反方向拉开距离，facing 改为背向机位。
   - 机位在哪，"镜头前"就在哪——先看机位坐标，再落轨迹终点。
3. props：按场景描述列出主要陈设的俯视投影（水箱、桌、床、门等），x,y 为中心点、w,h 为宽高（均为 0-1）；facing 为朝向角度（可选，0-360 整数，0=朝图纸上方、顺时针，门/床/柜等有明确朝向的陈设按场景描述填写，默认 0）。
4. characters：【出场角色】名单是全剧组候选名单，不是本镜头入画名单——只把口述或镜头描述中点名/明确参与的角色放进 characters（输出名字必须与名单逐字一致）。名单中未被提及的角色【严禁】放上图纸（实测最严重错误：口述只提一个人，AI 把全剧组都摆上去——无关角色的棋子图标会污染整张图纸并传导进分镜成片）。导演口述中若用了名单外的称呼（昵称、绰号、随口叫的名字，如把"胖虎"叫成"小胖"），必须按剧情与角色描述判断它实际指名单中的哪一位，映射成名单名输出，并在 note 中注明映射关系（如"口述的小胖=胖虎"）；严禁发明名单外的角色名，同名角色只能出现一次。口述/描述用「两人」「众人」「大家」等集合词而未点名的，按剧情推断出最可能的入画角色（数量与集合词一致）并在 note 中注明推断依据；已判定入画但口述未提及其位置的角色，按剧情合理推断放置并在 note 中注明。有移动的给 path（按时间顺序 2-6 个拐点）与 stops（与拐点对应）。
   【动谁停谁（极重要）】path 只给「口述中明确点到名字且描述了移动」的角色；口述里没出现名字的角色一律 path 空数组、stops 空数组、朝向照抄起点，严禁因为剧情氛围或画面描述里的其他动作就替他安排走位（实测教训：口述只说"小帅走向镜头"，AI 让小美也走了——错误）。
5. camera：机位位置与拍摄朝向；movement 为「固定机位」时 path 为空数组；fov 缺省 55。
6. 所有坐标数值必须在 0-1 范围内。
7. 若用户提供「现有俯视调度图坐标」（权威起点数据）：它是同一空间同一时刻的既成事实，所有角色的起点 x,y 与朝向必须照抄该数据中同名角色的值，严禁重新安排站位或凭剧情重新布局；轨迹 path 必须从该起点出发，只解析口述中提到的移动/朝向变化，未提及移动的角色停在原位（path 空数组、朝向照抄）；陈设 props 与机位起点同样照抄该数据，仅当口述明确说机位移动时才给 camera.path。"""

def _fallback_plot(req: TopViewRequest) -> dict:
    """DeepSeek 解析失败时的默认布局（明确告知用户重试）。"""
    chars = [str((c or {}).get("name") or "").strip() for c in (req.characters or []) if (c or {}).get("name")]
    chars = [c for c in chars if c]
    n = max(len(chars), 1)
    characters = []
    for i, name in enumerate(chars):
        row = i // 5
        col = i % 5
        cnt = min(n - row * 5, 5)
        characters.append({
            "name": name, "x": (col + 1) / (cnt + 1), "y": 0.45 + row * 0.25,
            "facing": 180 if row == 0 else 0, "path": [], "stops": [],
        })
    return {
        "props": [], "characters": characters,
        "camera": {"x": 0.5, "y": 0.95, "facing": 0, "fov": 55, "path": [], "movement": "固定机位"},
        "note": "AI 解析口述失败，已用默认布局——请检查口述后重新生成",
    }

def _clamp01(v) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


def _name_match(a: str, b: str) -> bool:
    """宽松名字匹配：完全相等 / 互为子串 / 去掉'小''阿''老'前缀后相等 / 单字重叠≥一半。
    用于把口述里的昵称（小胖/憨憨）对齐回角色名单（胖虎/陈屿）。"""
    a, b = a.strip(), b.strip()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    strip = lambda s: s.lstrip("小阿老")
    if strip(a) == strip(b):
        return True
    common = set(a) & set(b)
    return len(common) / max(len(set(a)), len(set(b))) >= 0.5 and len(common) >= 1


def _align_character_names(plot: dict, cast: list) -> dict:
    """把 AI 解析出的角色名强制对齐到项目角色名单。
    口述里的昵称/记错的名字若被 DeepSeek 照抄成新角色，图纸就会出现两套人马：
    调度约束与分镜提示词名字对不上 → 生图重复画人（两个"小胖"）、位置错乱。
    策略：精确/宽松匹配映射 → 完全对不上时按出场顺序映射 → 多出的丢弃 → 名单缺的补齐。"""
    canonical = []
    for c in cast or []:
        n = str((c or {}).get("name") or "").strip()
        if n and n not in canonical:
            canonical.append(n)
    if not canonical:
        return plot
    # 名字→描述文本（昵称对不上名字时，用描述里的特征字兜底，如"小胖"→胖虎描述里的"偏胖"）
    desc_of = {}
    for c in cast or []:
        n = str((c or {}).get("name") or "").strip()
        if n:
            desc_of[n] = str((c or {}).get("description") or "")
    ai_chars = [c for c in (plot.get("characters") or [])
                if isinstance(c, dict) and str(c.get("name") or "").strip()]
    if not ai_chars:
        return plot

    notes = []
    mapped, used = [], set()
    unmatched = []
    strip = lambda s: s.lstrip("小阿老").strip()
    for c in ai_chars:
        raw = str(c.get("name")).strip()
        target = next((n for n in canonical if n not in used and _name_match(raw, n)), None)
        if not target:
            # 描述特征匹配：昵称的特征字（去掉小/阿/老前缀）出现在角色描述里
            nick = strip(raw)
            if nick:
                target = next((n for n in canonical if n not in used and nick in desc_of[n]), None)
        if target:
            if raw != target:
                notes.append(f"口述的{raw}={target}")
            c["name"] = target
            used.add(target)
            mapped.append(c)
        else:
            unmatched.append(c)

    # 对不上的按出场顺序映射到名单中未占用的名字（口述顺序通常对应角色出场顺序，
    # 保住口述落位布局比丢弃更有价值；超出的才是真正该忽略的多余角色）
    unused = [n for n in canonical if n not in used]
    for i, c in enumerate(unmatched):
        if i >= len(unused):
            notes.append(f"忽略名单外角色{c.get('name')}")
            continue
        notes.append(f"口述的{c['name']}={unused[i]}")
        c["name"] = unused[i]
        used.add(unused[i])
        mapped.append(c)

    # 名单中实际出场但 AI 漏掉的：补默认站位（避免图纸少人、与分镜提示词又对不上）
    missing = [n for n in canonical if n not in used]
    for i, n in enumerate(missing):
        mapped.append({"name": n, "x": (len(mapped) % 4 + 1) / 5, "y": 0.5,
                       "facing": 180, "path": [], "stops": []})
    if missing:
        notes.append("补齐未提及角色：" + "、".join(missing))

    plot["characters"] = mapped
    if notes:
        prev = str(plot.get("note") or "").strip()
        plot["note"] = (prev + ("；" if prev else "") + "；".join(notes))[:200]
    return plot




def _merge_base_plot(plot: dict, base: dict) -> dict:
    """轨迹图以现有俯视调度图为起点：角色起点/朝向/陈设/机位起点强制对齐 base 坐标。
    AI 解析结果只贡献「运动轨迹与朝向变化」，防止其自由发挥重新布局。"""
    def _near(a, b, tol=0.09) -> bool:
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return False

    bchars = {}
    for c in (base.get("characters") or []):
        if isinstance(c, dict) and str(c.get("name") or "").strip():
            bchars[str(c.get("name")).strip()] = c

    merged, seen = [], set()
    for c in (plot.get("characters") or []):
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        name = str(c.get("name")).strip()
        b = bchars.get(name)
        if not b:
            merged.append(c)
            continue
        seen.add(name)
        path = [list(pt)[:2] for pt in (c.get("path") or [])
                if isinstance(pt, (list, tuple)) and len(pt) >= 2]
        if path:
            # 轨迹必须从 base 起点出发：首个拐点偏离起点则补插起点
            if not (_near(path[0][0], b.get("x")) and _near(path[0][1], b.get("y"))):
                path = [[b.get("x"), b.get("y")]] + path
            # 有移动：起点照抄 base，终点朝向用 AI 解析的（转身等朝向变化）
            merged.append({**c, "x": b.get("x"), "y": b.get("y"), "path": path})
        else:
            # 口述未提及移动：停在原位、朝向照抄 base
            merged.append({**c, "x": b.get("x"), "y": b.get("y"),
                           "facing": b.get("facing", c.get("facing")), "path": [], "stops": []})
    # base 里有、口述没提的角色：原位保留（两图人物一致，可叠合对照）
    for name, b in bchars.items():
        if name not in seen:
            merged.append({**b, "path": [], "stops": []})
    plot["characters"] = merged

    # 陈设照抄 base：同一场景，两图坐标系一致
    if base.get("props"):
        plot["props"] = base.get("props")
    # 房间尺寸照抄 base：同一场景，房间大小不能因 AI 重解析被洗回默认
    if isinstance(base.get("room"), dict):
        plot["room"] = base.get("room")

    # 机位起点照抄 base；仅当 AI 解析出机位移动路径时才允许动
    bc = base.get("camera") if isinstance(base.get("camera"), dict) else {}
    if bc:
        pc = plot.get("camera") if isinstance(plot.get("camera"), dict) else {}
        pc["x"], pc["y"] = bc.get("x"), bc.get("y")
        cam_path = [list(pt)[:2] for pt in (pc.get("path") or [])
                    if isinstance(pt, (list, tuple)) and len(pt) >= 2]
        if cam_path:
            if not (_near(cam_path[0][0], bc.get("x")) and _near(cam_path[0][1], bc.get("y"))):
                cam_path = [[bc.get("x"), bc.get("y")]] + cam_path
            pc["path"] = cam_path
        else:
            pc["facing"] = bc.get("facing", pc.get("facing"))
            if not str(pc.get("movement") or "").strip() or "固定" in str(pc.get("movement")):
                pc["movement"] = bc.get("movement") or "固定机位"
        plot["camera"] = pc
    return plot

def _room_dims(plot: dict) -> tuple:
    """房间真实尺寸（米）。plot.room={w:宽米,h:深米}，缺省 10×10（历史图纸兼容）。
    归一化坐标不变（仍 0-1），只是换算米数时按房间尺寸算——3D 导演台/2D 图纸/提示词同一套换算。"""
    room = plot.get("room") if isinstance(plot.get("room"), dict) else {}
    out = []
    for v in (room.get("w"), room.get("h")):
        try:
            out.append(round(min(50.0, max(2.0, float(v))), 1))
        except (TypeError, ValueError):
            out.append(10.0)
    return out[0], out[1]

def _normalize_plot(plot: dict) -> dict:
    props = []
    for p in (plot.get("props") or [])[:12]:
        if not isinstance(p, dict):
            continue
        props.append({
            "label": str(p.get("label") or "陈设")[:8],
            "shape": "rect",
            "x": _clamp01(p.get("x")), "y": _clamp01(p.get("y")),
            "w": min(1.0, max(0.02, _clamp01(p.get("w")) or 0.15)),
            "h": min(1.0, max(0.02, _clamp01(p.get("h")) or 0.1)),
            # 建筑朝向（度，0=朝图纸上方、顺时针，与人物/机位同一约定）。必须在 normalize 里保留，
            # 否则每次重绘（rerender-diagram）都会把导演在 3D 导演台转好的朝向洗掉
            "facing": float(p.get("facing") or 0) % 360,
        })
    characters = []
    seen_names = set()
    for c in (plot.get("characters") or [])[:10]:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        cname = str(c.get("name")).strip()
        if cname in seen_names:
            continue  # 同名去重：任何路径产出两个同名角色都只保留第一个，杜绝图上两个"小胖"
        seen_names.add(cname)
        path = []
        for pt in (c.get("path") or [])[:8]:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                path.append([_clamp01(pt[0]), _clamp01(pt[1])])
        stops = []
        for s in (c.get("stops") or [])[:8]:
            if isinstance(s, dict) and (str(s.get("action") or "").strip() or str(s.get("t") or "").strip()):
                stops.append({"t": str(s.get("t") or "")[:6], "action": str(s.get("action") or "")[:8]})
        facing = float(c.get("facing") or 0)
        characters.append({
            "name": str(c.get("name"))[:6],
            "x": _clamp01(c.get("x")), "y": _clamp01(c.get("y")),
            "facing": facing % 360,
            "path": path, "stops": stops,
        })
    cam = plot.get("camera") if isinstance(plot.get("camera"), dict) else {}
    cam_path = []
    for pt in (cam.get("path") or [])[:8]:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            cam_path.append([_clamp01(pt[0]), _clamp01(pt[1])])
    # 机位高度（米）：0.2-4.0，缺省视为 1.6（人眼平拍）。必须在 normalize 里保留，
    # 否则每次重绘（rerender-diagram）都会把导演在 3D 导演台调好的高度洗掉
    try:
        cam_height = round(min(4.0, max(0.2, float(cam.get("height")))), 2) if cam.get("height") is not None else None
    except (TypeError, ValueError):
        cam_height = None
    # 镜头与地面夹角（度）：正=朝下俯拍、负=朝上仰拍、0=水平。范围 -60~60
    try:
        cam_pitch = round(min(60.0, max(-60.0, float(cam.get("pitch")))), 0) if cam.get("pitch") is not None else None
    except (TypeError, ValueError):
        cam_pitch = None
    camera = {
        "x": _clamp01(cam.get("x", 0.5)), "y": _clamp01(cam.get("y", 0.95)),
        "facing": float(cam.get("facing") or 0) % 360,
        "fov": min(120.0, max(15.0, float(cam.get("fov") or 55))),
        "path": cam_path,
        "movement": str(cam.get("movement") or "固定机位")[:10],
    }
    if cam_height is not None:
        camera["height"] = cam_height
    if cam_pitch is not None:
        camera["pitch"] = cam_pitch
    # 房间尺寸（米）：导演在 3D 导演台调好的房间大小必须保留，否则每次重绘都会洗回 10×10
    room = plot.get("room") if isinstance(plot.get("room"), dict) else None
    out = {"props": props, "characters": characters, "camera": camera,
           "note": str(plot.get("note") or "")[:80]}
    if room:
        try:
            out["room"] = {"w": round(min(50.0, max(2.0, float(room.get("w") or 10))), 1),
                            "h": round(min(50.0, max(2.0, float(room.get("h") or 10))), 1)}
        except (TypeError, ValueError):
            pass
    return out

# 角色图标配色池（醒目、互相区分）
_DIAGRAM_PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
                    "#008080", "#f032e6", "#9a6324", "#000075", "#800000"]

def _frame_side_text(cross: float, dist: float, fov: float) -> str:
    """把机位相对的左右偏角换成生图模型能落地的画面落位描述。
    cross/dist ＝ 人物相对视轴的带符号正弦（>0 视轴右侧）。
    近轴（几乎正对镜头）时必须说'画面中部'——若硬说'画面左/右'，
    模型会把几度的微小偏差放大成明显靠边，构图直接跑偏。"""
    import math
    if dist <= 1e-6:
        return "画面中部"
    lat_deg = math.degrees(math.asin(max(-1.0, min(1.0, cross / dist))))  # >0=视轴右侧
    if abs(lat_deg) < 8:
        return "画面中部（几乎正对镜头）"
    side = "右侧" if lat_deg > 0 else "左侧"
    # 细分两档：偏中 / 靠边——只说"画面左侧"时，模型常把 20° 偏移和 5° 偏移画成同一位置
    if abs(lat_deg) < 20:
        return f"画面{side}偏中（约偏离画面中线四分之一画幅，与画面中心之间留有空隙）"
    edge = "、贴近画面边缘可能被裁切" if abs(lat_deg) > max(fov / 2, 30) else "、靠画面边一带"
    return f"画面{side}{edge}"


def _camera_height_desc(h) -> str:
    """机位高度（米）→ 拍摄角度描述。导演台滑条、2D 图纸标注、提示词翻译统一口径。"""
    try:
        h = float(h)
    except (TypeError, ValueError):
        return ""
    if h <= 0.6:
        return "贴地仰拍（低角度朝上拍）"
    if h <= 1.0:
        return "低机位微仰拍"
    if h <= 2.0:
        return "平拍（与人眼等高）"
    if h <= 2.8:
        return "微俯拍"
    return "高机位俯拍（从高处向下俯瞰）"


def _camera_height_rule(h) -> str:
    """机位高度 → 生图/生视频模型能执行的构图规则（空字符串=平拍无需特别约束）。"""
    try:
        h = float(h)
    except (TypeError, ValueError):
        return ""
    if h <= 0.6:
        return ("机位高度约{h:.1f}米，贴地仰拍：镜头从低处朝上拍，人物显得高大威严，"
                "画面下半部被人物腿部/身体占据，背景多为天空或天花板，地面几乎不可见，严禁俯视构图。".format(h=h))
    if h <= 1.0:
        return "机位高度约{h:.1f}米，低机位微仰拍：略带仰角，人物比画面中的建筑/背景更高耸。".format(h=h)
    if h <= 2.0:
        return ""
    if h <= 2.8:
        return "机位高度约{h:.1f}米，微俯拍：略带俯角，地面在画面中占比更大，人物略被压低。".format(h=h)
    return ("机位高度约{h:.1f}米，高机位俯拍：从高处向下俯瞰，人物头顶可见、身形被压缩，"
            "地面/地面陈设占据画面大部分，严禁出现地平线在画面上缘的正常视角构图。".format(h=h))


def _camera_pitch_desc(p) -> str:
    """镜头与地面夹角（度，正=朝下俯、负=朝上仰）→ 拍摄角度描述。"""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return ""
    if p <= -40:
        return f"大仰角（镜头朝上{abs(int(p))}°）"
    if p < -10:
        return f"低仰角（镜头朝上{abs(int(p))}°）"
    if p <= 10:
        return "水平视角"
    if p <= 40:
        return f"低俯角（镜头朝下{int(p)}°）"
    return f"大俯角（镜头朝下{int(p)}°）"


def _camera_pitch_rule(p) -> str:
    """镜头俯仰角 → 生图/生视频模型能执行的构图规则（空字符串=水平无需特别约束）。"""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return ""
    if -10 <= p <= 10:
        return ""
    if p > 40:
        return (f"镜头朝下俯约{int(p)}°（大俯角）：接近俯视机位，人物头顶明显可见、身形被压缩，"
                "地面与地面陈设占据画面主体，地平线移出画面或位于画面最上缘，严禁出现仰视或平视构图。")
    if p > 10:
        return (f"镜头朝下俯约{int(p)}°（带俯角）：画面整体向下倾斜，地平线明显上移、地面占比增大，"
                "人物略被俯视但仍有正常身形比例。")
    if p > -40:
        return (f"镜头朝上仰约{abs(int(p))}°（带仰角）：画面整体向上倾斜，地平线明显下移、"
                "天空/天花板入画增多，人物显得比实际更高大。")
    return (f"镜头朝上仰约{abs(int(p))}°（大仰角）：强烈仰视机位，人物高耸有压迫感，"
            "背景以天空或天花板为主，地面基本不可见，严禁出现俯视或平视构图。")


def _facing_scene_anchor(plot: dict, px: float, py: float, pf: float, self_name: str, cam: dict) -> str:
    """人物朝向的【场景内锚点】：他面朝方向锥内（陈设±55°/人物±45°）最近的门/家具/其他人物。
    实测教训：朝向只写镜头相对描述（"正面对镜头"）时，生图模型的"人要正脸朝观众"先验
    会压过文字——给画面里的具体目标（面朝「门」/面朝「发小」）比抽象角度服从度高得多。
    目标在机位视野外时同样要写（他面朝画面外本身就是构图事实），并注明"在本画面之外"。"""
    import math
    prad = math.radians(pf % 360)
    pfx, pfy = math.sin(prad), -math.cos(prad)
    cx, cy = float(cam.get("x", 0.5)), float(cam.get("y", 0.95))
    crad = math.radians(float(cam.get("facing") or 0) % 360)
    cfx, cfy = math.sin(crad), -math.cos(crad)
    fov = float(cam.get("fov") or 55)

    def in_view(tx: float, ty: float) -> bool:
        vx, vy = tx - cx, ty - cy
        vd = math.hypot(vx, vy)
        if vd < 0.15:
            return False
        dep = vx * cfx + vy * cfy
        if dep <= 0:
            return False
        return math.degrees(math.acos(max(-1.0, min(1.0, dep / vd)))) <= fov / 2 + 25

    best = None  # (角度, 是否陈设, 标签, 是否在视野内)
    for p in (plot.get("props") or []):
        label = str(p.get("label") or p.get("name") or "").strip()
        if not label or p.get("x") is None or p.get("y") is None:
            continue
        try:
            tx, ty = float(p["x"]) - px, float(p["y"]) - py
        except (TypeError, ValueError):
            continue
        d = math.hypot(tx, ty)
        if d < 0.05:   # 人物就贴在该陈设上（坐在椅子/床沿），朝向锚无意义
            continue
        ang = math.degrees(math.acos(max(-1.0, min(1.0, (pfx * tx + pfy * ty) / d))))
        if ang > 55:
            continue
        cand = (ang, 1, label, in_view(float(p["x"]), float(p["y"])))
        if best is None or cand[0] < best[0] - 5 or (abs(cand[0] - best[0]) <= 5 and cand[1] > best[1]):
            best = cand
    for c in (plot.get("characters") or []):
        nm = str(c.get("name") or "").strip()
        if not nm or nm == self_name or c.get("x") is None or c.get("y") is None:
            continue
        try:
            tx, ty = float(c["x"]) - px, float(c["y"]) - py
        except (TypeError, ValueError):
            continue
        d = math.hypot(tx, ty)
        if d < 0.02:
            continue
        ang = math.degrees(math.acos(max(-1.0, min(1.0, (pfx * tx + pfy * ty) / d))))
        if ang > 45:
            continue
        cand = (ang, 0, nm, in_view(float(c["x"]), float(c["y"])))
        if best is None or cand[0] < best[0] - 5 or (abs(cand[0] - best[0]) <= 5 and cand[1] > best[1]):
            best = cand
    if not best:
        return ""
    _, is_prop, label, iv = best
    if is_prop:
        return f"面朝「{label}」" + ("" if iv else "（该目标在本画面之外，他正朝画面外看）")
    return f"面朝「{label}」（正对着他/她）" + ("" if iv else "（该目标在本画面之外）")


def compute_camera_facts(plot: dict) -> dict:
    """【唯一几何真相源】把调度图坐标一次性算成机位视野的结构化事实。
    机位视野摘要、坐标翻译、AI 镜头翻译全部从这里取数——只算一次、口径只有一套，
    杜绝多处各算一套、补丁打不穷尽的问题。
    关键口径：透视成像大小 ∝ 1/沿视线投影深度 depth = vx*fx + vy*fy（不除以距离——
    除以距离就退化成视线夹角余弦，站在机位正前方的人无论多远都会被误判成"最近/最大"）。"""
    import math
    cam = plot.get("camera") or {}
    cx, cy = float(cam.get("x", 0.5)), float(cam.get("y", 0.95))
    facing = float(cam.get("facing") or 0) % 360
    fov = float(cam.get("fov") or 55)
    rad = math.radians(facing)
    fx, fy = math.sin(rad), -math.cos(rad)
    dir_desc = {0: "图纸上方", 45: "图纸右上方", 90: "图纸右方", 135: "图纸右下方",
                180: "图纸下方", 225: "图纸左下方", 270: "图纸左方", 315: "图纸左上方"}
    dir_name = dir_desc[min(((abs((facing % 360) - k), k) for k in dir_desc))[1]]

    visible, hidden = [], []
    for c in plot.get("characters") or []:
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        px, py = float(c.get("x", 0)), float(c.get("y", 0))
        vx, vy = px - cx, py - cy
        dist = math.hypot(vx, vy)
        pf = float(c.get("facing") or 0) % 360
        facing_desc = _facing_relative_to_camera(pf, vx, vy, dist, rad)
        # 场景内朝向锚点：面朝「门」/面朝「某人」优先于镜头相对朝向（压模型的"正脸朝观众"先验）
        _fa = _facing_scene_anchor(plot, px, py, pf, name, cam)
        if _fa:
            try:
                _tgt = _fa.split("「")[1].split("」")[0]
                facing_desc = facing_desc.replace("正脸朝向观众", f"正脸朝向「{_tgt}」") \
                                         .replace("脸大部分朝向观众", f"脸大部分朝向「{_tgt}」")
            except Exception:
                pass
            facing_desc = f"{_fa}，{facing_desc}"
        if dist < 1e-6:
            hidden.append({"name": name, "reason": "就站在机位上（视角人物，镜头从他眼中拍出）",
                           "facing_desc": facing_desc, "side": "", "plan_dir": "", "angle": 0.0})
            continue
        depth = vx * fx + vy * fy          # 沿视线方向投影深度：透视成像 ∝ 1/depth
        dot_norm = depth / dist            # 视线夹角余弦（仅用于算视偏角，不当远近用）
        angle = math.degrees(math.acos(max(-1.0, min(1.0, dot_norm))))
        cross = fx * vy - fy * vx
        side = _frame_side_text(cross, dist, fov)
        plan_dir = dir_desc[min(((abs(pf - k), k) for k in dir_desc))[1]].replace("图纸", "面朝图纸")
        if dist <= 0.15:
            hidden.append({"name": name, "reason": f"紧贴机位（就在镜头旁边，不入画）",
                           "facing_desc": facing_desc, "side": side, "plan_dir": plan_dir, "angle": angle})
        elif depth <= 0:
            hidden.append({"name": name, "reason": "在机位身后（镜头后方，不入画）",
                           "facing_desc": facing_desc, "side": side, "plan_dir": plan_dir, "angle": angle})
        elif angle > fov / 2 + 25:
            hidden.append({"name": name, "reason": "在机位视野范围之外（不入画）",
                           "facing_desc": facing_desc, "side": side, "plan_dir": plan_dir, "angle": angle})
        else:
            visible.append({"name": name, "dist": dist, "depth": depth, "angle": angle, "side": side,
                            "facing_desc": facing_desc, "plan_dir": plan_dir,
                            "near_edge": angle > fov / 2 + 10})
    # 升序：投影深度最小（离镜头最近、画面里最大）在前
    visible.sort(key=lambda c: c["depth"])
    d0 = visible[0]["depth"] if visible else 0.0
    for i, c in enumerate(visible):
        if i == 0 or d0 <= 1e-6:
            c["size_pct"] = 100
        else:
            # 画面大小 ∝ 1/depth：相对最近者的大小 = d0 / depth
            c["size_pct"] = max(int((d0 / max(c["depth"], 1e-6) * 100) // 5 * 5), 5)
    return {"camera": {"x": cx, "y": cy, "facing": facing, "fov": fov, "dir_name": dir_name,
                       "height": cam.get("height"), "height_desc": _camera_height_desc(cam.get("height")),
                       "height_rule": _camera_height_rule(cam.get("height")),
                       "pitch": cam.get("pitch"), "pitch_desc": _camera_pitch_desc(cam.get("pitch")),
                       "pitch_rule": _camera_pitch_rule(cam.get("pitch"))},
            "visible": visible, "hidden": hidden}


def _camera_view_summary(plot: dict) -> str:
    """按坐标几何计算机位视野：谁在镜头前方（入画，近→远）、谁在机位旁/镜头后方（绝不入画）。
    这是权威事实，直接注入分镜生图约束，杜绝"从主角位置拍过去"的反向理解。
    全部几何结论取自唯一真相源 compute_camera_facts。"""
    facts = compute_camera_facts(plot)
    cam = facts["camera"]
    visible, hidden = facts["visible"], facts["hidden"]
    lines = [f"镜头架在「机位」符号处、朝{cam['dir_name']}方向拍摄（视锥约{int(cam['fov'])}°）——画面就是从机位望出去的景象。"]
    if cam.get("height_rule"):
        lines.append(cam["height_rule"])
    if cam.get("pitch_rule"):
        lines.append(cam["pitch_rule"])
    if visible:
        n = max(len(visible) - 1, 1)
        seg = []
        for i, c in enumerate(visible):
            size_txt = ("画面里最大（高矮基准，画面中从脚到头顶最高）" if i == 0
                        else f"站在同一地面时，画面中从脚到头顶的高度约为最近者的{c['size_pct']}%（明显更矮小，这是远近透视，不是年龄小）")
            depth = "离镜头最近" if i == 0 else ("离镜头最远" if i == n else f"离镜头第{i+1}近")
            vert = "位于画面下缘" if i == 0 else ("靠近画面上缘地平线" if i == n else "位于画面纵深中部")
            edge = "，靠近视野边缘可能被裁切" if c["near_edge"] else ""
            seg.append(f"{c['name']}（{vert}、{depth}、{size_txt}、{c['side']}、{c['facing_desc']}{edge}）")
        lines.append("机位前方视野内、会出现在画面中（从近到远）：" + "、".join(seg) + "。"
                     + "此高度比例由程序按沿视线方向的真实深度透视换算：百分比相同的人在画面中一样高，相差≥20%时高矮差异必须明显可辨。"
                     + "【年龄身高锁定】近大远小只影响画面占比，绝不改变角色自身的年龄与身高设定——"
                     + "提示词描述为青年/成人的角色严禁画成小孩或儿童身材，角色的外貌年龄严格按各自的外貌描述与角色参考图执行。")
    else:
        lines.append("机位前方视野内没有人物（画面以环境空镜为主）。")
    if hidden:
        lines.append("以下人物站在机位上/机位旁/镜头后方或视野之外，绝不能出现在画面中：" +
                     "；".join(f"{c['name']}（{c['reason']}）" for c in hidden) + "。")
    lines.append("此名单由程序按调度图纸坐标计算，是权威事实：机位的位置与朝向、谁入画谁不入画、每人的画面左右与身体朝向都以本事实为准；"
                 "导演口述文字若与图纸「机位」符号不符（例如导演手动移过机位后口述仍写旧位置），口述中的机位描述作废，按本事实执行。")
    return "".join(lines)


_INDOOR_MARK = re.compile(r"屋内|屋里|室内|殿内|帐内|舱内|房内|堂内|洞内|宫内|府内|店内|馆内|轿内|船上|织机|灶|炕")
_OUTDOOR_MARK = re.compile(r"屋外|室外|庭院|院中|院里|旷野|山野|草原|沙漠|街上|街头|城头|城楼|桥上|田野|田间|林中|江边|湖边|海边|雪地")

# FACS AU 编码（AU4 / AU4+AU20+AU7 / AU1、AU7、AU4）是给表格和速查用的，
# 生图/生视频模型不认识这些 token，混在正文里只会干扰画面（实测会把脸画扭曲）。
# 发出去之前一律剥掉，情绪语义由紧挨着的中文词（焦急/强忍/震惊…）承担。
_AU_CODE_RE = re.compile(r"[（(]?\s*AU\s*\d+\s*(?:[+＋、,，/＆&]\s*AU\s*\d+\s*)*[)）]?", re.I)


def _clean_au_codes(text: str) -> str:
    """剥掉提示词里的 FACS AU 编码并清理被剥空的标点/空格。"""
    if not text or "AU" not in text.upper():
        return text
    out = _AU_CODE_RE.sub("", text)
    # 剥完后可能留下 \"，。，\" \"（）\" 这类空壳或连续标点，压一遍
    out = re.sub(r"[，,；;、]{2,}", "，", out)
    out = re.sub(r"（\s*）|\(\s*\)", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([，。；、！？])", r"\1", out)
    return out.strip()


# ---------- 角色设定图提示词 → 纯外貌摘要 ----------
# 角色的 image_prompt 是"画一张角色设定图"用的：里面必然包含单人全身立绘、正面站姿、
# 目视镜头、纯色浅背景、柔和均匀打光、8K/电影级景深这类**设定图专用**要求。
# 把这段原样当"外貌锚点"塞给分镜生图，模型会照着画成纯色背景的立绘/证件照，
# 而不是分镜画面——这是分镜崩坏最主要的一条。这里按分句过滤：
# 只要外貌与服装（年龄/脸/发/眼/眉/身形/肤色/衣/甲/饰…），丢掉构图与画质类描述。
_PORTRAIT_NOISE = re.compile(
    r"立绘|全身|半身|完整入画|入画|头顶到脚踝|脚踝|四分之三|正面|侧面|背面|朝向|"
    r"纯色|背景|打光|布光|站姿|坐姿|姿态|目视|看镜头|正视|构图|取景|画幅|画布|"
    r"超高清|高清|分辨率|像素|电影级|景深|颗粒|体积光|质感|渲染|卡通化|写实|画风|风格|色调|色系|"
    r"光照|光源|明暗|对比度|饱和度|曝光|镜头|焦段|视角|机位|版式|竖版|横版|比例|留白|居中|对称|"
    r"细节丰富|细节拉满|清晰|锐利|干净|柔和|均匀|一致|统一|高质量|杰作"
)
_PORTRAIT_KEEP = re.compile(
    r"岁|年龄|女子|男子|少女|少年|青年|中年|老年|孩童|"
    r"脸|面|发|髻|辫|胡|须|眼|眉|鼻|唇|齿|瞳|肤|额|颧|下巴|颈|耳|"
    r"身|高|体|肩|腰|腿|臂|手|脚|指|"
    r"穿|着|衣|裙|裤|衫|襦|袄|袍|褂|甲|铠|胄|鞋|靴|袜|帽|冠|巾|披|斗篷|带|腰带|饰|簪|佩|挂|纹样|布料|麻|丝|棉|皮"
)
# 服装/配饰类词：判定"这段分句讲的是穿什么"，正文已写明服装时用它把锚点里的服装剔掉
_OUTFIT_WORD = re.compile(
    r"穿|身着|上着|下着|下穿|外罩|内穿|衣|裙|裤|衫|襦|袄|袍|褂|甲|铠|胄|短褐|鞋|靴|袜|帽|冠|"
    r"头巾|布巾|腰带|系带|绳带|披风|斗篷|短刀|佩|挂|护身符|饰"
)


def _portrait_only(text: str, keep_outfit: bool = True) -> str:
    """从设定图提示词里只取外貌与服装分句，丢掉构图/站姿/背景/画质类要求。
    keep_outfit=False 时连服装分句一起去掉——分镜正文已经写明了穿什么的时候，
    锚点里再写另一套（角色卡常是铠甲造型）就会跟正文打架，模型随机挑一套画。"""
    s = (text or "").strip()
    if not s:
        return ""
    parts = re.split(r"(?<=[，。；、,;])", s)
    keep = []
    for p in parts:
        t = p.strip(" \t，。；、,;")
        if not t:
            continue
        if _PORTRAIT_NOISE.search(t) or not _PORTRAIT_KEEP.search(t):
            continue
        if not keep_outfit and _OUTFIT_WORD.search(t):
            continue
        keep.append(t)
    out = "，".join(keep)
    return out[:220]


def _visible_character_names(plot: dict) -> list[str] | None:
    """按机位几何计算本镜头"入画人物名单"（取自唯一真相源 compute_camera_facts）。
    无有效 plot 数据时返回 None（表示不过滤）。视角人物（站在机位上）不入画。"""
    if not isinstance(plot, dict):
        return None
    chars = [c for c in (plot.get("characters") or []) if str(c.get("name") or "").strip()]
    if not chars:
        return None
    return [c["name"] for c in compute_camera_facts(plot)["visible"]]


def _facing_relative_to_camera(pf: float, vx: float, vy: float, dist: float, cam_rad: float) -> str:
    """把人物在图纸上的朝向（0=图纸上方、顺时针）换算成生图模型听得懂的镜头相对朝向。
    六档量化（阈值按与机位视线夹角）：正面 / 四分之三面向 / 侧面 / 四分之三背对 / 完全背对。
    细档的意义：±60°内一律叫"面向镜头"时，实际已接近纯侧面的人物会被模型画成正脸——实测朝向不敏感的主因。"""
    import math
    if dist <= 1e-6:
        return "视角人物（镜头从他眼中拍出）"
    prad = math.radians(pf % 360)
    pfx, pfy = math.sin(prad), -math.cos(prad)  # 人物面朝方向（图纸坐标）
    to_cam = (-vx / dist, -vy / dist)           # 人物→机位方向（看向镜头的方向）
    dot = pfx * to_cam[0] + pfy * to_cam[1]
    # 带上具体偏差角度：六档量化之内还有细微差别（偏5°和偏30°同属一档，
    # 但画面上一个是几乎正脸、一个接近侧脸），模型按角度画得更准
    _a = math.degrees(math.acos(max(-1.0, min(1.0, dot))))  # 与"看向镜头"方向的夹角
    if dot >= 0.82:      # 与视线夹角 ≤ 约35°
        return f"正面对镜头（正脸朝向观众，脸完整可见，视线与镜头方向仅偏约{int(_a)}°）"
    if dot >= 0.45:      # 约35°~63°
        return f"四分之三面向镜头（斜侧身，脸大部分朝向观众、双眼可见，视线与镜头方向偏约{int(_a)}°——度数越小越接近正脸）"
    if dot <= -0.82:     # 背离视线 ≤ 约35°
        return f"完全背对镜头（只见后脑勺与后背，脸完全不可见，背离镜头视线约{int(180 - _a)}°）"
    if dot <= -0.45:     # 背离视线 约35°~63°
        return f"四分之三背对镜头（大半背影，脸部几乎不可见，背离镜头视线约{int(180 - _a)}°——度数越小越接近完全背影）"
    # 侧对：机位视线右侧在图纸上的方向向量（rad=0 朝上拍时=图纸右方 (1,0)）
    rx, ry = math.cos(cam_rad), math.sin(cam_rad)
    side_dot = pfx * rx + pfy * ry
    return "侧对镜头、面朝画面右（观众看到其右侧脸）" if side_dot > 0 else "侧对镜头、面朝画面左（观众看到其左侧脸）"


_FACING_VISUAL_RULE = [
    # (识别子串, 给生图模型的硬性视觉要求)  顺序敏感：先长串后短串
    ("完全背对镜头", "画面中只能出现其后脑勺与后背，严禁画出他的脸、正脸或侧脸"),
    ("四分之三背对镜头", "画面以背影为主，脸部几乎不可见，严禁画出正脸"),
    ("正面对镜头", "必须画出正脸、双眼朝向观众"),
    ("四分之三面向镜头", "画成斜侧身，脸大部分朝向观众、双眼可见"),
    ("侧对镜头", "画成侧面剪影，按写明的画面左右只露出对应的侧脸"),
]


def _facing_rule_text(facing_desc: str) -> str:
    """朝向描述 → 对应的硬性视觉要求短语（提示词里逐人跟在朝向后，压权重）。
    描述里带场景内面朝锚点（面朝「门」）时，"朝向观众"一律改成朝向该目标——
    否则档位规则（脸朝向观众）与锚点（脸朝门）互相打架，模型随机挑一个（实测漂移根源）。"""
    for key, rule in _FACING_VISUAL_RULE:
        if key in facing_desc:
            if "面朝「" in facing_desc:
                try:
                    tgt = facing_desc.split("面朝「", 1)[1].split("」", 1)[0]
                    rule = rule.replace("双眼朝向观众", f"双眼朝向「{tgt}」").replace("脸大部分朝向观众", f"脸大部分朝向「{tgt}」")
                except Exception:
                    pass
            return rule
    return "按写明的朝向执行"


def _facing_brief(desc: str) -> str:
    """朝向描述去掉括号内补充说明（硬约束行里已有视觉要求，避免双重括号重复）。"""
    i = desc.find("（")
    return desc[:i] if i > 0 else desc


def _hidden_props_note(plot: dict) -> str:
    """机位视野外的门窗/家具黑名单 → 参考图取舍说明。
    实测教训：场景设定图的参考说明写着"环境、陈设必须与此图一致"，图片的说服力压过
    提示词文字——调度图里明明把门摆在机位身后（视野外），模型仍从场景设定图把门抄进
    画面右缘。在场景参考说明里明确"位置取舍以调度图纸为准"并列出视野外陈设黑名单。"""
    import math
    cam = plot.get("camera") or {}
    if not isinstance(cam, dict) or "x" not in cam:
        return ""
    cx, cy = float(cam.get("x", 0.5)), float(cam.get("y", 0.95))
    rad = math.radians(float(cam.get("facing") or 0) % 360)
    fx, fy = math.sin(rad), -math.cos(rad)
    fov = float(cam.get("fov") or 55)
    hidden = []
    for p in (plot.get("props") or []):
        label = str(p.get("label") or p.get("name") or "").strip()
        if not label or not isinstance(p.get("x"), (int, float)):
            continue
        vx, vy = float(p["x"]) - cx, float(p["y"]) - cy
        dist = math.hypot(vx, vy)
        if dist < 1e-6:
            hidden.append(label)
            continue
        depth = vx * fx + vy * fy
        try:
            angle = math.degrees(math.acos(max(-1.0, min(1.0, depth / dist))))
        except ValueError:
            angle = 180.0
        if depth <= 0 or dist <= 0.15 or angle > fov / 2 + 25:
            hidden.append(label)
    if not hidden:
        return ""
    return ("【陈设取舍·权威】以下陈设在本镜机位视野之外，画面中严禁出现（即使场景设定图里有也必须删掉）："
            + "、".join(f"「{h}」" for h in hidden)
            + "。场景设定图只提供材质、样式与光线氛围参考；所有陈设的画面位置与取舍一律以调度图纸机位几何为准，"
              "两者冲突时以调度图纸为准。")


def _prop_frame_lines(plot: dict) -> str:
    """把门窗/家具陈设按机位几何翻译成"画面落位"（借鉴 compute_camera_facts 的人物口径）：
    画面左右按「站在机位处朝拍摄方向看」换算（机位视线左侧＝画面左侧，与图纸方位无关），
    按沿视线投影深度排远近，视野外/机位身后的陈设明确标不入画。
    2D 原始逻辑只翻译陈设朝向（format_plot_to_prompt 的陈设朝向行），这里补落位——
    门窗/大件家具在画面中的位置直接决定构图，实测模型自己换算不了。"""
    import math
    cam = plot.get("camera") or {}
    if not isinstance(cam, dict) or "x" not in cam:
        return ""
    cx, cy = float(cam.get("x", 0.5)), float(cam.get("y", 0.95))
    facing = float(cam.get("facing") or 0) % 360
    fov = float(cam.get("fov") or 55)
    rad = math.radians(facing)
    fx, fy = math.sin(rad), -math.cos(rad)
    items = []
    for p in (plot.get("props") or []):
        label = str(p.get("label") or p.get("name") or "").strip()
        if not label or not isinstance(p.get("x"), (int, float)):
            continue
        px, py = float(p["x"]), float(p["y"])
        vx, vy = px - cx, py - cy
        dist = math.hypot(vx, vy)
        if dist < 1e-6:
            continue
        depth = vx * fx + vy * fy
        dot_norm = depth / dist
        angle = math.degrees(math.acos(max(-1.0, min(1.0, dot_norm))))
        cross = fx * vy - fy * vx
        if depth <= 0:
            items.append((depth, f"「{label}」在机位身后（不出现在画面中）"))
        elif dist <= 0.15:
            items.append((depth, f"「{label}」紧贴机位（就在镜头旁边，基本不入画）"))
        elif angle > fov / 2 + 25:
            items.append((depth, f"「{label}」在机位视野范围之外（不出现在画面中）"))
        else:
            side = _frame_side_text(cross, dist, fov)
            edge = "、靠近视野边缘可能被画面裁切" if angle > fov / 2 + 10 else ""
            items.append((depth, f"「{label}」在画面中的{side}{edge}"))
    if not items:
        return ""
    items.sort(key=lambda t: t[0])   # 按投影深度升序＝从近到远
    n = len(items)
    out = []
    for i, (_, txt) in enumerate(items):
        # 视野内的才标远近序次（出画的标远近没有意义）
        if "不出现在画面中" in txt or "基本不入画" in txt:
            out.append(txt)
        else:
            rank = "离镜头最近" if i == 0 else ("离镜头最远" if i == n - 1 else f"离镜头第{i + 1}近")
            out.append(txt.replace("在画面中的", f"（{rank}）在画面中的"))
    return ("【陈设落位·机位相对】门窗/家具等陈设在画面中的左右与远近已按机位朝向换算"
            "（机位视线左侧＝画面左侧，与图纸方位无关），人物动作与陈设的相对位置据此执行："
            + "；".join(out) + "。")


def _char_prop_relations(plot: dict, facts: dict) -> str:
    """人物与视野内大件陈设（织布机/门/衣柜等）的前后关系，逐人说破。
    实测教训："木兰在织布机前面"这类空间关系靠模型从各自独立的落位描述里自己推，
    十有八九漂移（人画到陈设后面/穿模）。直接把谁在谁的靠镜头一侧写成权威事实。"""
    import math
    cam = plot.get("camera") or {}
    if not isinstance(cam, dict) or "x" not in cam:
        return ""
    cx, cy = float(cam.get("x", 0.5)), float(cam.get("y", 0.95))
    rad = math.radians(float(cam.get("facing") or 0) % 360)
    fx, fy = math.sin(rad), -math.cos(rad)
    fov = float(cam.get("fov") or 55)
    # 视野内陈设的投影深度（与 _prop_frame_lines 同一套口径）
    props_vis = []
    for p in (plot.get("props") or []):
        label = str(p.get("label") or p.get("name") or "").strip()
        if not label or not isinstance(p.get("x"), (int, float)):
            continue
        vx, vy = float(p["x"]) - cx, float(p["y"]) - cy
        dist = math.hypot(vx, vy)
        if dist < 1e-6:
            continue
        depth = vx * fx + vy * fy
        try:
            angle = math.degrees(math.acos(max(-1.0, min(1.0, depth / dist))))
        except ValueError:
            continue
        if depth > 0 and angle <= fov / 2 + 25:
            props_vis.append((label, depth))
    if not props_vis:
        return ""
    lines = []
    for c in facts["visible"]:
        cd = c["depth"]
        near = [lbl for lbl, d in props_vis if cd < d]    # 人物比陈设更近镜头
        far = [lbl for lbl, d in props_vis if cd >= d]    # 陈设比人物更近镜头
        seg = []
        for lbl in near[:2]:
            seg.append(f"{c['name']}在「{lbl}」靠镜头一侧（{lbl}在{c['name']}身后）")
        for lbl in far[:2]:
            seg.append(f"{c['name']}在「{lbl}」远离镜头一侧（{lbl}在{c['name']}与镜头之间）")
        if seg:
            lines.append("；".join(seg))
    if not lines:
        return ""
    return ("【人物与陈设前后关系·权威】" + "；".join(lines) + "。"
            "前后遮挡关系按此执行：靠镜头一侧的人物画在陈设前面（陈设被人物部分遮挡或位于其身后），"
            "严禁把人物画到陈设后面去。")


def _spatial_lock_lines(plot: dict, facts: dict) -> str:
    """空间大小比例关系全量锁定（米制）。图纸 0-1 归一化坐标按房间真实尺寸（plot.room，
    缺省 10×10 米）换算（与 3D 导演台同一换算）。人物间距、人物与陈设距离、
    人物离墙距离、陈设占地尺寸全部换算成米写进提示词——这些比例是权威事实，
    生图模型不得自行压缩或夸大空间，画面中的大小/间距/离墙比例按米数与透视执行。"""
    import math
    rw, rh = _room_dims(plot)
    cam = plot.get("camera") or {}
    if not isinstance(cam, dict) or "x" not in cam:
        return ""
    rad = math.radians(float(cam.get("facing") or 0) % 360)
    fx, fy = math.sin(rad), -math.cos(rad)
    fov = float(cam.get("fov") or 55)
    cx, cy = float(cam.get("x", 0.5)), float(cam.get("y", 0.95))

    def _in_view(px, py):
        vx, vy = px - cx, py - cy
        dist = math.hypot(vx, vy)
        if dist < 1e-6:
            return False
        depth = vx * fx + vy * fy
        try:
            angle = math.degrees(math.acos(max(-1.0, min(1.0, depth / dist))))
        except ValueError:
            return False
        return depth > 0 and angle <= fov / 2 + 25

    chars = [c for c in facts["visible"]]
    props_vis = [(str(p.get("label") or p.get("name") or "").strip(), float(p["x"]), float(p["y"]),
                  float(p.get("w") or 0.15), float(p.get("h") or 0.15))
                 for p in (plot.get("props") or [])
                 if isinstance(p.get("x"), (int, float)) and str(p.get("label") or p.get("name") or "").strip()
                 and _in_view(float(p["x"]), float(p["y"]))]
    if not chars and not props_vis:
        return ""
    lines = []
    for c in chars:
        px, py = float(c.get("_x", 0)) if False else 0, 0   # placeholder（不用）
    # 人物相关：间距/离陈设/离墙（facts.visible 没带坐标，从 plot 反查）
    char_xy = []
    for c in (plot.get("characters") or []):
        name = str(c.get("name") or "").strip()
        if not name or not isinstance(c.get("x"), (int, float)):
            continue
        if any(v["name"] == name for v in facts["visible"]):
            char_xy.append((name, float(c["x"]), float(c["y"])))
    for name, px, py in char_xy:
        seg = []
        for oname, ox, oy in char_xy:
            if oname == name:
                continue
            d = math.hypot((px - ox) * rw, (py - oy) * rh)
            seg.append(f"与{oname}相距约{d:.1f}米")
        for lbl, ox, oy, _w, _h in props_vis:
            d = math.hypot((px - ox) * rw, (py - oy) * rh)
            seg.append(f"离「{lbl}」约{d:.1f}米")
        # 离墙：左(px*rw)、右((1-px)*rw)、空间深处(py*rh)、镜头侧((1-py)*rh)，取最近的一堵
        walls = [("左墙", px * rw), ("右墙", (1 - px) * rw), ("空间深处的墙", py * rh), ("镜头侧的墙", (1 - py) * rh)]
        wn, wd = min(walls, key=lambda t: t[1])
        seg.append(f"离{wn}约{wd:.1f}米")
        if seg:
            lines.append(f"{name}：" + "、".join(seg))
    for lbl, _ox, _oy, w, h in props_vis:
        lines.append(f"「{lbl}」占地约{w * rw:.1f}米×{h * rh:.1f}米")
    if not lines:
        return ""
    return (f"【空间比例锁定·米制（权威）】图纸整个平面＝{rw:g}米×{rh:g}米的真实空间（下同，所有距离均为真实米数，"
            "人物身高按约1.7米等比换算）：\n- " + "\n- ".join(lines) +
            "\n画面中人物之间、人物与陈设的相对大小与间距比例，人物在空间里的宽窄远近关系，严格按上述米数与近大远小透视执行："
            "间距近的必须画得贴近，间距远的必须拉开距离；陈设占地大的画大、占地小的画小；"
            "不得压缩或夸大空间，不得为了让人物显眼而改变真实比例关系。")


def format_plot_to_prompt(plot: dict, dynamic: bool = False) -> str:
    """把调度图精准坐标数据翻译成生图/生视频模型听得懂的自然语言（注入 prompt 尾部）。
    生图模型读不懂白底俯视图上的圆点与扇形，但读得懂"谁离镜头最近、在画面哪一侧"。
    dynamic=False（分镜图）：名单描述初始帧＝全部画面，视野外角色绝对禁足；
    dynamic=True（生视频）：名单只约束0秒初始帧，之后谁入画由走位与镜头运动推演——
    走向镜头外的人、镜头跟随都可能让视野外的人自然入画，不能一刀切禁足。
    几何结论全部取自唯一真相源 compute_camera_facts（本函数是程序直译兜底；
    正常路径走 synthesize_shot_brief 的 AI 镜头翻译）。"""
    import math
    if not isinstance(plot, dict):
        return ""
    facts = compute_camera_facts(plot)
    chars = [c for c in (plot.get("characters") or []) if str(c.get("name") or "").strip()]
    if not chars and not plot.get("camera"):
        return ""
    cam = facts["camera"]
    dir_name = cam["dir_name"]

    lines = [f"【强制机位】镜头架在「机位」符号处，朝{dir_name}方向拍摄，视锥约{int(cam['fov'])}°。画面＝从机位望出去的景象。"]
    if cam.get("height_rule"):
        lines.append("【机位高度·拍摄角度】" + cam["height_rule"])
    if cam.get("pitch_rule"):
        lines.append("【镜头夹角·俯仰】" + cam["pitch_rule"])

    # 陈设朝向（导演在 3D 导演台转过朝向的建筑/道具）：门/床/柜朝哪直接影响画面构图
    _DIR8 = ["图纸上方", "右上", "图纸右方", "右下", "图纸下方", "左下", "图纸左方", "左上"]
    prop_dirs = []
    for p in (plot.get("props") or []):
        f = float(p.get("facing") or 0)
        if f % 360 < 1:
            continue   # 未转过的默认朝向不占提示词篇幅
        prop_dirs.append(f"「{p.get('label')}」开口/正面朝{_DIR8[int(((f % 360) + 22.5) // 45) % 8]}")
    if prop_dirs:
        lines.append("【陈设朝向】" + "；".join(prop_dirs) + "（方位＝俯视图纸上的方位，不是画面左右）。")
    # 陈设落位：门窗/家具在画面中的左右与远近按机位几何换算（与人物同一套口径）
    _pf = _prop_frame_lines(plot)
    if _pf:
        lines.append(_pf)
    # 人物与陈设前后关系："木兰在织布机前面"直接说破，不靠模型自己推（实测必漂）
    _rel = _char_prop_relations(plot, facts)
    if _rel:
        lines.append(_rel)
    # 空间比例锁定：人物间距/离陈设/离墙/陈设占地全部换算成米，禁止模型自行压缩或夸大空间
    _sp = _spatial_lock_lines(plot, facts)
    if _sp:
        lines.append(_sp)

    # 人物站位：程序直译（从近到远）。远近与大小按沿视线投影深度（透视成像 ∝ 1/深度）
    n = max(len(facts["visible"]) - 1, 1)
    seg = []
    for i, c in enumerate(facts["visible"]):
        size_txt = ("画面里最大（高矮基准，画面中从脚到头顶最高）" if i == 0
                    else f"站在同一地面时，画面中从脚到头顶的高度约为最近者的{c['size_pct']}%（明显更矮小，这是远近透视，不是年龄小）")
        depth_label = "离镜头最近" if i == 0 else ("离镜头最远" if i == n else f"离镜头第{i+1}近")
        vert = "位于画面下缘" if i == 0 else ("靠近画面上缘地平线" if i == n else "位于画面纵深中部")
        edge = "、靠近视野边缘可能被画面裁切" if c["near_edge"] else ""
        seg.append(f"{c['name']}（{vert}、{depth_label}、{size_txt}、{c['side']}、{c['facing_desc']}、{c['plan_dir']}{edge}）")
    if seg:
        title = "【人物站位·初始画面入画名单（0秒时刻，从近到远）】" if dynamic else "【人物站位·入画名单（从近到远）】"
        lines.append(title + "（本名单中的「画面左/右」＝成片图像里观众看到的左/右，"
                     "已按机位朝向换算，与图纸方位无关）" + "；".join(seg) + "。"
                     "此高度比例由程序按沿视线方向的真实深度透视换算：百分比相同的人在画面中一样高，相差≥20%时高矮差异必须明显可辨。"
                     "【年龄身高锁定】近大远小只影响画面占比，绝不改变角色自身的年龄与身高设定——"
                     "提示词描述为青年/成人的角色严禁画成小孩或儿童身材，角色外貌年龄严格按各自的外貌描述与角色参考图执行。")
        # 朝向单独成行、逐人硬约束（与 AI 镜头解读的强化保持一致；兜底路径同样不能弱）
        fr = "；".join(f"{c['name']}＝{_facing_brief(c['facing_desc'])}（{_facing_rule_text(c['facing_desc'])}）" for c in facts["visible"])
        lines.append("【人物朝向·逐人硬约束】" + fr + "。朝向是权威事实，与任何文字描述冲突时以本条为准。")
        lines.append("【图纸外群演】提示词正文明确写到、但图纸没有摆放的人物（宫人、侍卫、宾客等群演配角）按正文入画："
                     "安排在背景或正文指定的位置即可，不挤占名单人物的站位与景别，人数与正文一致；"
                     "正文没写的无名人影一律不画。")
    else:
        lines.append("【人物站位】机位前方视野内没有图纸人物：画面环境与人物以文字提示词为准；"
                     "提示词正文写到的人物（宫人、路人甲等群演）按正文入画，安排在画面中部或背景即可。")
    hidden = facts["hidden"]
    if hidden:
        if dynamic:
            # 视频：名单只约束初始帧。走位/镜头运动可能把视野外的人带进画面，不能一刀切禁足
            hidden_desc = []
            for m in hidden:
                d = (m["reason"].replace("（就在镜头旁边，不入画）", "（就在镜头旁边）")
                         .replace("（镜头后方，不入画）", "（镜头后方）")
                         .replace("（不入画）", ""))
                extra = f"、{m['facing_desc']}" if m.get("facing_desc") else ""
                hidden_desc.append(f"{m['name']}（初始画面不在镜头视野内：{d}{extra}）")
            lines.append("【人物站位·初始不在画面名单（0秒时刻）】" + "；".join(hidden_desc) + "。")
        else:
            lines.append("【人物站位·绝不入画名单】" + "；".join(
                f"{m['name']}（{m['reason']}{('，' + m['facing_desc']) if m.get('facing_desc') else ''}）" for m in hidden) + "。")
    if dynamic:
        lines.append("【透视规则·视频】离机位越近的人画面里越大（可为前景，可能只露腿部/肩膀/背影），越远越小、靠近地平线；"
                     "站在同一纵深位置的人物身高一致，纵深远近带来的高矮差异必须画出来；"
                     "以上两个名单描述的都是0秒初始画面的状态，不约束后续：初始在画面里的人物按上述站位出现；"
                     "初始不在画面的人物，若走位或镜头运动使其进入机位视野，则按真实透视自然走入画面（入画的就是本人，从画面边缘进来），"
                     "始终未进入视野的则全程不得出现、严禁凭空冒出。人物进出场只以走位与镜头运动为准。"
                     "每个人物的身体朝向按名单描述逐人严格执行：正面对镜头＝正脸朝向其被指定的面朝目标（未指定目标时才是朝向镜头）；四分之三面向镜头＝斜侧身、脸大部分朝向其面朝目标；侧对镜头＝只露写明的那一侧侧脸；四分之三背对镜头＝大半背影、脸部几乎不可见；完全背对镜头＝只见后脑勺与后背、严禁画出脸。被指定了面朝目标（如面朝「门」）的人，脸与身体一律朝着该目标，严禁为了让观众看清脸而擅自转向镜头。"
                     "每个角色在画面中只出现一次、只有一个身体：走位描述的是同一角色的位移，严禁画成分身、复制人或两个同名角色；"
                     "俯视调度图纸/轨迹图类参考图仅用于确定站位与走位，严禁把图纸、人偶棋子图标、扇形视锥画进画面。")
    else:
        lines.append("【透视规则】离机位越近的人画面里越大（可为前景，可能只露腿部/肩膀/背影），越远越小、靠近地平线；"
                     "站在同一纵深位置的人物身高一致，纵深远近带来的高矮差异必须画出来；"
                     "每个人物的身体朝向以上述描述为准、逐人严格执行：正面对镜头＝正脸朝向其被指定的面朝目标（未指定目标时才是朝向镜头）；四分之三面向镜头＝斜侧身、脸大部分朝向其面朝目标；侧对镜头＝只露写明的那一侧侧脸；四分之三背对镜头＝大半背影、脸部几乎不可见；完全背对镜头＝只见后脑勺与后背、严禁画出脸。被指定了面朝目标（如面朝「门」）的人，脸与身体一律朝着该目标，严禁为了让观众看清脸而擅自转向镜头；角色设定图里的正面站姿只锁外貌与服装，不决定本镜朝向。"
                     "文字提示词中的朝向描述（如'面朝XX方向''看画面的对侧''望向XX'）若与本名单冲突，一律作废、按本名单执行。"
                     "镜头就是对准入画名单里的人拍的：离镜头最近的主体应按景别呈现在镜头前方（构图围绕该主体），"
                     "写'正面对镜头/四分之三面向镜头'的主体必须让脸朝向他被指定的面朝目标（面朝「门」就看着门；未指定目标时才朝向观众），严禁把他挪到画面边缘、画成背身、或把画面取成顺着他视线望出去的方向。"
                     "上面两个名单由程序按坐标计算，是权威事实："
                     "绝不入画名单里的人禁止出现在画面中，即使文字提示词提到了他们。"
                     "每个角色在画面中只出现一次、只有一个身体：走位描述的是同一角色的位移，严禁画成分身、复制人或两个同名角色；"
                     "俯视调度图纸/轨迹图类参考图仅用于确定站位与走位，严禁把图纸、人偶棋子图标、扇形视锥画进画面。")

    # 运动路线（轨迹图）：拐点翻译成镜头相对方向（往画面哪边走、靠近/远离镜头），不甩裸坐标
    cam_facing = float(cam.get("facing") or 0)
    cx, cy = cam["x"], cam["y"]

    def _dir8(dx: float, dy: float) -> str:
        """位移向量 → 镜头相对运动方向描述（以机位视线为正前方）。"""
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return ""
        ang = (math.degrees(math.atan2(dx, -dy)) - cam_facing) % 360
        sectors = [
            (22.5, "向画面纵深走去（远离镜头）"), (67.5, "向画面右后方走（远离镜头）"),
            (112.5, "向画面右走"), (157.5, "向画面右前方走来（靠近镜头）"),
            (202.5, "朝镜头走来（靠近镜头）"), (247.5, "向画面左前方走来（靠近镜头）"),
            (292.5, "向画面左走"), (337.5, "向画面左后方走（远离镜头）"),
        ]
        for lim, txt in sectors:
            if ang < lim:
                return txt
        return "向画面纵深走去（远离镜头）"

    def _depth_trend(pts) -> str:
        """起点→终点相对机位的纵深变化。"""
        if len(pts) < 2:
            return ""
        d0 = math.hypot(pts[0][0] - cx, pts[0][1] - cy)
        d1 = math.hypot(pts[-1][0] - cx, pts[-1][1] - cy)
        if d1 < d0 - 0.05:
            return "整体逐渐靠近镜头（人物在画面里逐渐变大）"
        if d1 > d0 + 0.05:
            return "整体逐渐远离镜头（人物在画面里逐渐变小）"
        return "纵深基本持平"

    moves = []
    hidden_names = {m["name"] for m in hidden}  # 机位几何判定不入画的角色名
    for c in chars:
        path = c.get("path") or []
        if not (path and str(c.get("name")).strip()):
            continue
        pts = [[float(c.get("x") or 0), float(c.get("y") or 0)]] + [list(p)[:2] for p in path]
        segs = []
        for a, b in zip(pts, pts[1:]):
            d = _dir8(b[0] - a[0], b[1] - a[1])
            if d and (not segs or segs[-1] != d):
                segs.append(d)
        desc = str(c.get("name")).strip() + "从起点出发移动：" + "→".join(segs)
        trend = _depth_trend(pts)
        if trend:
            desc += f"，{trend}"
        stops = [s for s in (c.get("stops") or []) if isinstance(s, dict) and (str(s.get("action") or "").strip() or str(s.get("t") or "").strip())]
        if stops:
            desc += "；时间节点：" + "→".join(f"{str(s.get('t') or '').strip()}{str(s.get('action') or '').strip()}".strip() for s in stops)
        # 走位终点/时间节点点名了"不入画"角色时必须显式声明：只表示朝那个方向走，
        # 否则视频模型为了表现"走到XX附近"会把镜头外的人画进画面（实测导致大量人物崩坏）
        who = set()
        ex, ey = pts[-1]
        for o in chars:
            on = str(o.get("name") or "").strip()
            if not on or on == str(c.get("name")).strip() or on not in hidden_names:
                continue
            if math.hypot(float(o.get("x") or 0) - ex, float(o.get("y") or 0) - ey) < 0.12:
                who.add(on)
            for s in stops:
                if on in str(s.get("action") or ""):
                    who.add(on)
        if who:
            names_txt = "、".join(sorted(who))
            if dynamic:
                desc += (f"（注意：{names_txt}初始画面不在镜头视野内——上述走位只表示朝{names_txt}所在的方向走去，"
                         f"若走位或镜头运动使其进入视野则按真实透视自然入画（入画即本人，从画面边缘进来），"
                         f"始终未进入视野则不得凭空出现）")
            else:
                desc += (f"（注意：{names_txt}按机位视野计算不在画面内——上述走位只表示朝{names_txt}所在的方向走去，"
                         f"走到画面边缘或纵深方向即可，{names_txt}本人不得出现在画面中）")
        moves.append(desc)
    cam_path = cam.get("path") or []
    if cam_path:
        cam_pts = [[float(cam.get("x") or 0), float(cam.get("y") or 0)]] + [list(p)[:2] for p in cam_path]
        d = _dir8(cam_pts[-1][0] - cam_pts[0][0], cam_pts[-1][1] - cam_pts[0][1])
        moves.append(f"机位{d or '移动'}（{(cam.get('movement') or '移动机位')}）")
    if moves:
        lines.append("【运动路线】" + "；".join(moves) + "。运动要平滑连续，走位方向与快慢以上述路线为准。")
    return "\n".join(lines) + "\n"


# ---------- 2.70 顶层镜头翻译：坐标几何（程序）+ 画面语言（DeepSeek）一次性产出统一 JSON ----------
# 设计动机：不再零打碎敲地给"距离/大小/左右"公式打补丁——程序只算数学上不会错的硬事实
# （谁入画、投影深度次序、大小百分比、画面左右、身体朝向），DeepSeek 负责把这些事实
# 翻译成生图/生视频模型听得懂的"镜头画面 JSON"（景别、构图、遮挡、间距、布局描述）。
# 程序对返回 JSON 做硬校验：几何结论与程序事实冲突处一律以程序为准；调用失败自动回退程序直译。

_SHOT_BRIEF_CACHE: dict[str, dict] = {}   # key = sha1(plot+画面描述+dynamic) → 校验后的 brief JSON

SHOT_TRANSLATE_SYSTEM_PROMPT = """你是资深摄影指导兼镜头翻译官。输入有两部分：
①调度图纸的坐标数据（俯视图，0-1 归一化坐标：x 右为正、y 下为正；camera=机位位置/朝向/视锥，facing 0=图纸上方、90=右方、180=下方、270=左方；characters=人物位置/朝向/轨迹；props=陈设，含朝向 facing：0=图纸上方、顺时针，如门/床/柜的开口朝向）；
②程序按坐标算好的机位几何事实（visible=入画名单已按离镜头由近到远排序，含每人的 depth_rank 近远次序、size_percent 画面大小百分比、frame_side 画面左右、facing 身体朝向、near_edge 是否贴近视野边缘；hidden=绝不入画名单）。

几何事实是权威，不可更改：谁入画谁不入画、近远次序、大小百分比、画面左右、身体朝向一律照抄，禁止用你的直觉重算或调换。
你只做"画面语言化"——把事实翻译成文生图/图生视频模型听得懂的画面描述：
1. shot：按画面描述推断景别（特写/近景/中景/全景/远景）；机位高度与镜头夹角必须照抄输入 camera.height_desc 与 camera.pitch_desc（程序按导演设定的机位高度与镜头俯仰换算，如"平拍（与人眼等高）""大俯角（镜头朝下45°）"），禁止自行推断；再加一句话构图。
2. characters：给每个入画人物写 frame_position（按 depth_rank：最近者"画面下缘/前景"，最远者"画面上缘地平线/远景深处"，中间者"画面纵深中部"，再叠加 frame_side 的左右）、depth_label（前景/中景/背景）、occlusion（按坐标间距判断谁可能被谁部分遮挡，没有就空串）、spacing（与其他人物的间距语义，如"与小美并排、间隔约一步""独自远离人群半个画面"）、presentation（结合画面描述写这个人物在镜头里的一句话呈现：姿势/在做什么/情绪，服从画面描述，不新增剧情；并把该人物的 facing 落成具体可视描述写进 presentation，例如"背对镜头"→"只见后脑勺与背影"、"侧对镜头"→"侧脸朝向画面右"）。
3. excluded：照抄 hidden 名单与理由。
4. layout_summary：整幅画面布局一句话（谁前谁后、谁大谁小、谁在哪一侧、画面主体是谁）。
5. 若提供了运动轨迹（dynamic=true 时），在 presentation 里补一句该人物初始帧之后的运动意向（走位方向、靠近/远离镜头），并在 layout_summary 里补镜头运动（若有）。

只输出一个 JSON 对象，schema：
{"shot":{"type":"景别","camera_height":"机位高度","composition":"构图一句话"},
 "characters":[{"name":"...","in_frame":true,"frame_position":"...","depth_label":"前景/中景/背景","size_percent":100,"frame_side":"照抄","facing":"照抄","occlusion":"","spacing":"","presentation":""}],
 "excluded":[{"name":"...","reason":"照抄"}],
 "layout_summary":"..."}
size_percent 与 frame_side、facing 必须与输入事实逐字一致。facing 是硬性视觉约束：写"完全背对镜头"的人画面里绝不能出现他的脸（只能后脑勺与背影）；写"正面对镜头"的人必须正脸朝观众。"""


async def synthesize_shot_brief(plot: dict, scene_desc: str, dynamic: bool) -> dict | None:
    """把调度图坐标+程序几何事实交给 DeepSeek，产出统一的"镜头画面 JSON"。
    结果按内容哈希缓存（拖拽/移机位/改描述才会重算）；任何失败返回 None（调用方回退程序直译）。"""
    import hashlib
    if not isinstance(plot, dict) or not [c for c in (plot.get("characters") or []) if str(c.get("name") or "").strip()]:
        return None
    if not _dsk_ok():
        return None
    try:
        key = hashlib.sha1(json.dumps(
            {"plot": plot, "desc": (scene_desc or "")[:500], "dyn": bool(dynamic), "v": 3},
            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    except Exception:
        return None
    if key in _SHOT_BRIEF_CACHE:
        return _SHOT_BRIEF_CACHE[key]

    facts = compute_camera_facts(plot)
    compact = {
        "camera": {"x": round(facts["camera"]["x"], 3), "y": round(facts["camera"]["y"], 3),
                   "facing": facts["camera"]["facing"], "fov": facts["camera"]["fov"],
                   "height": facts["camera"].get("height") or 1.6,
                   "height_desc": facts["camera"].get("height_desc") or "平拍（与人眼等高）",
                   "pitch": facts["camera"].get("pitch") or 0,
                   "pitch_desc": facts["camera"].get("pitch_desc") or "水平视角"},
        "characters": [{"name": str(c.get("name")).strip(),
                        "x": round(float(c.get("x") or 0), 3), "y": round(float(c.get("y") or 0), 3),
                        "facing": float(c.get("facing") or 0),
                        "path": [[round(float(p[0]), 3), round(float(p[1]), 3)] for p in (c.get("path") or [])[:8]
                                 if isinstance(p, (list, tuple)) and len(p) >= 2]}
                       for c in plot.get("characters") or []],
        "props": [{"name": str(p.get("name") or "")[:20],
                  "x": round(float(p.get("x") or 0), 3), "y": round(float(p.get("y") or 0), 3),
                  "facing": round(float(p.get("facing") or 0), 0)}
                  for p in (plot.get("props") or [])[:12]],
    }
    facts_txt = {
        "visible": [{"name": c["name"], "depth_rank": i + 1, "size_percent": c["size_pct"],
                     "frame_side": c["side"], "facing": c["facing_desc"], "near_edge": c["near_edge"]}
                    for i, c in enumerate(facts["visible"])],
        "hidden": [{"name": c["name"], "reason": c["reason"]} for c in facts["hidden"]],
    }
    messages = [
        {"role": "system", "content": SHOT_TRANSLATE_SYSTEM_PROMPT},
        {"role": "user", "content":
            f"【调度图纸坐标数据】\n{json.dumps(compact, ensure_ascii=False)}\n\n"
            f"【程序几何事实（权威，照抄）】\n{json.dumps(facts_txt, ensure_ascii=False)}\n\n"
            f"【画面描述】{(scene_desc or '')[:500] or '（无，按坐标推断）'}\n\n"
            f"【模式】{'生视频（名单只约束0秒初始帧，需补运动意向）' if dynamic else '生单张分镜图（名单＝全部画面）'}\n"
            "请输出镜头画面 JSON。"},
    ]
    try:
        response = await asyncio.to_thread(
            _dsk().chat.completions.create,
            model="deepseek-chat", messages=messages, temperature=0.2, max_tokens=1500,
            timeout=90)  # 硬超时：翻译卡死时 90 秒后回退程序直译，不让视频提交整单挂住
        data = deepseek_json(response)
        brief = _validate_shot_brief(data, facts, dynamic)
        if brief:
            _SHOT_BRIEF_CACHE[key] = brief
            log_prompt("AI镜头翻译", json.dumps(brief, ensure_ascii=False)[:600])
            return brief
    except Exception as e:
        print(f"[shot-brief] AI镜头翻译失败，回退程序直译: {type(e).__name__}: {e}")
    return None


def _validate_shot_brief(data: Any, facts: dict, dynamic: bool) -> dict | None:
    """硬校验 AI 返回的镜头画面 JSON：入画名单必须与程序事实一致，
    大小百分比/画面左右/朝向以程序为准；多写的人剔除、漏写的人补默认条目。"""
    if not isinstance(data, dict):
        return None
    vis = facts["visible"]
    vis_names = [c["name"] for c in vis]
    hidden = facts["hidden"]
    shot = data.get("shot") if isinstance(data.get("shot"), dict) else {}
    out_chars, seen = [], set()
    for item in data.get("characters") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen or name not in vis_names:
            continue  # AI 多写进画面的人：一律剔除（入画名单以程序几何为准）
        seen.add(name)
        fc = next(c for c in vis if c["name"] == name)
        idx = vis_names.index(name)
        rank = idx + 1
        n = max(len(vis), 1)
        depth_label = str(item.get("depth_label") or "").strip()
        if depth_label not in ("前景", "中景", "背景"):
            depth_label = "前景" if idx == 0 else ("背景" if idx == n - 1 else "中景")
        # frame_position：AI 的措辞 + 程序左右纠偏（画面左/右写反时以程序为准）
        fp = str(item.get("frame_position") or "").strip()
        prog_side = fc["side"]  # 如 "画面右侧" / "画面中部（几乎正对镜头）"
        prog_left_right = "画面右" if "画面右" in prog_side else ("画面左" if "画面左" in prog_side else "")
        if prog_left_right:
            other = "画面左" if prog_left_right == "画面右" else "画面右"
            if other in fp and prog_left_right not in fp:
                fp = fp.replace(other, prog_left_right)
        if not fp:
            fp = prog_side
        out_chars.append({
            "name": name, "in_frame": True, "frame_position": fp, "depth_label": depth_label,
            "depth_rank": rank, "size_percent": fc["size_pct"], "frame_side": prog_side,
            "facing": fc["facing_desc"],
            "occlusion": str(item.get("occlusion") or "").strip()[:80],
            "spacing": str(item.get("spacing") or "").strip()[:80],
            "presentation": str(item.get("presentation") or "").strip()[:150],
        })
    # 漏写的人补默认条目（只用程序事实）
    for idx, fc in enumerate(vis):
        if fc["name"] in seen:
            continue
        n = max(len(vis), 1)
        out_chars.append({
            "name": fc["name"], "in_frame": True,
            "frame_position": fc["side"], "depth_label": "前景" if idx == 0 else ("背景" if idx == n - 1 else "中景"),
            "depth_rank": idx + 1, "size_percent": fc["size_pct"], "frame_side": fc["side"],
            "facing": fc["facing_desc"], "occlusion": "", "spacing": "", "presentation": ""})
    out_excluded, ex_seen = [], set()
    for item in data.get("excluded") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in ex_seen or name in vis_names:
            continue
        reason = str(item.get("reason") or "").strip()
        match = next((h for h in hidden if h["name"] == name), None)
        out_excluded.append({"name": name, "reason": (match["reason"] if match else reason) or "不在机位视野内"})
        ex_seen.add(name)
    for h in hidden:  # 程序 hidden 名单一个不能少
        if h["name"] not in ex_seen:
            out_excluded.append({"name": h["name"], "reason": h["reason"]})
            ex_seen.add(h["name"])
    if not out_chars and not out_excluded:
        return None
    return {
        "shot": {"type": str(shot.get("type") or "").strip()[:30],
                 "camera_height": str(shot.get("camera_height") or "").strip()[:30],
                 "composition": str(shot.get("composition") or "").strip()[:120]},
        "characters": out_chars, "excluded": out_excluded,
        "layout_summary": str(data.get("layout_summary") or "").strip()[:250],
        "dynamic": bool(dynamic),
    }


def render_shot_brief_text(brief: dict) -> str:
    """把校验后的镜头画面 JSON 渲染成注入提示词的紧凑文本。"""
    lines = ["【镜头画面解读（AI 按调度图坐标翻译，几何结论经程序校验，权威）】"]
    shot = brief.get("shot") or {}
    head = "；".join(x for x in (shot.get("type"), shot.get("camera_height"), shot.get("composition")) if x)
    if head:
        lines.append("镜头：" + head + "。")
    chars = brief.get("characters") or []
    if chars:
        seg = []
        for c in chars:
            parts = [c["frame_position"], f"{c['depth_label']}(第{c['depth_rank']}近)"]
            if c["size_percent"] and c["size_percent"] < 100:
                parts.append(f"大小约为最近者的{c['size_percent']}%")
            elif c["size_percent"]:
                parts.append("画面里最大")
            parts.append(c["facing"])
            if c.get("occlusion"):
                parts.append(f"遮挡：{c['occlusion']}")
            if c.get("spacing"):
                parts.append(f"间距：{c['spacing']}")
            if c.get("presentation"):
                parts.append(c["presentation"])
            seg.append(f"{c['name']}（" + "、".join(p for p in parts if p) + "）")
        lines.append("入画名单（从近到远）：" + "；".join(seg) + "。")
        # 朝向单独成行、逐人给出硬性视觉要求——埋在括号列表里权重太低，模型实测会忽略
        fr = "；".join(f"{c['name']}＝{_facing_brief(c['facing'])}（{_facing_rule_text(c['facing'])}）" for c in chars)
        lines.append("【人物朝向·逐人硬约束】" + fr + "。朝向是权威事实，与任何文字描述冲突时以本条为准。")
        lines.append("【图纸外群演】提示词正文明确写到、但图纸没有摆放的人物（宫人、侍卫、宾客等群演配角）按正文入画："
                     "安排在背景或正文指定的位置即可，不挤占名单人物的站位与景别，人数与正文一致；"
                     "正文没写的无名人影一律不画。")
    excluded = brief.get("excluded") or []
    if excluded:
        lines.append("绝不入画：" + "；".join(f"{c['name']}（{c['reason']}）" for c in excluded) + "。")
    if brief.get("layout_summary"):
        lines.append("整体布局：" + brief["layout_summary"] + "。")
    return "\n".join(lines) + "\n"


async def build_plot_prompt_text(plot: dict, scene_desc: str, dynamic: bool = False) -> str:
    """调度图→提示词文本的统一入口：优先 AI 镜头翻译（缓存），失败回退程序直译。"""
    brief = await synthesize_shot_brief(plot or {}, scene_desc, dynamic)
    if brief:
        text = render_shot_brief_text(brief)
        # AI 翻译只覆盖人物（入画名单/落位/朝向），门窗家具的画面落位由程序按机位几何直译补充
        _pf = _prop_frame_lines(plot or {})
        if _pf:
            text += _pf + "\n"
        # 人物与织布机等大件陈设的前后关系也说破（AI 翻译不输出这类相对关系）
        _rel = _char_prop_relations(plot or {}, compute_camera_facts(plot or {}))
        if _rel:
            text += _rel + "\n"
        # 空间比例锁定（米制）：人物间距/离陈设/离墙/陈设占地全部写成米数
        _sp = _spatial_lock_lines(plot or {}, compute_camera_facts(plot or {}))
        if _sp:
            text += _sp + "\n"
        # 轨迹/走位仍由程序直译补充（AI 只管初始帧画面语言；运动路线公式简单稳定，无需 AI）
        det = format_plot_to_prompt(plot or {}, dynamic=dynamic)
        det_moves = ""
        for ln in det.splitlines():
            if ln.startswith("【运动路线】") or ln.startswith("【人物站位·初始不在画面名单"):
                det_moves += ln + "\n"
        if dynamic and det_moves:
            text += "【运动路线与初始出画名单（程序按坐标直译，权威）】\n" + det_moves
        return text
    return format_plot_to_prompt(plot or {}, dynamic=dynamic)


def _load_cn_font(size: int):
    """加载中文字体。云端（Linux）没有 Windows 字体，必须优先用项目内置字体，
    否则调度图上的中文全部渲染成方块。arial 已剔除——它无中文字形且加载成功会静默产出豆腐块。"""
    from PIL import ImageFont
    import os
    # 1) 项目内置字体（随发布打包，所有环境一致）
    bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "msyh.ttc")
    try:
        return ImageFont.truetype(bundled, size)
    except Exception:
        pass
    # 2) Windows 系统字体
    for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc"):
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except Exception:
            continue
    # 3) Linux 常见中文字体（云平台若装了 Noto/文泉驿）
    for p in ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
              "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()

def _hex_rgb(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))

def render_diagram_png(plot: dict, kind: str) -> bytes:
    """把调度坐标数据绘制成平面图纸 PNG。坐标系：x 右为正、y 下为正（0-1 归一化）。"""
    from PIL import Image, ImageDraw
    import math

    W, H = 1400, 1460
    M = 40                      # 图纸外边距
    TITLE_H = 76
    LEGEND_H = 110 if plot.get("characters") else 46
    FX0, FY0 = M, TITLE_H       # 场景平面区域
    FW, FH = W - M * 2, H - TITLE_H - LEGEND_H - M - 30
    img = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    f_title = _load_cn_font(34)
    f_label = _load_cn_font(24)
    f_small = _load_cn_font(19)
    f_tag = _load_cn_font(22)

    is_traj = (kind or "").lower() == "trajectory"
    d.text((M, 18), ("轨迹调度图 · 运动轨迹与机位移动（正上方俯视平面图）" if is_traj
                     else "俯视调度图 · 人物位置与机位（正上方俯视平面图）"),
           fill="#1f2937", font=f_title)
    _rw, _rh = _room_dims(plot)
    d.text((W - M, 30), f"上=空间远端  下=空间近端  房间约{_rw:g}米×{_rh:g}米", fill="#9ca3af", font=f_small, anchor="ra")

    # 场景地面边框 + 网格
    d.rounded_rectangle([FX0, FY0, FX0 + FW, FY0 + FH], radius=18, outline="#374151", width=5)
    for i in range(1, 10):
        gx = FX0 + FW * i / 10
        gy = FY0 + FH * i / 10
        d.line([gx, FY0 + 4, gx, FY0 + FH - 4], fill="#f1f2f4", width=2)
        d.line([FX0 + 4, gy, FX0 + FW - 4, gy], fill="#f1f2f4", width=2)

    def P(x, y):
        return (FX0 + FW * x, FY0 + FH * y)

    def dashed_line(p1, p2, color, width=5, dash=16, gap=10):
        x1, y1 = p1; x2, y2 = p2
        dist = math.hypot(x2 - x1, y2 - y1)
        if dist < 1:
            return
        ux, uy = (x2 - x1) / dist, (y2 - y1) / dist
        t = 0.0
        while t < dist:
            t2 = min(t + dash, dist)
            d.line([x1 + ux * t, y1 + uy * t, x1 + ux * t2, y1 + uy * t2], fill=color, width=width)
            t = t2 + gap

    def arrow_head(tip, angle_deg, color, size=20):
        ax, ay = tip
        rad = math.radians(angle_deg)
        for spread in (150, 210):
            wx = ax + size * math.cos(rad + math.radians(spread))
            wy = ay + size * math.sin(rad + math.radians(spread))
            d.line([ax, ay, wx, wy], fill=color, width=width_of(color))

    def width_of(_c):
        return 5

    def facing_xy(x, y, facing, length):
        rad = math.radians(facing)
        return (x + length * math.sin(rad), y - length * math.cos(rad))

    # 陈设（facing：0=朝图纸上方、顺时针，与人物/机位同一约定；0 时退化为轴对齐矩形）
    for p in plot.get("props") or []:
        px, py = P(p["x"], p["y"])
        pw, ph = FW * p["w"], FH * p["h"]
        rad = math.radians(float(p.get("facing") or 0))
        # 局部坐标 → 图像偏移：正面法向（u，深度方向）=(sin, -cos)，右向（v，宽度方向）=(cos, sin)
        # 与 facing_xy 同一约定，保证 2D 图纸与 3D 导演台转出来的朝向一致
        corners = []
        for u, v in ((-ph / 2, -pw / 2), (-ph / 2, pw / 2), (ph / 2, pw / 2), (ph / 2, -pw / 2)):
            corners.append((px + u * math.sin(rad) + v * math.cos(rad),
                            py + u * (-math.cos(rad)) + v * math.sin(rad)))
        d.polygon(corners, fill="#eef1f5")
        d.line(corners + [corners[0]], fill="#8a93a3", width=3, joint="curve")
        top_off = abs(math.cos(rad)) * ph / 2 + abs(math.sin(rad)) * pw / 2
        d.text((px, py - top_off - 14), p["label"], fill="#6b7280", font=f_tag, anchor="mm")

    # 角色轨迹（先画线，再画人偶，线在人偶下方）——只在轨迹图模式画；
    # 俯视图（站位机位）只看位置与朝向，画轨迹线会与轨迹图重复、显得杂乱（用户反馈）
    chars = plot.get("characters") or []
    color_of = {c["name"]: _DIAGRAM_PALETTE[i % len(_DIAGRAM_PALETTE)] for i, c in enumerate(chars)}
    if is_traj:
        for c in chars:
            col = color_of[c["name"]]
            path_pts = [P(*pt) for pt in (c.get("path") or [])]
            if path_pts:
                full = [P(c["x"], c["y"])] + path_pts
                for i in range(len(full) - 1):
                    dashed_line(full[i], full[i + 1], col, width=6)
                arrow_head(full[-1], math.degrees(math.atan2(full[-1][1] - full[-2][1], full[-1][0] - full[-2][0])), col)
                # 时间节点
                stops = c.get("stops") or []
                for i, pt in enumerate(full):
                    d.ellipse([pt[0] - 9, pt[1] - 9, pt[0] + 9, pt[1] + 9], fill=col, outline="#ffffff", width=3)
                    label = ""
                    if i < len(stops):
                        label = f"{stops[i].get('t', '')} {stops[i].get('action', '')}".strip()
                    if label:
                        d.text((pt[0] + 14, pt[1] - 14), label, fill=col, font=f_small, anchor="lm")

        # 机位轨迹（黑色虚线，画在人偶下方）——同样只在轨迹图模式画
        cam = plot.get("camera") or {}
        cam_path_pts = [P(*pt) for pt in (cam.get("path") or [])]
        cam_pt = P(cam.get("x", 0.5), cam.get("y", 0.95))
        cam_full = [cam_pt] + cam_path_pts
        for i in range(len(cam_full) - 1):
            dashed_line(cam_full[i], cam_full[i + 1], "#111827", width=5, dash=12, gap=8)
            arrow_head(cam_full[i + 1], math.degrees(math.atan2(cam_full[i + 1][1] - cam_full[i][1], cam_full[i + 1][0] - cam_full[i][0])), "#111827", size=16)
    else:
        cam = plot.get("camera") or {}
        cam_pt = P(cam.get("x", 0.5), cam.get("y", 0.95))

    # 机位视锥（半透明感浅黄色扇形）
    fov = float(cam.get("fov") or 55)
    facing = float(cam.get("facing") or 0)
    R = min(FW, FH) * 0.38
    # PIL 角度以 3 点钟方向为 0、顺时针；本坐标系 0=朝上 → pil = facing - 90
    start = facing - fov / 2 - 90
    end = facing + fov / 2 - 90
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.pieslice([cam_pt[0] - R, cam_pt[1] - R, cam_pt[0] + R, cam_pt[1] + R],
                start, end, fill=(250, 214, 90, 88))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    d = ImageDraw.Draw(img)

    # 人物人偶棋子（起点位置；有轨迹的由轨迹线+箭头表达终点）
    char_pts = [(c, P(c["x"], c["y"])) for c in chars]
    for c, pt in char_pts:
        col = color_of[c["name"]]
        r = 17
        d.ellipse([pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r], fill=col, outline="#ffffff", width=4)
        # 朝向箭头
        tip = facing_xy(pt[0], pt[1], c.get("facing", 0), 40)
        d.line([pt[0], pt[1], tip[0], tip[1]], fill=col, width=6)
        arrow_head(tip, c.get("facing", 0), col)
        # 名字标签
        label = c["name"]
        tw = d.textlength(label, font=f_label)
        lx, ly = pt[0] - tw / 2 - 10, pt[1] + r + 8
        d.rounded_rectangle([lx, ly, lx + tw + 20, ly + 34], radius=8, fill="#ffffff", outline=col, width=3)
        d.text((lx + 10, ly + 4), label, fill="#111827", font=f_label)

    # 机位符号（相机机身图形 + 文字）
    d.ellipse([cam_pt[0] - 16, cam_pt[1] - 16, cam_pt[0] + 16, cam_pt[1] + 16], fill="#111827", outline="#ffffff", width=3)
    d.rectangle([cam_pt[0] - 9, cam_pt[1] - 5, cam_pt[0] + 9, cam_pt[1] + 6], fill="#ffffff")
    d.ellipse([cam_pt[0] - 4, cam_pt[1] - 2, cam_pt[0] + 4, cam_pt[1] + 4], fill="#111827")
    # 机位标签带拍摄角度（导演在 3D 导演台调过高度/俯仰时，图上一眼可见）
    camd = plot.get("camera") or {}
    cam_h_desc = _camera_height_desc(camd.get("height"))
    cam_p_desc = _camera_pitch_desc(camd.get("pitch"))
    if cam_p_desc and cam_p_desc != "水平视角":
        cam_label = "机位·" + ("俯" if cam_p_desc.startswith(("低俯", "大俯")) else "仰") + cam_p_desc.split("°")[0].split("朝")[-1] + "°"
    else:
        cam_label = "机位" + ("·" + cam_h_desc.split("（")[0] if cam_h_desc and "平拍" not in cam_h_desc else "")
    d.text((cam_pt[0], cam_pt[1] + 24), cam_label, fill="#111827", font=f_label, anchor="ma")

    # 图例
    ly0 = FY0 + FH + 18
    x = M
    d.text((x, ly0), "图例：", fill="#374151", font=f_tag)
    x += 92
    for c in chars:
        col = color_of[c["name"]]
        d.ellipse([x, ly0 + 4, x + 22, ly0 + 26], fill=col, outline="#ffffff", width=2)
        d.text((x + 30, ly0 + 3), c["name"], fill="#374151", font=f_tag)
        x += 30 + d.textlength(c["name"], font=f_tag) + 26
    d.ellipse([x, ly0 + 4, x + 22, ly0 + 26], fill="#111827")
    d.text((x + 30, ly0 + 3), "机位（扇形=视野）", fill="#374151", font=f_tag)
    x += 30 + d.textlength("机位（扇形=视野）", font=f_tag) + 26
    if is_traj:
        d.line([x, ly0 + 15, x + 44, ly0 + 15], fill="#111827", width=4)
        d.text((x + 52, ly0 + 3), "机位移动", fill="#374151", font=f_tag)

    note = str(plot.get("note") or "").strip()
    if note:
        d.text((M, H - 40), f"备注：{note}", fill="#6b7280", font=f_small)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def _apply_lens_moves(plot: dict, text: str) -> dict:
    """程序兜底纠偏：口述「XX走向镜头/机位」时，AI 常把"镜头"误解成图纸下方/上方（观众方向）。
    「镜头」的唯一含义是机位符号所在坐标点——此处按坐标硬纠偏：把被点名角色的轨迹终点
    吸附到机位附近（起点→机位连线上、距机位 0.08 处，两点一线），朝向改为面向机位。
    AI 已解析正确（终点已在机位附近）时不覆盖，保留其原有拐点。"""
    import math
    if not isinstance(plot, dict) or not text:
        return plot
    if not re.search(r"(走向|走近|走到|跑向|冲向|扑向|来到|退到)(了|到)?(镜头|机位|摄影机|摄像机)", text):
        return plot
    cam = plot.get("camera") or {}
    cx, cy = float(cam.get("x", 0.5)), float(cam.get("y", 0.95))
    names = [str(c.get("name") or "").strip() for c in plot.get("characters") or [] if str(c.get("name") or "").strip()]
    # 找出口述中点名走向镜头的角色（名字出现在动词短语前），找不到再回退到口述里出现过的角色名
    subjects = set()
    for m in re.finditer(r"([\u4e00-\u9fa5A-Za-z0-9·]{1,8}?)(?:走向|走近|走到|跑向|冲向|扑向|来到)(?:了|到)?(?:镜头|机位|摄影机|摄像机)", text):
        frag = m.group(1)
        for nm in names:
            if nm and nm in frag:
                subjects.add(nm)
    if not subjects:
        for nm in names:
            if nm and nm in text:
                subjects.add(nm)
    if not subjects:
        return plot
    for c in plot.get("characters") or []:
        nm = str(c.get("name") or "").strip()
        if nm not in subjects:
            continue
        px, py = float(c.get("x") or 0), float(c.get("y") or 0)
        dx, dy = cx - px, cy - py
        d = math.hypot(dx, dy)
        if d < 0.15:
            continue  # 起点就在机位旁（可能是视角人物），不动
        path = [list(p)[:2] for p in (c.get("path") or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
        end = path[-1] if path else [px, py]
        end_d = math.hypot(end[0] - cx, end[1] - cy)
        if end_d <= 0.25:
            continue  # AI 已把终点放到机位附近，尊重其解析
        ex = min(max(cx - dx / d * 0.08, 0.0), 1.0)
        ey = min(max(cy - dy / d * 0.08, 0.0), 1.0)
        c["path"] = [[round(px, 3), round(py, 3)], [round(ex, 3), round(ey, 3)]]  # 两点一线
        c["facing"] = round(math.degrees(math.atan2(dx, -dy)) % 360, 1)           # 面向机位
        stops = c.get("stops")
        if isinstance(stops, list) and stops:
            last = stops[-1]
            if isinstance(last, dict) and not str(last.get("action") or "").strip():
                last["action"] = "走到镜头前"
    return plot


def _stop_unmentioned(plot: dict, text: str) -> dict:
    """程序兜底：口述里完全没点名的角色一律停在原位（path/stops 清空），
    防止 AI 给口述未提及的角色擅自加戏走位（实测：口述只说"小帅走向镜头"，AI 让小美也走了）。
    口述里一个名单名都没出现时不清理（可能在用昵称，避免误杀被点名角色的轨迹）。"""
    if not isinstance(plot, dict) or not text:
        return plot
    chars = [c for c in (plot.get("characters") or []) if isinstance(c, dict) and str(c.get("name") or "").strip()]
    mentioned = [c for c in chars if str(c["name"]).strip() in text]
    if not mentioned:
        return plot  # 名单名一个都没出现（昵称/代称），保守起见不清轨迹
    for c in chars:
        if c in mentioned:
            continue
        if any(isinstance(p, (list, tuple)) and len(p) >= 2 for p in (c.get("path") or [])):
            c["path"] = []
            c["stops"] = []
    return plot


@app.post("/api/render-diagram")
async def render_diagram(req: TopViewRequest):
    """调度图纸：DeepSeek 解析口述 → 坐标数据 → PIL 程序化绘制。解析失败用默认布局兜底并提示重试。"""
    kind = (req.kind or "topview").lower()
    user_lines = []
    if req.scene_name:
        user_lines.append(f"所在场景：{req.scene_name}")
    if req.scene_description:
        user_lines.append(f"场景描述：{req.scene_description}")
    user_lines.append(f"镜头画面描述：{req.description or '（无）'}")
    user_lines.append(f"台词（仅供理解剧情）：{req.dialogue or '（无）'}")
    user_lines.append(f"镜头时长：{req.duration} 秒")
    if req.video_prompt:
        user_lines.append(f"视频运动描述：{req.video_prompt}")
    if req.characters:
        chars = "；".join(str(c.get("name") or "") for c in req.characters if c.get("name"))
        if chars:
            user_lines.append(f"出场角色：{chars}")
    if req.feedback:
        user_lines.append(f"导演口述（最高优先级，逐条落实为坐标）：{req.feedback}")
    base = req.base_plot if isinstance(req.base_plot, dict) else None
    if base:
        blines = []
        for c in (base.get("characters") or []):
            if isinstance(c, dict) and str(c.get("name") or "").strip():
                blines.append(f"- {c.get('name')}：起点({c.get('x')},{c.get('y')})，朝向{c.get('facing')}度")
        bc = base.get("camera") if isinstance(base.get("camera"), dict) else {}
        if bc:
            blines.append(f"- 机位：({bc.get('x')},{bc.get('y')})，朝向{bc.get('facing')}度，movement={bc.get('movement')}")
        if blines:
            user_lines.append("现有俯视调度图坐标（权威起点，角色起点/朝向/机位起点必须照抄，轨迹从此出发，禁止重新布局）：\n" + "\n".join(blines))

    plot, source = None, "ai"
    try:
        response = _dsk().chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": DIAGRAM_PLOT_PROMPT},
                {"role": "user", "content": "\n".join(user_lines)},
            ],
            response_format={"type": "json_object"},
            max_tokens=1200,
            temperature=0.2,
        )
        data = deepseek_json(response)
        if isinstance(data.get("characters"), list) and data["characters"]:
            plot = data
            # 名字对齐：口述里的昵称/错名会被 AI 照抄成名单外新角色，造成图纸与分镜提示词两套人马
            plot = _align_character_names(plot, req.characters)
    except Exception as e:
        print(f"[render-diagram] DeepSeek 解析失败（{kind}），用默认布局: {type(e).__name__}: {e}")
    if not plot:
        plot = _fallback_plot(req)
        source = "template"
    if base:
        # 程序层强制对齐：即使 AI 没照抄起点，也以 base 坐标为准合并
        plot = _merge_base_plot(plot, base)
    # 「走向镜头/机位」语义纠偏：镜头＝机位坐标点，不是图纸下方；终点吸附到机位附近（两点一线）
    lens_txt = (req.feedback or "") + " " + (req.description or "") + " " + (req.video_prompt or "")
    plot = _apply_lens_moves(plot, lens_txt)
    # 口述未点名的角色一律停走：AI 常给没提到的人擅自加轨迹（口述只说小帅，小美也被走位）
    plot = _stop_unmentioned(plot, lens_txt)
    plot = _normalize_plot(plot)
    png = render_diagram_png(plot, kind)
    os.makedirs("static/uploads", exist_ok=True)
    fname = f"diagram_{kind}_{uuid.uuid4().hex[:10]}.png"
    with open(os.path.join("static", "uploads", fname), "wb") as f:
        f.write(png)
    log_prompt(f"调度图纸绘制({kind})", json.dumps(plot, ensure_ascii=False)[:500])
    cam_summary = _camera_view_summary(plot)
    log_prompt(f"机位视野计算({kind})", cam_summary)
    return {"image_url": f"/static/uploads/{fname}", "plot": plot, "cam_summary": cam_summary, "source": source}

# ---------- 2.62 手动移机位：导演在图上标注/拖动机位后，把 plot.camera 真正移过去并重绘图纸（消除双机位） ----------
class MoveCameraRequest(BaseModel):
    plot: dict  # /api/render-diagram 返回的坐标数据
    x: float  # 新机位 x（0-1，图纸坐标）
    y: float  # 新机位 y（0-1，图纸坐标）
    facing: float | None = None  # 新机位朝向（度，0=图纸上方、90=右方、180=下方、270=左方）；None=保持原朝向

@app.post("/api/move-camera")
async def move_camera(req: MoveCameraRequest):
    """导演手动标注机位：更新 plot.camera 位置与朝向 → 重绘图纸 → 重算机位视野名单。
    机位是唯一权威：图纸只画这一个机位符号，标注即替换，不再叠加第二个机位。"""
    plot = _normalize_plot(req.plot or {})
    plot["camera"]["x"] = _clamp01(req.x)
    plot["camera"]["y"] = _clamp01(req.y)
    if req.facing is not None:
        plot["camera"]["facing"] = float(req.facing) % 360
        plot["camera"]["movement"] = "固定机位"
        plot["camera"]["path"] = []
    png = render_diagram_png(plot, "topview")
    os.makedirs("static/uploads", exist_ok=True)
    fname = f"diagram_topview_{uuid.uuid4().hex[:10]}.png"
    with open(os.path.join("static", "uploads", fname), "wb") as f:
        f.write(png)
    cam_summary = _camera_view_summary(plot)
    log_prompt("手动移机位", json.dumps(plot.get("camera"), ensure_ascii=False)[:200])
    log_prompt("机位视野计算(移机位后)", cam_summary)
    return {"image_url": f"/static/uploads/{fname}", "plot": plot, "cam_summary": cam_summary}

# ---------- 2.63 拖拽编辑：导演在图上直接拖动角色/轨迹拐点/陈设/机位，前端把新坐标发回重绘 ----------
class RerenderDiagramRequest(BaseModel):
    plot: dict  # 前端拖拽后改好的坐标数据（0-1 归一化坐标）
    kind: str = "topview"  # topview=俯视调度图 / trajectory=轨迹调度图

@app.post("/api/rerender-diagram")
async def rerender_diagram(req: RerenderDiagramRequest):
    """拖拽编辑的统一重绘入口：前端负责拖挂数学（角色/机位连同轨迹整体平移、
    拐点单独拖动、陈设移中心点），后端只做规范化钳制与重绘，保证图纸永远合法。
    角色移动会同步平移其运动轨迹、机位移动会同步平移其移动路径（整条走位跟着人走）。"""
    kind = "trajectory" if (req.kind or "").lower() == "trajectory" else "topview"
    plot = _normalize_plot(req.plot or {})
    png = render_diagram_png(plot, kind)
    os.makedirs("static/uploads", exist_ok=True)
    fname = f"diagram_{kind}_{uuid.uuid4().hex[:10]}.png"
    with open(os.path.join("static", "uploads", fname), "wb") as f:
        f.write(png)
    cam_summary = _camera_view_summary(plot)
    log_prompt(f"拖拽重绘({kind})", json.dumps(
        {"characters": [(c.get("name"), round(float(c.get("x") or 0), 3), round(float(c.get("y") or 0), 3)) for c in plot.get("characters") or []],
         "camera": [plot.get("camera", {}).get("x"), plot.get("camera", {}).get("y")]},
        ensure_ascii=False)[:300])
    return {"image_url": f"/static/uploads/{fname}", "plot": plot, "cam_summary": cam_summary}

# ---------- 2.65 调度纵深解读：把俯视口述换算成"人物近/中/远景层次"说明（供分镜图理解远近关系） ----------
class StagingBriefRequest(BaseModel):
    description: str = ""  # 分镜画面描述/提示词
    topview_desc: str | None = None  # 俯视调度图口述（位置关系+机位）
    trajectory_desc: str | None = None  # 轨迹调度图口述（运动轨迹）
    characters: List[Dict[str, Any]] | None = None
    cam_summary: str | None = None  # 程序按坐标计算的机位视野事实（谁入画/谁出画），权威

STAGING_SYSTEM_PROMPT = """你是场面调度解读员。导演给了一个镜头的调度口述（俯视走位/运动轨迹，机位信息含在其中），需要解读成一段"画面纵深调度说明"，让文生图模型准确理解人物的远近关系。

【解读规则】
0. 若提供「机位视野事实」（程序按坐标计算），它关于机位位置与朝向、谁入画/谁不入画、每人身体朝向（面向镜头/背对镜头/侧对镜头面朝画面左或右）的结论全部是权威事实，解读必须逐条原样遵守：不得把名单外的人写进入画、不得把名单内的人写出画面、不得改动机位位置与朝向、不得给任何人另编一个朝向；你只在其基础上细化纵深次序、遮挡关系与画面表现。口述文字与机位视野事实冲突时（如导演手动移过机位后口述仍写旧机位），以机位视野事实为准。
0. 先做坐标系换算：口述里的画左/画右/画上方/画下方是【俯视图平面】上的方位，不是成片画面的方位。换算规则：俯视图上离机位符号近的 = 成片近景，离机位远的 = 成片远景；俯视图的左右按机位朝向换算（当机位位于俯视图下方朝画面上方拍摄时，俯视图左 = 成片画面左、俯视图右 = 成片画面右、俯视图上 = 远景；机位在其它方位时按此几何关系类推，换算依据写明）。
1. 先从口述中确定机位位置与拍摄朝向（如"平拍""固定机位""机位在天台东侧"）；若口述未明确，按最合理的观察点推断并注明（推断）。
2. 机位视角占主导：先以机位与拍摄朝向为准，把出场人物分成两组：
   【镜头后方组】——站在机位所在位置上的人物（视角人物，镜头从他眼中拍出）、以及位于机位身后（与拍摄朝向相反一侧）的人物：一律「在镜头后方，完全出画、不出现」，不标注画面位置；
   【机位前方组】——机位视线前方的一切人物（包括紧挨机位、视野极边缘的人）：按物理透视正常入画，给每人标注【成片画面中的布局】：左右（画面左缘/中部/右缘）+ 与机位的相对远近次序（谁比谁离镜头近、谁最远）与画面表现：
   - 离机位近 → 离镜头近 → 画面中显得大、位于画面下缘，可能部分遮挡别人或被取景框裁切（紧挨机位的人可能只露出腿部/肩膀，按真实视野处理）；
   - 离机位远 → 显得小、靠近画面上缘地平线。
   分寸提示：从分镜描述中识别该镜头的景别与画面主体——主体按景别呈现，但主体同样站在调度图给他安排的位置上取景（不要把主体搬到前景）；其余人物的大小由与镜头的真实距离按透视自然形成，身高一致、纵深相近者大小相近，严禁画成巨人或小矮人；相对远近次序不变，与谁在做什么、谁是主角无关。
3. 左右关系按"站在机位处朝拍摄方向看去"的左右为准；每人朝向必须照抄机位视野事实里的结论（若事实里带「面朝『某陈设/某人』」锚点，必须把该锚点写进 brief 并让人物脸朝该目标）：正面对镜头＝正脸朝其面朝目标（未指定目标时朝观众）、四分之三面向镜头＝斜侧身脸大部分朝向其面朝目标、侧对镜头＝按写明的画面左右侧脸、四分之三背对镜头＝大半背影脸几乎不可见、完全背对镜头＝只见背影严禁画脸；被指定面朝目标的人严禁为了露脸而转向镜头。
4. 写明明显的前后遮挡关系（谁在谁前面）。
5. 必须写明人物之间的相对间距，保持口述中的比例：谁和谁挨得近（如"小美就在小帅右侧两三步处，两人几乎并排""三人扎堆围在栅栏边"）、谁离人群远（如"瘦猴独自在角落，与人群隔半个画面"）。相近的人在画面中也应相邻、允许互相部分遮挡；不要把人物均匀散布在画面里。
6. 若有移动轨迹，写明从起点纵深到终点纵深的变化（如"从远景跑向近景，逐渐变大"）。
7. 只依据口述与画面描述，不要新增口述里没有的人物或位置；口述含糊处用（推断）标注。
8. 机位视角占主导：机位落在某人物身上时，该人物是视角人物（镜头从他眼中拍出），视角人物与机位身后的人物完全出画；紧挨机位但位于机位视线前方的人物按物理透视正常入画（可为前景，可能只露部分身体）；解读结尾写明谁在镜头后方不入画。取景框是次要参考：人物因物理透视需要入画时（即使在取景框边缘或框外），按机位视角的真实视野处理，不强行因框而删除。

【输出】严格输出 JSON：{"staging_brief": "单段中文，180-320字，可直接拼进文生图提示词"}，不要 markdown，不要其他文字。brief 必须涵盖：① 每个人物离机位的远近次序；② 每个人物的画面左右位置；③ 谁与谁前后相邻或部分遮挡；④ 谁靠近画面下缘/上缘；⑤ 结合该镜头景别判断：哪些人物可能被画面边缘裁切、哪些人物会完全落在取景框外（特写/近景时其余人自然出画，留少量边缘身影由模型自行决定）；主体尽量完整入画。"""

def _fallback_staging_brief(req: StagingBriefRequest) -> str:
    parts = ["先做坐标系换算：口述的画左/画右/上方/下方是俯视平面方位——俯视图上离机位近的人物为前景（占画面大、靠画面下缘），中距为中景，离机位远的人物为背景（占画面小、靠画面上缘地平线）；俯视图的左右按机位朝向换算成成片画面左右（机位在俯视图下方朝上拍时，俯视图左＝成片画面左）；左右关系按从机位朝拍摄方向看去判定；人物之间的间距按口述比例保持，挨得近的在画面中也相邻，不要均匀散开；谁离机位远谁就是背景，与谁在做什么无关"]
    if req.topview_desc:
        parts.append(f"位置口述：{req.topview_desc[:150]}")
    if req.trajectory_desc:
        parts.append(f"轨迹口述：{req.trajectory_desc[:150]}")
    return "；".join(parts)

@app.post("/api/staging-brief")
async def staging_brief(req: StagingBriefRequest):
    """把调度口述解读为纵深调度说明。失败时返回通用换算规则兜底，保证生图流程不中断。"""
    if not (req.topview_desc or "").strip() and not (req.trajectory_desc or "").strip():
        return {"staging_brief": "", "source": "empty"}
    user_lines = [f"镜头画面描述：{req.description or '（无）'}"]
    if req.cam_summary:
        user_lines.append(f"机位视野事实（程序按坐标计算，权威——其中「不入画」名单不可更改，你的解读必须严格遵守）：{req.cam_summary}")
    if req.topview_desc:
        user_lines.append(f"俯视走位口述（含机位）：{req.topview_desc}")
    if req.trajectory_desc:
        user_lines.append(f"运动轨迹口述：{req.trajectory_desc}")
    if req.characters:
        chars = "；".join(f"{str(c.get('name') or '')}" for c in req.characters if c.get("name"))
        if chars:
            user_lines.append(f"出场人物：{chars}")
    try:
        response = _dsk().chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": STAGING_SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(user_lines)},
            ],
            response_format={"type": "json_object"},
            max_tokens=700,
            temperature=0.4,
        )
        data = deepseek_json(response)
        brief = str(data.get("staging_brief") or "").strip()
        if brief:
            log_prompt("调度纵深解读", brief)
            return {"staging_brief": brief, "source": "ai"}
    except Exception as e:
        print(f"[staging-brief] DeepSeek 解读失败，用模板兜底: {type(e).__name__}: {e}")
    fb = _fallback_staging_brief(req)
    log_prompt("调度纵深解读(模板兜底)", fb)
    return {"staging_brief": fb, "source": "template"}


# ---------- 2.75 提示词冲突一键检测：输入框提示词 vs 调度图/3D导演台坐标事实 ----------
# 优先级约定（与产品逻辑一致）：P1 调度图/3D导演台（第一原则，永远获胜）
#   > P2 导演在输入框手动修改/重写的内容 > P3 AI 自动生成的文本（必要时可舍弃）。
# 检测是纯规则比对（逐句扫描 vs compute_camera_facts 唯一真相源），不调 AI、毫秒级返回。

class ConflictCheckRequest(BaseModel):
    prompt: str = ""          # 生图提示词（输入框当前内容）
    video_prompt: str = ""    # 视频提示词（输入框当前内容）
    plot_data: dict = {}      # 本镜调度图/3D导演台坐标数据

_FACE_TOWARD_RE = re.compile(r"正面对镜头|面对镜头|面向镜头|朝向镜头|看向镜头|望着镜头|目视镜头|正脸朝观众|朝观众|对准镜头")
_FACE_AWAY_RE = re.compile(r"背对镜头|背对观众|背影|背身|转过身去|转身背")
_FACE_SIDE_RE = re.compile(r"侧对镜头|侧脸|侧身(朝|面向|对)")
_SIDE_LEFT_RE = re.compile(r"画面左|画左|靠左|左侧|左边")
_SIDE_RIGHT_RE = re.compile(r"画面右|画右|靠右|右侧|右边")
_PRESENCE_RE = re.compile(r"站|坐|躺|走|跑|跪|趴|蹲|立|出现在|入画|位于|走在|站在")

_CONFLICT_LEGEND = [
    {"level": "P1", "name": "调度图 / 3D 导演台", "desc": "坐标几何事实，第一原则，永远获胜"},
    {"level": "P2", "name": "输入框手动修改 / 重写", "desc": "导演手改的内容，第二优先级；与 P1 冲突时会标红提醒，由导演决定改哪边"},
    {"level": "P3", "name": "AI 自动生成文本", "desc": "必要时可舍弃；与 P1 冲突处建议直接删改"},
]


def _facing_family(desc: str) -> str:
    """机位几何事实的朝向档位 → 三大家族（toward/away/side），供与正文朝向词比对。"""
    if "正面对镜头" in desc or "四分之三面向镜头" in desc:
        return "toward"
    if "完全背对镜头" in desc or "四分之三背对镜头" in desc:
        return "away"
    if "侧对镜头" in desc:
        return "side"
    return ""


def _claim_family(sentence: str) -> str:
    """提示词句子里的朝向词 → 三大家族。同时命中多类（如"转身又看向镜头"）时返回空（不判）。"""
    fams = set()
    if _FACE_TOWARD_RE.search(sentence):
        fams.add("toward")
    if _FACE_AWAY_RE.search(sentence):
        fams.add("away")
    if _FACE_SIDE_RE.search(sentence):
        fams.add("side")
    if "面朝「" in sentence or "面朝门" in sentence or "朝门" in sentence:
        fams.add("anchor")   # 正文自己指定了面朝目标，与镜头相对档位不冲突，交给锚点裁决
    if len(fams) == 1:
        return fams.pop()
    return ""


def _claim_side(sentence: str) -> str:
    """句子里写明的画面左右（同时写左右/含'左边和右边'类并列时不判）。"""
    l, r = bool(_SIDE_LEFT_RE.search(sentence)), bool(_SIDE_RIGHT_RE.search(sentence))
    if l and not r:
        return "left"
    if r and not l:
        return "right"
    return ""


def _fact_side(side_text: str) -> str:
    if "画面右" in side_text:
        return "right"
    if "画面左" in side_text:
        return "left"
    return ""


def _prop_view_states(plot: dict) -> list:
    """每个陈设的机位视野状态：是否入画、画面左右（与 _hidden_props_note 同一套几何口径）。"""
    import math
    cam = plot.get("camera") or {}
    if not isinstance(cam, dict) or "x" not in cam:
        return []
    cx, cy = float(cam.get("x", 0.5)), float(cam.get("y", 0.95))
    rad = math.radians(float(cam.get("facing") or 0) % 360)
    fx, fy = math.sin(rad), -math.cos(rad)
    fov = float(cam.get("fov") or 55)
    out = []
    for p in (plot.get("props") or []):
        label = str(p.get("label") or p.get("name") or "").strip()
        if not label or p.get("x") is None:
            continue
        vx, vy = float(p["x"]) - cx, float(p["y"]) - cy
        dist = math.hypot(vx, vy)
        if dist < 1e-6:
            out.append({"label": label, "in_view": False, "side": ""})
            continue
        depth = vx * fx + vy * fy
        angle = math.degrees(math.acos(max(-1.0, min(1.0, depth / dist))))
        cross = fx * vy - fy * vx
        ok = depth > 0 and dist > 0.15 and angle <= fov / 2 + 25
        out.append({"label": label, "in_view": ok,
                    "side": _frame_side_text(cross, dist, fov) if ok else ""})
    return out


def _scan_prompt_conflicts(scope: str, text: str, plot: dict, facts: dict, dynamic: bool) -> list:
    """逐句扫描一份提示词，与机位几何事实比对，返回冲突列表。"""
    results = []
    s = (text or "").strip()
    if not s:
        return results
    # 段落标签（【画面内容】等）与坐标数据块不参与逐句判定
    body = re.sub(r"[【\[]([^\]】]{1,60})[\]】]", " ", s)
    sentences = [x.strip() for x in re.split(r"[。！？；;\n]", body) if len(x.strip()) >= 4]
    visible = {c["name"]: c for c in facts["visible"]}
    hidden = {c["name"]: c for c in facts["hidden"]}
    props = _prop_view_states(plot)

    def add(sev, ctype, found, fact, advice):
        results.append({"scope": scope, "severity": sev, "type": ctype,
                        "found": found[:80], "fact": fact[:160], "advice": advice[:160]})

    for sent in sentences:
        # ① 人物入画冲突：几何判"不入画"的人被正文写成正在画面里（视频动态模式仅提示）
        for nm, m in hidden.items():
            if nm not in sent or not _PRESENCE_RE.search(sent):
                continue
            if dynamic:
                add("info", "人物入画（视频·初始帧）", sent,
                    f"调度图事实：{nm}{m['reason']}，仅不在0秒初始画面内。",
                    "P1 调度图：初始帧不得出现 TA；若走位/镜头运动把 TA 带入画面则从边缘自然入画。如正文原意如此可忽略。")
            else:
                add("high", "人物入画冲突", sent,
                    f"调度图事实：{nm}{m['reason']}。",
                    "P1 调度图 > P2 手改：该人物本镜不得入画。若剧情需要其存在，改用画外音/门后影子等手法，或修改调度图。")
        # ② 人物朝向冲突：正文朝向词与几何档位家族不同
        for nm, c in visible.items():
            if nm not in sent:
                continue
            fam = _facing_family(c["facing_desc"])
            claim = _claim_family(sent)
            if fam and claim and fam != claim:
                add("medium", "人物朝向冲突", sent,
                    f"调度图事实：{nm}＝{c['facing_desc']}。",
                    "P1 调度图 > P2 手改 > P3 AI 自动生成：按调度图朝向改写该句，或调整 3D 导演台里的朝向后重检。")
            # ③ 画面左右冲突
            cs, fs = _claim_side(sent), _fact_side(c["side"])
            if cs and fs and cs != fs:
                add("medium", "画面左右冲突", sent,
                    f"调度图事实：{nm}在{c['side']}。",
                    "P1 调度图 > P2 手改：按调度图机位几何改写左右方位（图纸方位≠画面方位，程序已换算）。")
        # ④ 陈设冲突：视野外的门窗家具被正文写实；画面左右写反
        for p in props:
            lb = p["label"]
            if lb not in sent:
                continue
            if lb == "门" and ("门声" in sent or "敲门" in sent or "开门声" in sent or "画外" in sent):
                continue   # 画外音/声音提及不算画面冲突
            if not p["in_view"]:
                add("high", "陈设入画冲突", sent,
                    f"调度图事实：「{lb}」在本镜机位视野之外，画面不应出现。",
                    "P1 调度图 > P3 AI 自动生成（可舍弃）：删掉该陈设的画面描述（画外音提及可保留），或到 3D 导演台挪动机位/陈设。")
            else:
                cs, fs = _claim_side(sent), _fact_side(p["side"])
                if cs and fs and cs != fs:
                    add("medium", "陈设左右冲突", sent,
                        f"调度图事实：「{lb}」在{p['side']}。",
                        "P1 调度图 > P2 手改：按机位几何换算的画面左右改写。")
    return results


@app.post("/api/check-prompt-conflicts")
async def check_prompt_conflicts(req: ConflictCheckRequest):
    """一键检测提示词与调度图/3D导演台的冲突。纯规则比对，权威源=compute_camera_facts。"""
    try:
        plot = _normalize_plot(req.plot_data or {})
    except Exception:
        plot = req.plot_data or {}
    has_geo = isinstance(plot, dict) and (plot.get("camera") or {}).get("x") is not None \
        and ((plot.get("characters") or []) or (plot.get("props") or []))
    if not has_geo:
        return {"conflicts": [], "legend": _CONFLICT_LEGEND,
                "checked": False,
                "note": "本镜还没有调度图 / 3D 导演台坐标数据（机位/人物未摆放），没有可比对的 P1 基准；请先在调度图或 3D 导演台摆放人物与机位。"}
    facts = compute_camera_facts(plot)
    conflicts = _scan_prompt_conflicts("生图提示词", req.prompt, plot, facts, dynamic=False)
    conflicts += _scan_prompt_conflicts("视频提示词", req.video_prompt, plot, facts, dynamic=True)
    order = {"high": 0, "medium": 1, "info": 2}
    conflicts.sort(key=lambda c: order.get(c["severity"], 3))
    return {"conflicts": conflicts, "legend": _CONFLICT_LEGEND, "checked": True,
            "summary": {"high": sum(1 for c in conflicts if c["severity"] == "high"),
                        "medium": sum(1 for c in conflicts if c["severity"] == "medium"),
                        "info": sum(1 for c in conflicts if c["severity"] == "info")}}

# ---------- 2.7 AU 表情分析：从提示词捕捉表情/动作关键词，映射到 FACS Action Units ----------
class ExtractAURequest(BaseModel):
    prompt: str

@app.post("/api/extract-au")
async def extract_au(req: ExtractAURequest):
    # 第一层：本地词库匹配（零成本、毫秒级）
    base = au_lexicon.extract_aus(req.prompt)
    base_matches = base["matches"]
    base["tips"] = au_lexicon.AU_TIPS

    # ⚠️ 关键：词库命中**不等于分析完了**。用户一句话里常同时写好几个表情
    # （例："焦急地等消息，又怕又盼"），词库只收录了其中一部分，旧逻辑在这里
    # 直接 return，剩下的词永远不会被 AI 补上——这就是"分析不到全部词语"的根因。
    # 现在改成：把已识别的交给 AI 当"已知"，只让它补遗漏的，再合并去重。
    # 已识别 ≥6 组时收益很低，直接返回省一次等待。
    if not _dsk_ok() or len(base_matches) >= 6:
        base["source"] = "lexicon"
        return base

    known_desc = "、".join(f"{m['label']}（{m['au']}）" for m in base_matches) or "（无）"
    try:
        response = _dsk().chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": (
                    "你是 FACS（面部动作编码系统）表情分析器。给你一段提示词、AU 速查表，"
                    "以及**词库已识别**的表情条目。你的任务：补全提示词里其余**所有**表情与"
                    "面部/头部动作，映射到 AU 编码组合。规则："
                    "① 只输出词库没覆盖的部分，已识别项一律不要重复输出；"
                    f"② 只允许使用速查表中存在的 AU 编码（含 AU51-AU58 头部动作）；最多输出 6 组；"
                    "③ 脸型、年龄、外貌、体重、服饰、身份描述（鹅蛋脸/清瘦/少年/铠甲等）一律忽略；"
                    "④ 一句话里出现多个情绪或情绪交叠（如「又惊又喜」「哭笑不得」「焦急又期待」），"
                    "必须分别输出、不要合并成一条；"
                    "⑤ label 用不超过 12 字的中文概括；确实没有遗漏就输出空的 matches。"
                    '严格只输出 JSON：{"matches": [{"au": "AU12+AU6", "label": "真心笑"}]}，'
                    "不要 markdown，不要任何解释文字。"
                )},
                {"role": "user", "content": (
                    f"AU速查表：{json.dumps(au_lexicon.AU_TABLE, ensure_ascii=False)}\n\n"
                    f"词库已识别：{known_desc}\n\n"
                    f"提示词：{req.prompt}"
                )},
            ],
            response_format={"type": "json_object"},
            max_tokens=500,
            temperature=0.2,
            timeout=9,
        )
        data = deepseek_json(response)
        added = []
        seen_au = {str(m["au"]).replace(" ", "") for m in base_matches}
        seen_lbl = {str(m["label"])[:4] for m in base_matches}
        for m in (data.get("matches") or [])[:6]:
            au = str(m.get("au") or "").strip()
            label = str(m.get("label") or "").strip()[:12]
            if not re.search(r"AU\d+", au):
                continue
            if au.replace(" ", "") in seen_au or (label and label[:4] in seen_lbl):
                continue
            seen_au.add(au.replace(" ", ""))
            seen_lbl.add(label[:4])
            added.append({"au": au, "label": label, "keywords": []})

        if added:
            merged = (base_matches + added)[:16]
            codes, seen_code = [], set()
            for m in merged:
                for code in re.findall(r"AU\d+", m["au"]):
                    if code not in seen_code:
                        seen_code.add(code)
                        codes.append(code)
            log_prompt("AU分析(词库+AI补齐)", req.prompt[:120],
                       f"词库={len(base_matches)}条 补齐={len(added)}条 → {[m['au'] for m in added]}")
            return {"matches": merged, "au_string": "+".join(codes),
                    "source": "hybrid" if base_matches else "ai", "tips": au_lexicon.AU_TIPS}
    except Exception as e:
        print(f"[AU分析] AI 补齐失败，按词库结果返回: {type(e).__name__}: {e}")

    base["source"] = "lexicon"
    return base

# ---------- 3. 豆包生图 (存本地返回 URL) ----------
MAX_REF_IMAGES = 10  # 豆包 seedream 参考图上限

def select_relevant_references(pool: list[str], meta: list[Dict[str, Any]] | None, context: str) -> list[int]:
    """从候选参考图中筛选与当前画面相关的子集（豆包上限 10 张）。

    策略：
    0. 调度图（kind=diagram，俯视走位/运动轨迹）是导演定稿，永远保留且排在最前，不参与筛选
    0.5 机位预览（kind=shot_preview，导演台成片构图渲染）是构图基准，同样永不筛掉
    1. 总数不超限 → 全部使用（不加延迟）
    2. 超限 → 先按名字出现在上下文中做关键词匹配（零开销）
    3. 关键词匹配后仍超限或匹配为空 → DeepSeek 按语义挑选相关的（角色优先）
    4. 兜底：任何环节失败则取前 10 张（调度图始终保留）
    """
    meta = meta or []
    diagram_idx = [i for i, m in enumerate(meta[:len(pool)]) if str(m.get("kind") or "") == "diagram"]
    # 机位预览帧：导演台成片构图渲染，与调度图同等优先级永不筛掉
    preview_idx = [i for i, m in enumerate(meta[:len(pool)]) if str(m.get("kind") or "") == "shot_preview"]
    # 接上一帧：上一镜头末帧是连续镜头的衔接锚点，与调度图同等优先级永不筛掉
    prev_idx = [i for i, m in enumerate(meta[:len(pool)]) if str(m.get("kind") or "") == "prev_frame"]

    def _finalize(picked: list[int]) -> list[int]:
        """合并调度图/预览帧/末帧与筛选结果：调度图、预览帧、末帧排最前，去重、限流、保序"""
        seen, ordered = set(), []
        for i in diagram_idx + preview_idx + prev_idx + list(picked):
            if 0 <= i < len(pool) and i not in seen:
                seen.add(i)
                ordered.append(i)
        return ordered[:MAX_REF_IMAGES]

    if len(pool) <= MAX_REF_IMAGES:
        return _finalize(list(range(len(pool))))

    # 第一步：关键词匹配
    kw_hits = []
    for i, m in enumerate(meta[:len(pool)]):
        if str(m.get("kind") or "") in ("diagram", "shot_preview", "prev_frame"):
            continue
        name = str(m.get("name") or "").strip()
        if name and len(name) >= 2 and name in context:
            kw_hits.append(i)
    if 0 < len(kw_hits) <= MAX_REF_IMAGES:
        print(f"[参考图筛选] {len(pool)} 张 -> 关键词命中 {len(kw_hits)} 张: {kw_hits}（调度图固定保留 {len(diagram_idx)} 张）")
        return _finalize(kw_hits)

    # 第二步：DeepSeek 语义精选（调度图已固定保留，只筛其余的）
    try:
        budget = max(MAX_REF_IMAGES - len(diagram_idx), 0)
        candidates = []
        for i in range(min(len(pool), len(meta)) if meta else 0):
            if i in set(diagram_idx) or i in set(preview_idx) or i in set(prev_idx):
                continue
            m = meta[i]
            candidates.append({
                "index": i,
                "kind": m.get("kind", ""),
                "name": str(m.get("name") or ""),
                "description": str(m.get("description") or "")[:80],
            })
        if candidates and budget > 0:
            response = _dsk().chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": (
                        "你是分镜参考图筛选器。给定画面描述和候选参考图列表（index/kind/name/description），"
                        "挑选与该画面相关的参考图。规则：只输出 JSON {\"indices\": [..]}；"
                        "最多选 " + str(budget) + " 个；无关的不要选；"
                        "画面中出现某角色时必须选该角色的参考图；场景相关的场景图优先。"
                    )},
                    {"role": "user", "content": f"画面描述：{context}\n\n候选：{json.dumps(candidates, ensure_ascii=False)}"},
                ],
                response_format={"type": "json_object"},
                max_tokens=200,
                temperature=0.1,
            )
            data = deepseek_json(response)
            picked = [int(i) for i in data.get("indices", []) if isinstance(i, (int, str)) and str(i).lstrip("-").isdigit()]
            picked = [i for i in picked if 0 <= i < len(pool)]
            # 去重保序，角色优先排序（融脸照片跟随其角色，视同角色优先级）
            seen = set()
            ordered = []
            for i in sorted(set(picked), key=lambda x: (0 if (meta[x].get("kind") if x < len(meta) else "") in ("character", "face") else 1, x)):
                if i not in seen:
                    seen.add(i); ordered.append(i)
            if ordered:
                print(f"[参考图筛选] {len(pool)} 张 -> DeepSeek 精选 {len(ordered[:budget])} 张: {ordered[:budget]}（调度图固定保留 {len(diagram_idx)} 张）")
                return _finalize(ordered[:budget])
    except Exception as e:
        print(f"[参考图筛选] DeepSeek 精选失败，回退前 {MAX_REF_IMAGES} 张: {type(e).__name__}: {e}")

    # 兜底：调度图 + 其余按原顺序取满
    others = [i for i in range(len(pool)) if i not in set(diagram_idx)]
    return _finalize(others[:max(MAX_REF_IMAGES - len(diagram_idx), 0)])

# ---------- 调试日志：把每次发给模型的提示词追加写入文件，便于排查"生成内容不对" ----------
def log_prompt(kind: str, prompt: str, extra: str = ""):
    try:
        from datetime import datetime
        with open("debug_prompts.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {kind} {extra}\n{prompt}\n{'-'*60}\n")
    except Exception:
        pass

# ---------- 导演显式落位检测：重写提示词里直接写了"某人在画面XX"，必须压过程序算的默认落位 ----------
_FRAME_POS_KEYWORDS = [
    # 长词在前，命中多个时取最长（"画面右上角"优先于"画面右"）
    "画面右上角", "画面左上角", "画面右下角", "画面左下角",
    "画面右上方", "画面左上方", "画面右下方", "画面左下方",
    "画面正中间", "画面正中", "画面中央", "画面上方", "画面下方",
    "画面右侧", "画面左侧", "画面右", "画面左",
    "右上角", "左上角", "右下角", "左下角", "正中间", "正中",
    "居中", "中央", "偏右", "偏左", "靠右", "靠左",
]

def _detect_frame_position_overrides(prompt: str, characters: list) -> str:
    """扫描导演最终确认的文字提示词：某句话里同时出现角色名与画面落位词
    （画面右上角/画面左/居中/偏右…）即视为导演对落位的显式指令。
    若不注入覆盖声明，尾部权威名单里的默认"画面左/右"会把导演指令判为冲突作废，
    导致重写永远不生效（生成结果总回到图纸几何位置）。返回"人→落位"清单文本。"""
    import re
    if not prompt:
        return ""
    names = []
    for c in characters or []:
        n = str((c or {}).get("name") or "").strip()
        if n and n in prompt:
            names.append(n)
    # 紧邻落位词前如果是"面向/朝/望向"等词，那是视线或运动方向，不是落位
    _GAZE_PREFIXES = ("面向", "朝向", "望向", "看向", "望着", "走向", "跑向", "扑向",
                      "冲向", "飞向", "朝着", "指", "面朝", "望", "朝", "看")
    hits: list[str] = []

    def _add(entry: str):
        if entry not in hits:
            hits.append(entry)

    for sentence in re.split(r"[。！？\n]", prompt):
        cur_name = None  # 当前句主语：逗号分句没点名时沿用最近提到的人
        # 按逗号/顿号也切开，避免"小帅在画面中央，小美靠左"两句被并成一句错配
        for clause in re.split(r"[；，,、：:]", sentence):
            # 主语识别先行：即使本分句只有视线用法，后面分句仍可沿用主语
            hit_names = [n for n in names if n in clause]
            if hit_names:
                cur_name = hit_names[-1]
            matched = {k for k in _FRAME_POS_KEYWORDS if k in clause}
            # 去掉被同句更长关键词包含的短词（"画面右上角"吃掉"右上角""画面右"）
            kws = [k for k in matched if not any(k != o and k in o for o in matched)]
            for kw in sorted(kws, key=len, reverse=True):
                # 每次出现都检查前缀：整句只有视线用法时跳过
                is_gaze_only = True
                start = 0
                while True:
                    i = clause.find(kw, start)
                    if i < 0:
                        break
                    prefix = clause[max(0, i - 2):i]
                    if not prefix.endswith(_GAZE_PREFIXES):
                        is_gaze_only = False
                    start = i + len(kw)
                if is_gaze_only:
                    continue
                if "主体" in clause or "构图" in clause:
                    # 无角色名的构图指令（如"构图主体略偏右"）按导演指令对待
                    _add("画面主体→" + kw)
                    continue
                if cur_name:
                    _add(f"{cur_name}→{kw}")
    return "；".join(hits[:8])


@app.post("/api/generate-image")
async def generate_image(req: ImageRequest):
    # 入口统一净化：剥掉 AU 编码（生图模型不认识），正文里的中文情绪词照常保留
    req.prompt = _clean_au_codes(req.prompt or "")
    if req.context:
        req.context = _clean_au_codes(req.context)
    log_prompt("生图", req.prompt, f"size={req.size} 参考图={len(req.reference_urls or [])}张")
    payload = {
        "model": DOUBAO_MODEL,
        "prompt": req.prompt,
        "size": req.size,
        "response_format": "b64_json",
        "watermark": False,
    }
    missing_refs: list[str] = []  # 无参考图的调用路径也要初始化（首次生成设定图时 reference_urls 为空）
    n_diag = 0                    # 本镜送了几张调度图纸（>0 = 走导演定稿的构图注入 + 不算篇幅压缩）
    if req.reference_urls and len(req.reference_urls) > 0:
        # 先按相关性筛选（关键词 -> DeepSeek），再转换；豆包云端无法下载本机 127.0.0.1 URL，
        # 本地图一律转 base64 data URI 直传，公网 URL 原样保留
        context = (req.context or req.prompt or "").strip()
        # 先按机位几何剔除"绝不入画"角色的参考图：生图模型看到角色参考图就会把他画进画面，
        # 提示词里的"绝不入画名单"拗不过参考图，必须在发送前过滤（调度图/场景/道具不受影响）
        pool = list(req.reference_urls or [])
        metas = list(req.reference_meta or [])
        # 空镜保护：提示词声明"空镜头/画面中无人物"时，角色与道具参考图一律不发——
        # 生图模型看到参考图就会把他们画进画面，提示词里的"无人物"拗不过参考图
        empty_shot = any(k in context for k in (
            "空镜", "画面中无人物", "画面里无人物", "无人物", "没有人物", "画面中没有人"))
        # 影子镜头保护：提示词只拍影子/倒影/剪影时，角色参考图和入画名单会让模型把
        # 人物本体画进画面（用户实测"特写影子"出图成了真人站立）——与空镜同理，
        # 角色参考图一律不发，并注入影子镜头规则压过入画名单
        _shadow_kw = ("影子", "倒影", "剪影", "人影")
        _person_too = ("和影子", "与影子", "影子同框", "人物本体", "人与影子", "本体",
                       "站在画面", "出现在画面", "人物出镜", "入画", "站在画面里")
        shadow_hit = any(k in context for k in _shadow_kw)
        shadow_only = shadow_hit and not any(k in context for k in _person_too)
        vis_names = _visible_character_names(req.plot_data or {})
        if empty_shot or vis_names is not None:
            allowed = set(vis_names) if vis_names is not None else None
            f_pool, f_meta, removed = [], [], []
            for i in range(len(pool)):
                m = metas[i] if i < len(metas) else {}
                kind = str(m.get("kind") or "")
                name = str(m.get("name") or "").strip()
                if empty_shot and kind in ("character", "prop", "face"):
                    removed.append(f"{name or kind}（空镜）")
                    continue
                if shadow_only and kind in ("character", "face"):
                    removed.append(f"{name or kind}（影子镜头）")
                    continue
                if kind in ("character", "face") and allowed is not None and name and name not in allowed:
                    removed.append(name)
                    continue
                f_pool.append(pool[i]); f_meta.append(m)
            if removed:
                log_prompt("参考图过滤(不入画角色/空镜)",
                           f"剔除：{'、'.join(removed)}"
                           + (f"；保留入画角色：{'、'.join(vis_names) or '无'}" if allowed is not None else ""))
            pool, metas = f_pool, f_meta
        # 场景设定图的"环境陈设必须与此图一致"会压过提示词里的"门不入画"（图片强于文字，
        # 实测门从场景图漏进画面右缘）：有调度数据时给场景参考说明追加取舍规则——
        # 视野外的门窗严禁入画，陈设位置以调度图纸为准
        if req.plot_data:
            _scene_note = _hidden_props_note(req.plot_data)
            if _scene_note:
                for _i in range(len(metas)):
                    if isinstance(metas[_i], dict) and str(metas[_i].get("kind") or "") == "scene":
                        _d = str(metas[_i].get("description") or "")
                        metas[_i]["description"] = (_d + "。" if _d else "") + _scene_note
                log_prompt("场景参考取舍", _scene_note[:140])
        picked = select_relevant_references(pool, metas, context)
        # 容错：项目搬运/换电脑后 static/uploads 里可能没有旧参考图文件（此前会报
        # "参考图文件不存在"直接炸掉整个生图）。缺失的参考图跳过、不阻断生成，
        # 丢失名单随响应带回前端提示；用户重新上传对应参考图即可恢复一致性。
        # 注意必须在 picked 上剔除（而不是在 resolved 里跳过）——后面 n_diag/指令注入
        # 都按 picked 索引数 meta，剔除后张数才不会错位。
        _KIND_LABEL = {"character": "角色", "scene": "场景", "prop": "道具", "diagram": "调度图", "face": "融脸照片"}
        missing_refs = []
        _kept = []
        for i in picked:
            u = pool[i]
            if u.startswith(("http://127.0.0.1", "http://localhost", "/static/")) and local_static_path(u) is None:
                m = metas[i] if i < len(metas) else {}
                label = str(m.get("name") or "").strip()
                kind = _KIND_LABEL.get(str(m.get("kind") or ""), "")
                missing_refs.append(f"{kind}「{label}」" if label else u.split("/")[-1])
                continue
            _kept.append(i)
        picked = _kept
        if missing_refs:
            print(f"⚠️ 跳过 {len(missing_refs)} 张丢失的参考图（继续生成）: {missing_refs}")
        resolved = []
        for i in picked:
            u = pool[i]
            if u.startswith(("http://127.0.0.1", "http://localhost", "/static/")):
                resolved.append(local_url_to_data_uri(u))
            else:
                resolved.append(u)
        if resolved:
            payload["image"] = resolved[:MAX_REF_IMAGES]  # 最终安全上限

        # 调度图（俯视走位/运动轨迹）是导演定稿，优先级高于文字提示词：
        # 注入强制约束，让构图布局以调度图为准，文字只负责画风/长相/服装/细节
        # 注意：picked 是过滤后 metas（机位剔除不入画角色后）的索引，不能用原始 reference_meta
        n_diag = sum(
            1 for i in picked
            if i < len(metas) and str(metas[i].get("kind") or "") == "diagram"
        )
        if n_diag:
            # 收集调度图的口述说明（前端把口述内容放在 diagram 参考的 description 里）
            notes = []
            for i in picked:
                if i < len(metas) and str(metas[i].get("kind") or "") == "diagram":
                    d = str(metas[i].get("description") or "").strip()
                    n = str(metas[i].get("name") or "").strip() or "调度图"
                    if d:
                        notes.append(f"▸{n}：{d}")
            # 完整版调度指令（对齐 3D 桌面版这套已跑通的口径：四规则 + 口述 + 纵深解读 +
            # 坐标翻译 + 落位覆盖 + 结尾重申，全部追加在 prompt 末尾——末尾服从度最高）。
            # 此前移动端为了"省字数"把它砍成 300 字并挪到开头，实测反而完全不生效。
            directive = (
                f"【机位与调度（最高优先级）】本图的前 {n_diag} 张参考图是导演定稿的调度图纸："
                "正上方垂直俯视的平面图（白底图纸，人偶棋子图标＝人物、旁边标签是人名，「机位」符号＋扇形视锥＝摄影机位置与拍摄朝向，虚线箭头＝运动路线）。"
                "若图中出现红框且框外区域被压暗，红框＝导演画定的取景范围。本镜头的观察点就在机位符号处、朝视锥方向看：\n"
                "规则一·机位定视角：镜头架在「机位」符号处、朝视锥扇形方向拍摄，画面内容＝从机位望出去的景象——"
                "站在机位上的人物是视角人物（镜头从他眼中拍出），视角人物、紧贴机位旁的人物与机位身后的人物一律不出现在画面中；"
                "机位视线前方的人物按真实透视入画——离机位越近画面里显得越大（可为前景，可能只露出腿部、肩膀或部分背影），越远越小、靠近地平线。"
                "特别注意：画面主体的位置由机位视角决定，不因他在文字提示词里是主角就把镜头架到他那边去拍。\n"
                "规则二·图纸定位置：人物在画面中的左右按「站在机位处朝拍摄方向看」换算（机位视线左侧＝画面左侧，不得机械套用图纸方位）；"
                "人物之间的间距与前后遮挡按图纸比例，挨得近的画得挨近，禁止均匀散布。"
                "人物的位置与相对远近由图纸锁定；每个人的动作、表情、长相、服装、所持道具由文字提示词决定——调度只安排在哪里，不改变任何人在做什么。\n"
                "规则三·景别定取景：景别、主体、构图、画风按文字提示词。景别只是取景框的远近：特写/近景＝取景框只框住主体所在区域，"
                "其他人自然落在框外看不见，严禁把他们挪到主体旁边、缩小塞进画面或为露脸改变位置；"
                "全景时所有人按规则一的透视呈现。同一地面人物身高一致，纵深相近者大小相近，严禁巨人/小矮人；"
                "严禁为了让某人入画或完整而挪动位置、违例缩放；处在取景框边缘的人可被画面裁切。\n"
                "规则四·红框约束：红框只是导演画定的取景范围（画面边缘裁切参考），不是机位——镜头位置与朝向只由「机位」符号决定，"
                "严禁把红框理解为镜头所在处、或朝红框方向拍摄。画面内容严格限制在红框内——压暗区域里的人与景一律不画（即使文字提示词提到了他们）；"
                "红框与机位真实视野冲突时，以机位与真实透视为准，红框只作构图参考。\n"
                "注意：最终输出的是正常视角的真实镜头画面，不要画成俯视图、示意图或图纸形式；画面里的人物是成片形象，不是图标。\n"
            )
            # 场景空间锚定：图纸是白底俯视站位图，没有任何室内外线索，模型容易顺势把
            # 室内场景画成露天（实测：正文写屋内织布，成图整个搬到室外）。按正文空间词锚定。
            _scene_txt = str(req.prompt or "") + " " + str(req.context or "")
            if _INDOOR_MARK.search(_scene_txt):
                directive += (
                    "▸ 场景空间＝室内（最高优先级）：墙体、门窗、屋顶、梁柱等室内结构必须画出，"
                    "俯视图纸只是站位平面图、不代表露天场地，严禁画成室外或空旷场地；\n"
                )
            elif _OUTDOOR_MARK.search(_scene_txt):
                directive += "▸ 场景空间＝室外：按正文地点与环境要素执行，图纸只定站位与机位、不定室内外；\n"
            if notes:
                directive += ("导演口述的调度要求（注意：机位位置与朝向、谁入画、每人身体朝向一律以上面图纸坐标数据为准——"
                              "导演手动移过机位后口述里的旧机位描述作废；口述只用来补充动作、剧情与细节）：\n"
                              + "\n".join(notes) + "\n")
            else:
                directive += "请观察调度图纸中人偶图标的站位与朝向、虚线运动路线、机位符号与视锥方向，把上述空间关系落到画面里。\n"
            if req.diagram_brief:
                directive += f"【画面纵深调度解读】（机位视角换算的辅助参考）：{req.diagram_brief.strip()}\n"
            # 坐标翻译：生图模型看不懂俯视图纸上的图标，但听得懂"谁离镜头最近/在哪侧/谁不入画/朝哪边"。
            # 优先 AI 镜头翻译（按坐标+程序几何事实产出，内容哈希缓存），失败自动回退程序直译。
            plot_txt = await build_plot_prompt_text(
                req.plot_data or {}, (req.prompt or "") + " " + (req.context or ""), dynamic=False)
            if plot_txt:
                directive += "【调度图纸坐标数据（程序按坐标翻译，权威）】\n" + plot_txt + "\n"
            # 机位预览帧：导演台成片构图的实时渲染——用户要求"最后出图以预览窗所见为准"。
            # 指明它是第几张参考图并声明构图权威性；同时压住"把示意模型画进画面/照抄示意渲染风格"的风险
            _pv_notes = []
            for k, i in enumerate(picked):
                if i < len(metas) and str(metas[i].get("kind") or "") == "shot_preview":
                    _pv_notes.append(f"第{k + 1}张参考图")
            if _pv_notes:
                directive += (
                    f"【机位预览·构图基准（最高优先级）】{'、'.join(_pv_notes)}是导演 3D 导演台的机位预览"
                    "（从镜头位置按机位坐标实时渲染的本镜构图）：最终画面的人物站位、画面左右、远近大小层次、"
                    "身体朝向、谁被谁遮挡、机位高度与视角必须与该图一致——该图就是成片应该达到的构图。"
                    "但图中的彩色胶囊、方块、锥体只是站位示意模型：人物真实长相/发型/服装按角色设定图与提示词执行，"
                    "家具门窗真实样式按场景设定图与提示词执行；严禁把胶囊人偶、几何方块、琥珀色锥体等示意元素画进画面；"
                    "画风、光线、色调按画风设定与提示词执行，不参照该图的示意渲染风格。\n"
                )
            # 导演在重写框里直接指定的画面落位（画面右上角/偏右/居中…）必须压过程序算的默认落位——
            # 否则权威名单里的"画面左/右"会把导演指令判为冲突作废，重写永远不生效
            override_txt = _detect_frame_position_overrides(
                req.prompt, (req.plot_data or {}).get("characters") or [])
            if override_txt:
                directive += (
                    f"【导演落位指令·最高优先级】导演已在本条提示词中直接指定画面落位：{override_txt}。"
                    "这是导演的显式构图指令，人物在成片画面中的实际落位以导演指定为准；"
                    "上面名单与纵深解读中的「画面左/右」只是按调度图纸几何推算的默认落位，"
                    "与导演指定冲突时按导演指定执行，不算文字冲突、不作废。"
                    "但谁入画谁不入画、人物身体朝向、远近大小关系仍按上面名单与透视规则执行——导演落位指令只改变人物在画面中的方位。\n"
                )
            directive += (
                "【结尾重申·最高优先级】请逐条核对后再出图：①镜头位置与朝向只由「机位」符号决定；"
                "②画面中出现的人物必须且只能是入画名单里的人，绝不入画名单里的人一个都不能出现；"
                "③人物远近与大小按入画名单从近到远按透视排列；"
                "④人物身体朝向逐人核对：写「正面对镜头」的必须正脸朝向其被指定的面朝目标（面朝「门」就看着门，未指定时朝观众），写「完全背对镜头」的只能画背影、画面里严禁出现他的脸，写「侧对镜头」的只露写明那一侧的侧脸；被指定了面朝目标的人严禁为露脸而转向镜头；"
                "⑤若存在【导演落位指令】，被指定人物在画面中的落位按导演指定执行；"
                "⑥最终输出正常视角的真实镜头画面，不是俯视图或示意图。\n"
            )
            # 🌟 强指令放在 prompt 末尾：大模型对末尾指令的服从度最高，放开头容易被当背景介绍忽略
            payload["prompt"] = payload["prompt"] + "\n\n" + directive
            log_prompt("调度约束注入(尾部·全量)", f"调度图 {n_diag} 张\n" + "\n".join(notes)
                       + f"\nbrief={req.diagram_brief or ''}\nplot_data={'有' if req.plot_data else '无'}\n"
                       + f"坐标翻译={'有' if plot_txt else '无'}\n---\n" + directive, "")

        # 影子镜头规则：挪到所有注入之后（prompt 最终末尾）再注入——末尾服从度最高，
        # 原位置在调度约束之后、外貌锚点/融脸之前，会被后续"画人"指令稀释（实测仍会出人形）
        # （正式规则在下方融脸块之后追加，确保是 prompt 的最后一段）

        # 角色外貌锚点：角色卡上最终确定的外貌（导演重写/识图提取后的最新版）是权威基准。
        # 剧本初稿生成时把当时的外貌整段复制进了各分镜提示词，之后导演改了角色卡，
        # 旧分镜提示词里的过时外貌（如性别写反、发型不符）会把参考图往回拉——锚点必须压过它。
        # 前端把每位角色的最新外貌放在 character 参考图元数据的 description 里带来。
        # 影子镜头（shadow_only）时跳过：外貌锚点是"画人"指令，会把模型往人物本体上拉
        anchor_lines = []
        _anchor_map: dict[str, str] = {}
        # 本镜正文是否已经写清楚穿什么：写了就不再让锚点重复另一套服装（角色卡常是铠甲造型，
        # 与本镜的民女打扮并存时模型会随机挑一套，实测出现"布裙镜头画出铠甲"）
        _prompt_says_outfit = bool(_OUTFIT_WORD.search(str(req.prompt or "")))
        for i in range(len(metas)):
            m = metas[i] if i < len(metas) else {}
            if str(m.get("kind") or "") != "character":
                continue
            if m.get("outfit"):
                continue  # 服装变体图只作参考，不单独写入外貌锚点（否则会覆盖本体长相摘要）
            n = str(m.get("name") or "").strip()
            if not n:
                continue
            # 前端传 appearance = 角色卡的 image_prompt 原文（"画设定图"用的），
            # 这里只取外貌与服装分句，丢掉立绘/站姿/纯色背景/画质这类设定图专用要求。
            raw = str(m.get("appearance") or "").strip() or str(m.get("description") or "").strip()
            app = _portrait_only(raw, keep_outfit=not _prompt_says_outfit)
            if app and len(app) > len(_anchor_map.get(n, "")):
                _anchor_map[n] = app
        anchor_lines = [f"▸{n}：{a}" for n, a in _anchor_map.items()]
        if anchor_lines and not shadow_only:
            anchor_txt = (
                "【角色外貌锚点·权威】\n" + "\n".join(anchor_lines)
                + "\n锚点只补本镜提示词没写到的部分：提示词已明确写出的长相/发型/服装一律以提示词为准，"
                "不得用锚点覆盖；锚点也只锁长相与服装，画面构图、站姿、背景、镜头按提示词与调度，不得画成设定图/立绘。\n"
            )
            payload["prompt"] = payload["prompt"] + "\n\n" + anchor_txt
            log_prompt("角色外貌锚点注入", "\n".join(anchor_lines), "")

        # 照片融脸：参考图里 kind=face 的是角色真实面部照片。picked 顺序与 payload["image"] 一一对应，
        # 按最终发送序号指明哪张是融脸照，并把"面部相似度"写成最高优先级指令（放在 prompt 末尾服从度最高）
        face_notes = []
        for k, i in enumerate(picked):
            if i < len(metas) and str(metas[i].get("kind") or "") == "face":
                nm = str(metas[i].get("name") or "").strip() or "对应角色"
                face_notes.append(f"第{k + 1}张参考图是角色「{nm}」的真实人物面部照片")
        if face_notes:
            face_txt = (
                "【融脸·最高优先】" + "；".join(face_notes) + "：生成画面中该角色的脸型五官必须与照片高度相似；"
                "发型/发色/服装/体型/画风仍按设定图与提示词；严禁带入照片的背景衣着与真人写实风格。\n"
            )
            payload["prompt"] = payload["prompt"] + "\n\n" + face_txt
            log_prompt("融脸指令注入", "；".join(face_notes), "")

        # 设定图参考压制：角色设定图（kind=character）本身是"单人全身立绘、正面站姿、纯色背景"，
        # 生图模型看到图就会把"站着的人"照搬进画面——与本镜正文描述的人物并存时直接出
        # "两个父亲"（同角色画两次）。这里按最终发送序号指明哪些张是角色设定图，并注入
        # 强规则：参考图只锁长相服装，姿态/背景/人数以正文为准，严禁复制成第二个人。
        # 放在融脸之后、影子规则之前（影子规则必须保持 prompt 最后一段）。
        char_notes = []
        for k, i in enumerate(picked):
            if i < len(metas) and str(metas[i].get("kind") or "") == "character":
                nm = str(metas[i].get("name") or "").strip()
                char_notes.append(f"第{k + 1}张" + (f"（角色「{nm}」的设定图）" if nm else "（角色设定图）"))
        if char_notes:
            ref_rule = (
                "【角色设定图参考·用途限定（最高优先级）】" + "、".join(char_notes)
                + "。这些参考图的唯一用途是锁定对应角色的长相、发型与服装（照着这个人画）。除此之外：\n"
                "①参考图中的站姿、面向、纯色背景、设定图版式一律不得照搬——人物在本画面中的动作、姿态、朝向、"
                "所处环境完全按提示词正文执行；\n"
                "②画面人数严格等于提示词正文写出的人物数量：参考图里的每个角色只画一次，"
                "严禁把参考图人物复制成第二个人、双胞胎、影子分身或背景路人；\n"
                "③提示词正文没有提到的角色，即使参考图里出现了也绝不入画。"
            )
            payload["prompt"] = payload["prompt"] + "\n\n" + ref_rule
            log_prompt("设定图参考压制注入", "；".join(char_notes), "")

        # 影子镜头规则最终落位：这里是 prompt 组装的最后一步（调度约束/外貌锚点/融脸之后），
        # 追加到最末尾——末尾服从度最高，压过一切"画人"指令。
        # 用户实测教训：只拍影子时模型会画出站立的人形/半透明幽灵（"鬼"），规则必须
        # ①放最末尾 ②明说"影子是贴地的二维投影，不是立体人形"。
        if shadow_hit:
            if shadow_only:
                rule = (
                    "【影子镜头·最高优先级（压过前文所有关于人物入画/外貌的指令）】导演本镜头拍的是投在地面或墙面上的影子/倒影/剪影：\n"
                    "①影子是紧贴地面/墙面的【二维扁平暗色投影】，只有轮廓形状——没有身体厚度、不是立体物、不占空间、不发光；\n"
                    "②画面中严禁出现任何人物本体：真人、全身、半身、背影、站立的身体、探出的手脚都不行，画面里只能有影子；\n"
                    "③严禁把影子画成第二个人、半透明人形、幽灵、鬼魂、灵体、飘浮虚影、发光人形——影子只能平贴在地面/墙面上，"
                    "绝不站立、绝不悬空、绝不从影子里长出身体；\n"
                    "④影子轮廓按人物身份与动作刻画（可从轮廓辨认是谁在做什么，如长发裙摆、奔跑姿态），但它始终只是地面/墙面上的暗色扁平投影；\n"
                    "⑤前文入画名单、调度约束、外貌锚点里的「谁入画」「谁出现」，在本镜头一律指「谁的影子出现在画面里」，不是人物本人；\n"
                    "⑥画面中除影子外不能存在任何其他人形物体——出现任何站立/悬浮的人形即视为错误。"
                )
            else:
                rule = (
                    "【影子镜头规则】本镜头同时包含人物本体与影子/倒影/剪影：\n"
                    "①影子是紧贴地面/墙面的二维扁平暗色投影，按光源方向绘制，没有身体厚度、不发光、不悬空；\n"
                    "②严禁把影子单独画成半透明人形、幽灵、鬼魂、第二个人——影子只是本体的投影，与本体的动作/轮廓对应；\n"
                    "③人物本体数量以外貌锚点/入画名单为准，影子不额外增加人形数量。"
                )
            payload["prompt"] = payload["prompt"].replace("__SHADOW_RULE__", rule)
            log_prompt("影子镜头规则注入(末尾)", f"shadow_only={shadow_only}（{'已剔除角色参考图与外貌锚点' if shadow_only else '人物+影子同框，规则放宽'}）", "")

    # 不做篇幅压缩：调度指令是构图能否执行的唯一依据，任何"省字数"压缩都会把它吃掉
    # （实测教训：780 字硬压缩把 2500 字构图指令压成 55 字，成图完全不听图纸）。
    # 3D 桌面版从不压缩、用同一套指令跑得通，说明模型吃得住这个长度。

    headers = {
        "Authorization": f"Bearer {_require_own_key('ark') or _eff_key('ark')}",
        "Content-Type": "application/json",
    }
    
    t0 = __import__("time").time()
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            resp = await client.post(
                f"{DOUBAO_BASE}/images/generations",
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError as e:
            # 超时/断网：云端可能已生成并计费，但本地拿不到图——记下来便于和方舟账单对账
            log_prompt("生图超时/中断(云端可能已计费)", f"{type(e).__name__}: {e}", f"耗时{__import__('time').time()-t0:.0f}s")
            raise HTTPException(504, f"生图请求超时或中断（云端可能已计费）：{type(e).__name__}，请稍后再试")

        if resp.status_code != 200:
            print("\n❌ 豆包 API 报错详情:", resp.text, "\n")
            log_prompt("生图失败", resp.text[:300], f"HTTP {resp.status_code} 耗时{__import__('time').time()-t0:.0f}s")
            raise HTTPException(resp.status_code, f"豆包生图失败：{resp.text}")

        data = resp.json()
        b64_data = data["data"][0]["b64_json"]
        # 记录豆包实际返回几张（确认是否存在"生成了多张但只显示一张"的情况）
        print(f"🖼️ 豆包本次返回图片数: {len(data.get('data', []))}")
        log_prompt("生图成功", f"返回{len(data.get('data', []))}张，耗时{__import__('time').time()-t0:.0f}s")
        img_bytes = base64.b64decode(b64_data)
        
        filename = f"img_{uuid.uuid4().hex}.jpg"
        filepath = os.path.join("static/uploads", filename)
        with open(filepath, "wb") as f:
            f.write(img_bytes)

        result = {"image_url": f"/static/uploads/{filename}"}
        if missing_refs:
            result["warnings"] = (
                f"已跳过 {len(missing_refs)} 张丢失的参考图（文件不在 static/uploads，多半是换电脑/搬项目时没拷 uploads 目录）："
                + "、".join(missing_refs)
                + "。本次已继续生成，但涉及的角色/场景/道具长相可能与之前不一致；到对应角色/场景/道具卡片重新上传参考图即可恢复。"
            )
        return result

class UploadImageRequest(BaseModel):
    image_base64: str  # data URI 或纯 base64
    filename: str | None = None

# ---------- 3.5 本地图片上传（存到 uploads，返回 URL 供分镜/生视频使用） ----------
# ---------- 3.55 冲突检测：角色名单 × 调度图纸 × 分镜提示词 矛盾清单 ----------
class CheckConflictsRequest(BaseModel):
    characters: List[Dict[str, Any]] = []
    scenes: List[Dict[str, Any]] = []
    props: List[Dict[str, Any]] = []
    storyboards: List[Dict[str, Any]] = []

CONFLICT_CHECK_PROMPT = """你是动画项目的制片审校。给你角色/场景/道具名单与各分镜的文本材料，找出设定互相矛盾的地方。
【检查维度】性别、年龄、体型、发型发色、瞳色、服装、身份/职业、称谓关系、各分镜出场人物与名单是否一致。
【优先级规则】标注「用户已修改」的内容代表导演的明确意志，优先级最高：矛盾时以它为基准，报告另一份（AI 生成的）需要修改。用户手动上传的参考图同理优先。
【注意】
- 同一角色的「设定描述」与「生图提示词」若不一致，按修改标记判断以哪份为准。
- 只报告真实矛盾，不要把画风差异、无关细节凑数。
- 每条给出具体位置与可直接执行的修改建议（改哪一份、怎么改）。
【输出】严格只输出 JSON，不要 markdown：{"conflicts":[{"where":"位置(如:分镜3 / 角色:憨憨)","type":"矛盾类型(如:性别冲突)","detail":"矛盾内容，引用双方原文关键句","suggestion":"具体修改建议","severity":"error或warn"}]}
没有矛盾输出 {"conflicts":[]}"""

def _program_conflicts(req: CheckConflictsRequest) -> list:
    """确定性检查：不需要 AI，零成本先跑一轮。"""
    out, cast = [], []
    for c in (req.characters or []):
        n = str((c or {}).get("name") or "").strip()
        if n:
            cast.append(n)
    dup = sorted({n for n in cast if cast.count(n) > 1})
    if dup:
        out.append({"severity": "error", "where": "角色列表", "type": "同名角色",
                    "detail": f"存在同名角色：{'、'.join(dup)}",
                    "suggestion": "重命名或删除重复角色，否则调度图纸与生图会互相混淆"})
    cast_set = set(cast)
    has_img = {}
    for c in (req.characters or []):
        n = str((c or {}).get("name") or "").strip()
        if n:
            has_img[n] = bool((c or {}).get("imgSrc") or (c or {}).get("has_img"))
    missing_img_reported = set()
    for i, sb in enumerate(req.storyboards or [], 1):
        label = f"分镜{i}"
        for kind, key in (("俯视调度图", "topViewPlot"), ("轨迹调度图", "trajViewPlot")):
            plot = sb.get(key)
            if not isinstance(plot, dict):
                continue
            for c in plot.get("characters") or []:
                n = str((c or {}).get("name") or "").strip()
                if n and cast_set and n not in cast_set:
                    out.append({"severity": "error", "where": f"{label}·{kind}", "type": "名单外角色",
                                "detail": f"图纸上出现角色名单里没有的「{n}」",
                                "suggestion": "点该图的「🔄 重生调度图」重新解析口述，或把口述里的称呼改成角色名单里的名字"})
        prompt_txt = " ".join(str(sb.get(k) or "") for k in ("description", "edited_prompt", "image_prompt", "video_prompt"))
        for n in sorted(cast_set):
            if n in prompt_txt and not has_img.get(n, False) and n not in missing_img_reported:
                missing_img_reported.add(n)
                out.append({"severity": "warn", "where": f"{label} 等", "type": "缺参考图",
                            "detail": f"提示词提到角色「{n}」，但该角色还没有参考图",
                            "suggestion": f"先到「角色参考图」为 {n} 生成或上传参考图，再生成相关分镜图/视频，否则人物长相每张图都会变"})
    return out

@app.post("/api/check-conflicts")
async def check_conflicts(req: CheckConflictsRequest):
    """冲突检测：程序确定性检查 + DeepSeek 语义矛盾检查，返回清单供导演逐条手动修改。"""
    conflicts = _program_conflicts(req)

    # ---- DeepSeek 语义检查（性别/外貌/身份等程序判不了的矛盾）----
    lines = ["【角色名单】"]
    for c in (req.characters or [])[:20]:
        n = str((c or {}).get("name") or "").strip()
        if not n:
            continue
        edited = bool(str((c or {}).get("edited_prompt") or "").strip())
        desc = str((c or {}).get("description") or "")[:120]
        prompt = str((c or {}).get("edited_prompt") or (c or {}).get("image_prompt") or "")[:180]
        lines.append(f"- {n}｜用户{'已修改(优先)' if edited else '未修改'}｜设定：{desc}｜生图提示词：{prompt}")
    if req.scenes or req.props:
        lines.append("【场景/道具】")
        for s in (req.scenes or [])[:12]:
            if str((s or {}).get("name") or "").strip():
                lines.append(f"- 场景 {s.get('name')}：{str((s or {}).get('description') or '')[:80]}")
        for p in (req.props or [])[:12]:
            if str((p or {}).get("name") or "").strip():
                lines.append(f"- 道具 {p.get('name')}：{str((p or {}).get('description') or '')[:80]}")
    lines.append("【分镜材料】")
    for i, sb in enumerate((req.storyboards or [])[:40], 1):
        parts = [f"分镜{i}"]
        if str(sb.get("description") or "").strip():
            parts.append(f"画面描述：{str(sb.get('description'))[:150]}")
        if str(sb.get("dialogue") or "").strip():
            parts.append(f"台词：{str(sb.get('dialogue'))[:80]}")
        prompt = str(sb.get("edited_prompt") or sb.get("image_prompt") or "")
        mark = "用户已修改(优先)" if str(sb.get("edited_prompt") or "").strip() else "未修改"
        if prompt.strip():
            parts.append(f"生图提示词({mark})：{prompt[:280]}")
        if str(sb.get("video_prompt") or "").strip():
            parts.append(f"视频提示词：{str(sb.get('video_prompt'))[:180]}")
        if str(sb.get("topViewDesc") or "").strip():
            parts.append(f"俯视口述：{str(sb.get('topViewDesc'))[:120]}")
        if str(sb.get("trajViewDesc") or "").strip():
            parts.append(f"轨迹口述：{str(sb.get('trajViewDesc'))[:120]}")
        if len(parts) > 1:
            lines.append("- " + "｜".join(parts))

    ai_note = ""
    if not _dsk_ok():
        ai_note = "未配置 DeepSeek Key，仅完成程序检查（名单外角色/同名/缺参考图）。"
    else:
        try:
            response = _dsk().chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": CONFLICT_CHECK_PROMPT},
                    {"role": "user", "content": "\n".join(lines)},
                ],
                response_format={"type": "json_object"},
                max_tokens=1500,
                temperature=0.1,
            )
            data = deepseek_json(response)
            for c in (data.get("conflicts") or [])[:20]:
                if isinstance(c, dict) and str(c.get("detail") or "").strip():
                    conflicts.append({
                        "severity": str(c.get("severity") or "warn"),
                        "where": str(c.get("where") or "")[:40],
                        "type": str(c.get("type") or "矛盾")[:20],
                        "detail": str(c.get("detail") or "")[:300],
                        "suggestion": str(c.get("suggestion") or "")[:300],
                    })
        except Exception as e:
            ai_note = f"AI 语义检查失败（{type(e).__name__}），仅完成程序检查。"
            print(f"[check-conflicts] DeepSeek 失败: {e}")
    return {"conflicts": conflicts, "note": ai_note}


# ---------- 参考图识图提取文字描述 ----------
# 场景：角色/场景/道具上传参考图后可能没有文字描述（如 AI 自动生成故事时漏写的角色，
# 或用户只想传图），而下游的分镜提示词拼装、冲突检测都依赖文字描述——用 DeepSeek
# 视觉能力（V4.1 起支持 image 输入）从参考图反推描述与生图提示词。
_REF_VISION_PROMPTS = {
    "character": (
        "你是动画项目的角色设定师。仔细观察这张角色参考图，只描述画面中真实可见的信息。",
        "description：角色外貌中文描述，一句话到两句话，必须涵盖：性别与年龄段、发型发色、五官特征、服装样式与颜色、体格气质。不要编造图中没有的特征，不要写性格剧情。",
        "image_prompt：用于 AI 生图的角色设定图提示词，以人物外貌关键词为主（性别/发型/服装/体格/配色），风格中性，不含背景剧情。",
    ),
    "scene": (
        "你是动画项目的场景美术师。仔细观察这张场景参考图，只描述画面中真实可见的信息。",
        "description：场景中文描述，一句话到两句话，涵盖：地点类型、时间光线氛围、主要景物构成、色调。不要编造图中没有的内容。",
        "image_prompt：用于 AI 生图的场景空镜提示词，以场景构成关键词为主（地点/光线/色调/主要物件），不含人物与剧情。",
    ),
    "prop": (
        "你是动画项目的道具设定师。仔细观察这张道具参考图，只描述画面中真实可见的信息。",
        "description：道具中文描述，一句话，涵盖：物品类型、材质颜色、外观细节特征。不要编造图中没有的内容。",
        "image_prompt：用于 AI 生图的道具设定图提示词，以物品外观关键词为主（类型/材质/颜色/细节）。",
    ),
}


class AnalyzeRefImageRequest(BaseModel):
    image_base64: str | None = None
    image_url: str | None = None     # 本地 /static/... 路径或远程 http(s) URL（与 base64 二选一）
    kind: str = "character"          # character / scene / prop
    name: str | None = None          # 角色/场景/道具名（供提示词参考，可空）


@app.post("/api/analyze-ref-image")
async def analyze_ref_image(req: AnalyzeRefImageRequest):
    """识图提取：从上传的参考图反推文字描述与生图提示词。"""
    if not _dsk_ok():
        raise HTTPException(400, "未配置 DeepSeek API Key，无法识图。请到 ⚙️ API 设置填写。")
    b64 = (req.image_base64 or "").strip()
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[1] if "," in b64 else b64
    if not b64 and (req.image_url or "").strip():
        u = req.image_url.strip()
        try:
            if u.startswith("http://") or u.startswith("https://"):
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as hc:
                    r = await hc.get(u)
                    r.raise_for_status()
                img_bytes = r.content
            else:
                # 本地 /static/... 路径：限制在 static 目录内，防目录穿越
                rel = u.lstrip("/").removeprefix("static/")
                p = Path("static") / rel
                if not p.resolve().is_relative_to(Path("static").resolve()):
                    raise HTTPException(400, "非法的图片路径")
                if not p.exists():
                    raise HTTPException(404, f"图片不存在：{u}")
                img_bytes = p.read_bytes()
            b64 = base64.b64encode(img_bytes).decode()
        except HTTPException:
            raise
        except Exception as e:
            print(f"[analyze-ref-image] 取图失败: {e}")
            raise HTTPException(400, f"无法读取参考图（{type(e).__name__}），请重新上传或生成")
    if not b64:
        raise HTTPException(400, "缺少图片内容（image_base64 或 image_url 二选一）")
    kind = req.kind if req.kind in _REF_VISION_PROMPTS else "character"
    p_intro, p_desc, p_prompt = _REF_VISION_PROMPTS[kind]
    type_label = {"character": "角色", "scene": "场景", "prop": "道具"}[kind]
    name_line = (f"该{type_label}的名字是「{(req.name or '').strip()}」，仅作参考，不要把名字本身写进描述。"
                 if (req.name or "").strip() else "")
    try:
        response = _dsk().chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": (
                    f"{p_intro}\n{name_line}\n用 JSON 返回两个字段：\n{p_desc}\n{p_prompt}"
                )},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
            response_format={"type": "json_object"},
            max_tokens=500,
            temperature=0.2,
        )
        data = deepseek_json(response)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[analyze-ref-image] 识图失败: {e}")
        raise HTTPException(502, f"识图失败：{type(e).__name__}，请稍后重试")
    description = str(data.get("description") or "").strip()
    image_prompt = str(data.get("image_prompt") or "").strip()
    if not description:
        raise HTTPException(502, "识图未返回有效描述，请换一张更清晰的参考图重试")
    return {"description": description[:500], "image_prompt": image_prompt[:600]}

@app.post("/api/upload-image")
async def upload_image(req: UploadImageRequest):
    b64 = req.image_base64.strip()
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[1] if "," in b64 else ""
    try:
        img_bytes = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, "图片数据无效")
    if len(img_bytes) < 100:
        raise HTTPException(400, "图片内容为空或损坏")
    ext = ".png" if (req.filename or "").lower().endswith(".png") else ".jpg"
    filename = f"img_{uuid.uuid4().hex}{ext}"
    filepath = os.path.join("static/uploads", filename)
    with open(filepath, "wb") as f:
        f.write(img_bytes)
    return {"image_url": f"/static/uploads/{filename}"}

# ---------- 3.6 参考视频上传（垫片用：存到 uploads，返回 URL 供 Seedance 垫参考视频） ----------
class UploadRefVideoRequest(BaseModel):
    video_base64: str  # data URI 或纯 base64
    filename: str | None = None

@app.get("/api/download-video")
async def download_video_proxy(request: Request, url: str):
    """同源视频下载代理：豆包成片在火山 CDN（跨域），手机浏览器直连 fetch 会被 CORS 拦，
    回落到"长按保存"等兜底路径时部分浏览器只存视频流、丢掉音频轨（用户实测：网页播放有声、
    下载无声）。经本服务器原样转发完整字节（含音轨），前端拿同源 blob 触发保存。"""
    from fastapi.responses import Response
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        raise HTTPException(400, "无效的视频地址（仅支持 http/https 直链）")
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        r = await client.get(u)
    if r.status_code >= 400:
        raise HTTPException(502, f"从视频源拉取失败（上游 HTTP {r.status_code}），可稍后再试")
    if len(r.content) < 1000:
        raise HTTPException(502, "从视频源拉取到的内容为空，源链接可能已过期")
    return Response(content=r.content, media_type="video/mp4",
                    headers={"Cache-Control": "no-store"})

@app.post("/api/upload-ref-video")
async def upload_ref_video(req: UploadRefVideoRequest):
    b64 = req.video_base64.strip()
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[1] if "," in b64 else ""
    try:
        video_bytes = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, "视频数据无效")
    if len(video_bytes) < 1000:
        raise HTTPException(400, "视频内容为空或损坏")
    raw = (req.filename or "").lower()
    ext = ".mp4"
    for e in (".mp4", ".webm", ".mov", ".m4v"):
        if raw.endswith(e):
            ext = e
            break
    filename = f"refvideo_{uuid.uuid4().hex}{ext}"
    filepath = os.path.join("static/uploads", filename)
    with open(filepath, "wb") as f:
        f.write(video_bytes)
    print(f"📥 参考视频已上传: {filename} ({len(video_bytes) / 1024 / 1024:.1f}MB)")
    return {"video_url": f"/static/uploads/{filename}"}

# ---------- 3.5 配音：VoxCPM 声音克隆 / 文本转语音（语音服务独立进程，未启动时端点明确报错不拖垮主流程） ----------
class UploadVoiceRequest(BaseModel):
    audio_base64: str
    filename: str | None = None

class GenerateDubRequest(BaseModel):
    text: str
    voice_url: str | None = None        # 角色音色参考音频（/static/uploads/voice_*.wav），空 = 默认音色
    voice_text: str | None = None       # 参考音频的文字内容（克隆必填）
    preset_id: str | None = None        # 预设音色 ID（voice_presets.json 里的 id），如 preset_yj
    cfg_value: float = 2.0
    inference_timesteps: int = 10

# 预设音色包：static/voice_presets/ 下 mp3 样本 + manifest（Edge TTS 生成的高质量中文人声，作 VoxCPM 克隆源）
def _load_voice_presets() -> dict:
    try:
        with open("static/voice_presets/manifest.json", "r", encoding="utf-8") as f:
            items = json.load(f)
        return {p["id"]: p for p in items if p.get("id") and p.get("file")}
    except Exception:
        return {}

@app.get("/api/voice/presets")
async def voice_presets():
    """预设音色列表（配音抽屉下拉框用）"""
    items = _load_voice_presets()
    out = []
    for pid, p in items.items():
        fp = os.path.join("static/voice_presets", p["file"])
        out.append({"id": pid, "label": p.get("label", pid), "exists": os.path.isfile(fp)})
    return {"presets": [p for p in out if p["exists"]]}

_VOICE_AUDIO_EXT = (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac")

def _voice_file_ok(name: str) -> bool:
    return name.lower().endswith(_VOICE_AUDIO_EXT)

async def _voice_server_call(path: str, payload: dict, timeout: float = 300.0) -> dict:
    """调用 VoxCPM 配音微服务；未启动/超时给出人话提示。"""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{VOICE_SERVER_URL}{path}", json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise HTTPException(503, "配音服务还没就绪：在线版首次部署需自动下载模型（约几分钟），请稍后重试；本地版需先启动 voice_server.py")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"配音服务连接异常：{type(e).__name__}")
    if r.status_code >= 400:
        try: detail = r.json().get("detail", f"配音服务错误（HTTP {r.status_code}）")
        except Exception: detail = f"配音服务错误（HTTP {r.status_code}）"
        raise HTTPException(r.status_code if r.status_code != 500 else 502, detail)
    return r.json()

async def _voice_net_probe() -> dict:
    """探测沙箱外网出口与内存上限（诊断权重下载/加载被杀用）"""
    import httpx as _hx
    out = {}
    targets = {"pypi": "https://pypi.org/simple/", "hf-mirror": "https://hf-mirror.com",
               "modelscope": "https://modelscope.cn", "hf": "https://huggingface.co"}
    try:
        async with _hx.AsyncClient(timeout=3) as c:
            for name, url in targets.items():
                try:
                    r = await c.head(url, follow_redirects=True)
                    out[name] = r.status_code
                except Exception as e:
                    out[name] = f"ERR:{type(e).__name__}"
    except Exception as e:
        out["probe"] = f"ERR:{type(e).__name__}"
    # 内存：cgroup v2 / v1 限额 + /proc/meminfo
    try:
        for p, key in (("/sys/fs/cgroup/memory.max", "cgroup_max"),
                       ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "cgroup_max_v1")):
            try:
                out[key] = open(p).read().strip()
            except Exception:
                pass
        for line in open("/proc/meminfo"):
            if line.startswith(("MemTotal", "MemAvailable")):
                k, v = line.split(":", 1)
                out["mem_" + k] = v.strip()
    except Exception:
        pass
    return out

def _weights_progress_mb() -> float:
    """models/VoxCPM-0.5B 目录已下载的字节数（MB，诊断下载进度用）"""
    total = 0.0
    try:
        d = os.path.join("static", "..", "models", "VoxCPM-0.5B")
        d = os.path.normpath(d)
        if os.path.isdir(d):
            for n in os.listdir(d):
                try: total += os.path.getsize(os.path.join(d, n))
                except OSError: pass
    except Exception:
        pass
    return round(total / 1024 / 1024, 1)

@app.get("/api/voice/status")
async def voice_status():
    """探测配音微服务是否在线（设置页展示用；探测失败静默降级为未就绪）"""
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await client.get(f"{VOICE_SERVER_URL}/health")
        tail = ""
        try:
            with open("voice_server.log", "r", encoding="utf-8", errors="replace") as f:
                tail = "\n".join(f.read().splitlines()[-10:])[-1500:]
        except Exception:
            pass
        return {**r.json(), "ready": bool(r.json().get("ready")), "weights_mb": _weights_progress_mb(), "log_tail": tail}
    except Exception:
        tail = ""
        try:
            with open("voice_server.log", "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            tail = "\n".join(lines[-15:])[-2000:]
        except Exception:
            pass
        net = await _voice_net_probe()
        return {"ready": False, "warming": False, "model": "VoxCPM-0.5B", "device": "cpu", "log_tail": tail, "net": net, "weights_mb": _weights_progress_mb()}

@app.post("/api/upload-voice")
async def upload_voice(req: UploadVoiceRequest):
    """上传角色音色参考音频（克隆素材），存为 voice_*.<ext>"""
    b64 = req.audio_base64.strip()
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[1] if "," in b64 else ""
    try:
        audio = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, "音频数据无效")
    if len(audio) < 1000:
        raise HTTPException(400, "音频内容为空或损坏")
    if len(audio) > 15 * 1024 * 1024:
        raise HTTPException(400, "参考音频超过 15MB，请裁剪到 5-20 秒再上传")
    ext = os.path.splitext(req.filename or ".wav")[1].lower()
    if ext not in _VOICE_AUDIO_EXT:
        ext = ".wav"
    filename = f"voice_{uuid.uuid4().hex}{ext}"
    with open(os.path.join("static/uploads", filename), "wb") as f:
        f.write(audio)
    return {"voice_url": f"/static/uploads/{filename}"}

# ---------- 配音异步任务（云端 CPU 合成 1-3 分钟，超过网关超时，必须后台执行） ----------
_dub_jobs: Dict[str, Dict[str, Any]] = {}
_dub_jobs_lock = __import__("threading").Lock()

def _dub_job_gc():
    now = _time.time()
    with _dub_jobs_lock:
        for jid in [j for j, v in _dub_jobs.items() if now - v.get("created", now) > 1800]:
            _dub_jobs.pop(jid, None)

def _dub_job_run(job_id: str, payload: dict):
    """后台线程：调配音微服务合成并存文件（同步 httpx，独立于请求生命周期）"""
    try:
        with httpx.Client(timeout=600.0) as client:
            r = client.post(f"{VOICE_SERVER_URL}/tts", json=payload)
        if r.status_code >= 400:
            try: detail = r.json().get("detail", f"配音服务错误（HTTP {r.status_code}）")
            except Exception: detail = f"配音服务错误（HTTP {r.status_code}）"
            raise RuntimeError(detail)
        result = r.json()
        wav = base64.b64decode(result["audio_b64"])
        filename = f"dub_{uuid.uuid4().hex}.wav"
        with open(os.path.join("static/uploads", filename), "wb") as f:
            f.write(wav)
        with _dub_jobs_lock:
            _dub_jobs[job_id].update(status="done", audio_url=f"/static/uploads/{filename}",
                                     sample_rate=result.get("sample_rate"))
    except Exception as e:
        with _dub_jobs_lock:
            _dub_jobs[job_id].update(status="error", detail=f"{type(e).__name__}: {e}" if not isinstance(e, RuntimeError) else str(e))

@app.post("/api/generate-dub")
async def generate_dub(req: GenerateDubRequest):
    """为分镜台词生成配音：提交后台任务立即返回 job_id（前端轮询 /api/dub-result 取结果）"""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "台词为空：先在分镜里填写台词 / 旁白")
    if len(text) > 500:
        raise HTTPException(400, "台词过长（最多 500 字），请拆分或精简")

    payload = {
        "text": text,
        "cfg_value": req.cfg_value,
        "inference_timesteps": req.inference_timesteps,
    }
    voice_path = local_static_path(req.voice_url) if req.voice_url else None
    if not voice_path and req.preset_id:
        # 预设音色：直接读 static/voice_presets/ 下的样本（不用上传）
        preset = _load_voice_presets().get(req.preset_id)
        if not preset:
            raise HTTPException(400, "预设音色不存在或不可用")
        voice_path = os.path.join("static/voice_presets", preset["file"])
        req.voice_text = preset.get("prompt_text") or req.voice_text
    if voice_path:
        with open(voice_path, "rb") as f:
            payload["prompt_wav_b64"] = base64.b64encode(f.read()).decode("ascii")
        payload["prompt_text"] = (req.voice_text or "").strip()
        if not payload["prompt_text"]:
            raise HTTPException(400, "该音色缺少参考文本：请在角色设定里填写「音色参考文本」（参考音频里说的那句话）")

    _dub_job_gc()
    job_id = uuid.uuid4().hex
    with _dub_jobs_lock:
        _dub_jobs[job_id] = {"status": "pending", "created": _time.time()}
    __import__("threading").Thread(target=_dub_job_run, args=(job_id, payload), daemon=True).start()
    return {"job_id": job_id, "status": "pending"}

@app.get("/api/dub-result")
async def dub_result(job_id: str = Query(...)):
    with _dub_jobs_lock:
        job = _dub_jobs.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在或已过期")
        return {k: v for k, v in job.items() if k != "created"}

# ---------- 3.6 聊天附件解析（txt/md/pdf/docx → 纯文本，供剧本对话引用） ----------
@app.post("/api/extract-file")
async def extract_file(file: UploadFile = File(...)):
    name = file.filename or "附件"
    ext = os.path.splitext(name)[1].lower()
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "文件超过 20MB，请拆分后上传")

    text = ""
    try:
        if ext in (".txt", ".md", ".csv", ".log", ".json"):
            for enc in ("utf-8", "gbk", "utf-16"):
                try:
                    text = data.decode(enc)
                    break
                except (UnicodeDecodeError, UnicodeError):
                    continue
            if not text:
                text = data.decode("utf-8", errors="replace")
        elif ext == ".pdf":
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            if not text.strip():
                raise HTTPException(400, "这个 PDF 提取不到文字（可能是扫描件/图片版），请换成文字版 PDF")
        elif ext == ".docx":
            import io
            import docx
            d = docx.Document(io.BytesIO(data))
            parts = [p.text for p in d.paragraphs if p.text.strip()]
            for t in d.tables:
                for row in t.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
            text = "\n".join(parts)
        elif ext == ".doc":
            raise HTTPException(400, "暂不支持老版 .doc，请用 Word 另存为 .docx 后上传")
        else:
            raise HTTPException(400, f"暂不支持 {ext or '该'} 格式，支持的附件：图片 / txt / md / csv / pdf / docx")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"解析失败：{type(e).__name__}: {e}")

    if not text.strip():
        raise HTTPException(400, "文件内容为空")
    return {"name": name, "text": text, "chars": len(text)}

# ---------- 4. AutoDL 提交视频任务 ----------
def local_static_path(url: str | None) -> str | None:
    """把本地图床 URL 解析成磁盘路径；文件不存在返回 None（供容错判断，
    项目搬运/换电脑时 static/uploads 可能没跟着拷，旧参考图 URL 会指向不存在的文件）"""
    if not url or "/static/" not in url:
        return None
    path_part = url.split("/static/")[-1].split("?")[0]
    p = os.path.join("static", path_part)
    return p if os.path.exists(p) else None

def local_url_to_data_uri(url: str) -> str:
    """把本地图床 URL 转成 base64 data URI。
    AutoDL 云端无法访问本机 127.0.0.1 地址；实测 minimax_h3_zm 系列工作流
    支持 data:image/...;base64 格式的 ref_image，无需第三方图床中转。"""
    path_part = url.split("/static/")[-1].split("?")[0]
    local_path = os.path.join("static", path_part)
    if not os.path.exists(local_path):
        raise HTTPException(404, f"参考图文件不存在：{local_path}")
    ext = os.path.splitext(local_path)[1].lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "jpeg")
    with open(local_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:image/{mime};base64,{b64}"

# ---------- 按参考图宽高比自动选横/竖屏档位 ----------
# H3 系工作流默认档是竖屏（如 768p竖=768×1344），横屏参考图不改档位就会输出竖屏视频。
# AutoDL 工作流详情接口（/api/v1/comfyui/workflows/{id}）的 input_rules 里有 resolution
# 参数的官方枚举（如 "480p竖/480p横/480p(1:1)/768p竖/768p横/768p(1:1)"），据此选档最可靠。
# 档位列表必须跨请求缓存（此前缓存放函数内每次清空，查询一失败就丢档位 → 横屏变默认竖屏）
_WORKFLOW_RES_CACHE: dict = {}

def measure_local_image_aspect(url: str | None) -> float | None:
    """后端兜底：从本地图床 URL 实测图片宽高比。
    前端是靠 new Image() 在浏览器里探测宽高比的，一旦探测失败（加载卡死/旧版前端没传），
    image_aspect 就是 null，后端会回落到工作流默认竖屏档 —— 横屏项目就出了竖屏视频。
    本项目的分镜图/参考图全部落在 static/uploads，后端直接用 Pillow 读文件实测，
    与前端是否传值无关，彻底消除这条断链。"""
    try:
        if not url or "/static/" not in url:
            return None
        path_part = url.split("/static/")[-1].split("?")[0]
        local_path = os.path.join("static", path_part)
        if not os.path.exists(local_path):
            return None
        from PIL import Image as PILImage
        with PILImage.open(local_path) as im:
            w, h = im.size
        return (w / h) if h and w else None
    except Exception:
        return None

def _frame_divergence(url1: str, url2: str) -> float | None:
    """估算两张帧图的低频构图差异（0-1）。32x18 灰度缩略图逐像素平均绝对差：
    实测同一图=0、同一镜头相邻帧≈0.001、全景→特写的跨构图≈0.18。
    读取失败返回 None（预检失败不阻断生成）。仅本地 /static/uploads/ 路径。"""
    def resolve(u: str):
        if not u:
            return None
        name = str(u).split("/")[-1].split("?")[0]
        if not re.fullmatch(r"[\w\-]+\.(jpg|jpeg|png|webp)", name):
            return None
        p = Path("static") / "uploads" / name
        if not p.is_file():
            return None
        return p

    import numpy as np
    try:
        from PIL import Image
        p1, p2 = resolve(url1), resolve(url2)
        if not p1 or not p2:
            return None
        a = np.asarray(Image.open(p1).convert("L").resize((32, 18)), dtype=int)
        b = np.asarray(Image.open(p2).convert("L").resize((32, 18)), dtype=int)
        return float(np.abs(a - b).mean() / 255.0)
    except Exception as e:
        print(f"⚠️ 首尾帧差异预检失败（忽略）: {type(e).__name__}: {e}")
        return None


# 局域网/本机 host：这些地址 Ark/AutoDL 云端都无法回源拉取
_LOCAL_HOST_RE = re.compile(r"^(localhost(:\d+)?|127\.\d+\.\d+\.\d+(:\d+)?|0\.0\.0\.0(:\d+)?|\[::1\](:\d+)?|::1(:\d+)?|192\.168\.\S+|10\.\S+|172\.(1[6-9]|2\d|3[01])\.\S+)$", re.I)

def public_media_url(request, url: str, share_code: str | None = None, origin: str | None = None) -> str | None:
    """把本机 /static/ 路径转成公网可拉取的绝对 URL（附 sharecode 过代理门禁）。
    Ark/AutoDL 云端无法访问本机与局域网地址；线上版经公网域名访问时才能转出。
    优先用前端传来的页面地址（origin，可靠），请求头 host 经代理转发后常是内网地址只作兜底。
    无法确定公网地址（本地部署/缺 sharecode）时返回 None。"""
    if not url or "/static/" not in url:
        return None
    scheme = host = ""
    if origin:
        try:
            p = urlparse(origin if "://" in origin else f"https://{origin}")
            scheme, host = (p.scheme or "https"), p.netloc
        except Exception:
            pass
    if not host:
        host = ((request.headers.get("x-forwarded-host") or request.headers.get("host") or "")
                .split(",")[0].strip())
        scheme = (request.headers.get("x-forwarded-proto") or "https").split(",")[0].strip()
    if not host or _LOCAL_HOST_RE.match(host):
        return None
    path = "/static/" + url.split("/static/")[-1].split("?")[0]
    out = f"{scheme}://{host}{path}"
    if share_code:
        out += f"?sharecode={share_code}"
    return out

def local_url_to_data_uri_video(url: str) -> str:
    """本地视频 URL → base64 data URI（AutoDL 垫参考视频兜底用）。
    非 /static/ 的远程 URL 原样返回。"""
    if "/static/" not in url:
        return url
    path_part = url.split("/static/")[-1].split("?")[0]
    local_path = os.path.join("static", path_part)
    if not os.path.exists(local_path):
        raise HTTPException(404, f"参考视频文件不存在：{local_path}")
    ext = os.path.splitext(local_path)[1].lower().lstrip(".")
    mime = {"mp4": "mp4", "webm": "webm", "mov": "quicktime", "m4v": "mp4"}.get(ext, "mp4")
    with open(local_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:video/{mime};base64,{b64}"

# ---------- 4b. 豆包 Seedance（火山方舟 Ark）视频生成 ----------
# 提交：POST {DOUBAO_BASE}/contents/generations/tasks  →  {"id": "cgt-..."}
# 查询：GET  {DOUBAO_BASE}/contents/generations/tasks/{id} → status: queued/running/succeeded/failed
# 返回的 task_id 统一加 "doubao:" 前缀，与 AutoDL 任务在状态查询/保存接口里区分开。
DOUBAO_TASK_PREFIX = "doubao:"

def _doubao_ratio_from_aspect(aspect: float | None) -> str:
    """参考图宽高比 → Seedance ratio 档位（官方支持 21:9/16:9/4:3/1:1/3:4/9:16）；拿不到宽高比默认 16:9。"""
    if not aspect:
        return "16:9"
    if aspect >= 21 / 9 * 0.92:
        return "21:9"
    if aspect > 1.20:
        return "16:9"
    if aspect > 1.05:
        return "4:3"
    if aspect < 0.42:
        return "9:16"
    if aspect < 0.75:
        return "3:4"
    return "1:1"

async def submit_doubao_video(req: VideoRequest, prompt: str, frame_mode: str) -> dict:
    """提交豆包 Seedance 视频生成任务（火山方舟 contents/generations/tasks）。
    参考图按帧模式映射 role：reference → reference_image；first/last/first_last → first_frame/last_frame。
    垫参考视频（req.ref_video_urls）以 reference_video 传入，只作运镜/构图/节奏引导。"""
    _require_own_key("ark")
    api_key = _eff_key("ark").strip()
    if not api_key:
        raise HTTPException(400, "未配置即梦(豆包) ARK API Key：请到 ⚙️ API 设置填入，或在程序目录 .env 写入 ARK_API_KEY=你的key")

    model = (req.video_model or DOUBAO_VIDEO_MODEL or "").strip() or DOUBAO_VIDEO_MODEL

    content: list[dict] = [{"type": "text", "text": prompt}]

    def img_entry(url: str, role: str) -> dict:
        u = str(url).strip()
        # 公网直链优先（保住"信任模型产物"的原始字节）；本地部署转不出 URL 才回落 base64
        val = u if u.startswith(("http://", "https://")) else local_url_to_data_uri(url)
        return {"type": "image_url", "image_url": {"url": val}, "role": role}

    if frame_mode == "reference":
        for u in (req.image_urls or [])[:30]:
            content.append(img_entry(u, "reference_image"))
    elif frame_mode == "first":
        if not req.first_frame_url:
            raise HTTPException(400, "首帧模式需要先标记首帧分镜图")
        content.append(img_entry(req.first_frame_url, "first_frame"))
    elif frame_mode == "last":
        if not req.last_frame_url:
            raise HTTPException(400, "尾帧模式需要先标记尾帧分镜图")
        content.append(img_entry(req.last_frame_url, "last_frame"))
    else:  # first_last
        if not (req.first_frame_url and req.last_frame_url):
            raise HTTPException(400, "首尾帧模式需要同时标记首帧与尾帧分镜图")
        content.append(img_entry(req.first_frame_url, "first_frame"))
        content.append(img_entry(req.last_frame_url, "last_frame"))

    # 垫参考视频：引导运镜/节奏/构图（Seedance 要求公网 web URL，不接受 base64，
    # 也不接受本机/局域网地址）。正常流程上方已全部公网化。
    # 注意：不能拿 "/static/" in u 判断本机路径——转换后的公网直链路径里同样含 /static/，
    # 之前正是这个条件把自家转换结果拦下（v88~v89 报错的真实原因）。
    # 正确判断：只放行 http(s) 绝对链接，其余（相对路径/data: URI）一律报错并带原始值。
    for u in (req.ref_video_urls or [])[:10]:
        u = str(u).strip()
        if not u:
            continue
        if not (u.startswith("http://") or u.startswith("https://")):
            raise HTTPException(400,
                f"[垫视频诊断v90] 收到异常值（前80字符）：{u[:80]}。"
                "垫参考视频必须是公网可访问的 URL（Ark 只接受 web 直链，不接受 base64 或本机地址）。"
                "请把这条报错完整发给开发者。")
        content.append({
            "type": "video_url",
            "video_url": {"url": u},
            "role": "reference_video",
        })

    payload = {
        "model": model,
        "content": content,
        "generate_audio": True,
        "ratio": _doubao_ratio_from_aspect(req.image_aspect),
        # 官方限制 4-30s（Seedance 2.5 单段直出最长 30s；3-15s 是 2.0 的旧限制）
        "duration": max(4, min(int(req.duration or 5), 30)),
        # 分辨率：官方可选 480p/720p/1080p（默认 480p 最省钱，720p 为官方默认档）
        "resolution": (req.doubao_resolution or "").lower() if (req.doubao_resolution or "").lower() in ("480p", "720p", "1080p") else "480p",
        "watermark": False,
    }

    print(f"📤 提交给豆包 Seedance: model={model}, ratio={payload['ratio']}, duration={payload['duration']}s, "
          f"图={sum(1 for c in content if c['type'] == 'image_url')}张, 垫视频={sum(1 for c in content if c['type'] == 'video_url')}条")
    log_prompt("生视频(Seedance)", prompt, f"model={model} 模式={frame_mode} duration={payload['duration']} ratio={payload['ratio']}")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def _post(pl: dict):
        last_exc = None
        async with httpx.AsyncClient(timeout=60) as client:
            for attempt in (1, 2, 3):
                try:
                    return await client.post(f"{DOUBAO_BASE}/contents/generations/tasks", headers=headers, json=pl)
                except httpx.HTTPError as e:
                    last_exc = e
                    print(f"⚠️ 连接火山方舟异常(第{attempt}次): {type(e).__name__}，重试…")
                    await asyncio.sleep(1.5)
        raise HTTPException(503, f"连接火山方舟失败（网络瞬断，已重试 3 次）：{type(last_exc).__name__}，请稍后再试")

    resp = await _post(payload)

    # 自动降级重试（实测诊断 2026-11-14）：真人审核拦的几乎总是"角色/场景设定图"——
    # 它们虽然是豆包产物，但是用用户上传的真人脸参考照片做图生图产出的衍生物，
    # 不在官方"Seedream 5.0 文生图产物"信任白名单内（官方文档 82379/2608626）；
    # 而分镜图单独提交实测可过审。被拦时自动只保留第一张参考图（分镜图）重提一次。
    def _is_sensitive(r) -> bool:
        return r.status_code == 400 and ("SensitiveContentDetected" in r.text or "real person" in r.text)

    if _is_sensitive(resp):
        _img_entries = [c for c in content if c.get("type") == "image_url"]
        _has_extra = len(_img_entries) > 1 or any(c.get("type") == "video_url" for c in content)
        _first = _img_entries[0] if _img_entries else None
        if _has_extra and _first and _first.get("role") == "reference_image":
            print("🚧 Seedance 真人审核拦截 → 自动降级：只保留分镜图重试（设定图/垫视频属图生图衍生或外部素材，不在官方信任白名单）")
            log_prompt("Seedance审核降级重试", prompt,
                       f"原图{len(_img_entries)}张+垫视频{sum(1 for c in content if c.get('type')=='video_url')}条 → 降级为仅分镜图")
            resp = await _post({**payload, "content": [{"type": "text", "text": prompt}, _first]})

    if resp.status_code == 401:
        raise HTTPException(401, "ARK API Key 无效或已过期，请到 ⚙️ API 设置检查即梦(豆包) Key")
    if resp.status_code != 200:
        # 垫视频相关报错时把实际提交的 URL 带上，方便确诊 Ark 为什么拒收
        _rv = [c["video_url"]["url"] for c in content if c.get("type") == "video_url"]
        if _rv and "reference_video" in resp.text:
            raise HTTPException(resp.status_code,
                f"Seedance 提交失败：{resp.text[:400]}\n--- 实际提交的垫视频 URL ---\n" + "\n".join(_rv[:4]))
        # 方舟输入审核：参考图疑似真人。官方政策：Seedance 2.5/2.0 不支持直接上传含真人脸的参考图，
        # 但同一方舟账号 30 天内的 Seedream 5.0 文生图产物被信任（官方文档 82379/2608626）。
        # 降级后仍被拦说明分镜图本身不可信：上传的图/编辑过的图/超30天/别的账号生成。
        if "SensitiveContentDetected" in resp.text or "real person" in resp.text:
            raise HTTPException(400,
                "🚫 Seedance 审核判定参考图可能包含真人（已自动尝试只保留分镜图重试，仍被拦）。"
                "说明本镜分镜图不在官方信任白名单内——只有「同一个方舟账号、30 天内、豆包生图(Seedream 5.0)的原始产物」"
                "才免审核；上传的图、裁剪/压缩/编辑过的图、超过 30 天的图都会失效。"
                "解决：用豆包生图重新生成本镜分镜图后立刻生成视频，或写实项目改用 AutoDL 工作流引擎（无此限制）。")
        raise HTTPException(resp.status_code, f"Seedance 提交失败：{resp.text[:500]}")
    data = resp.json() or {}
    task_id = data.get("id")
    if not task_id:
        raise HTTPException(500, f"Seedance 未返回任务 ID：{str(data)[:300]}")
    return {"task_id": f"{DOUBAO_TASK_PREFIX}{task_id}", "raw": data, "engine": "doubao"}


async def get_doubao_task(task_id: str, api_key: str | None = None) -> dict:
    """查询豆包 Seedance 任务状态。返回 {"status": QUEUED/RUNNING/SUCCESS/FAILURE, "video_url": str|None, "raw": dict}"""
    key = (api_key or _eff_key("ark")).strip()
    if not key:
        raise HTTPException(400, "未配置即梦(豆包) ARK API Key，无法查询 Seedance 任务")
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"{DOUBAO_BASE}/contents/generations/tasks/{task_id}", headers=headers)
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"Seedance 任务查询失败：{resp.text[:300]}")
    data = resp.json() or {}
    status = str(data.get("status") or "").lower()
    video_url = None
    if status == "succeeded":
        content = data.get("content") or {}
        video_url = content.get("video_url")
        if isinstance(video_url, dict):
            video_url = video_url.get("url")
    mapped = {"queued": "QUEUED", "running": "RUNNING", "succeeded": "SUCCESS", "failed": "FAILURE"}.get(status, "UNKNOWN")
    # 异步失败原因翻译：方舟的任务失败信息是英文原文，前端只显示"生成失败"看不到原因
    friendly_error = None
    if status == "failed":
        raw_err = str((data.get("error") or {}).get("message") or data.get("error") or "")
        if "copyright" in raw_err.lower():
            friendly_error = ("输出内容被平台版权审核误伤（与参考图无关，概率性误判）：点重新生成重试一次通常即可通过。"
                              "若同一镜反复出现，可微调视频提示词避开与特定影视剧相似的表述。")
        elif "SensitiveContentDetected" in raw_err or "real person" in raw_err.lower():
            friendly_error = ("成片内容审核未通过（平台对输出画面的概率性审核）：点重新生成重试一次通常即可通过。")
        elif raw_err:
            friendly_error = f"Seedance 任务失败：{raw_err[:200]}"
    return {"status": mapped, "video_url": video_url, "raw": data, "error": friendly_error}


@app.post("/api/generate-video")
@app.post("/api/submit-video")  # 别名路由：绕开个别环境对该路径的拦截（浏览器实测 /api/generate-video 请求消失时备用）
async def generate_video(req: VideoRequest, request: Request):
    print(f"📥 收到视频提交请求: mode={req.frame_mode}, 参考图={len(req.image_urls or [])}张, duration={req.duration}s, 垫视频={len(req.ref_video_urls or [])}条")
    # 帧模式归一化（先算好，才能决定用哪支令牌）
    frame_mode = (req.frame_mode or "reference").lower()
    if frame_mode in ("first_frame", "first"):
        frame_mode = "first"
    elif frame_mode in ("last_frame", "last"):
        frame_mode = "last"
    elif frame_mode in ("first_last", "both", "start_end"):
        frame_mode = "first_last"
    else:
        frame_mode = "reference"

    # 容错：参考图模式过滤掉磁盘上已丢失的本地图（项目搬运/换电脑没拷 uploads）。
    # 此前会报"参考图文件不存在"直接 404 炸掉提交；现在跳过并随响应提示，
    # 用户重新上传参考图即可恢复一致性。首/尾帧是工作流必填项，缺失仍报错。
    missing_video_refs: list[str] = []
    if frame_mode == "reference":
        _kept_urls, _rest = [], list(req.image_urls or [])
        for u in _rest[:9]:
            if "/static/" in u and local_static_path(u) is None:
                missing_video_refs.append(u.split("/")[-1])
                continue
            _kept_urls.append(u)
        if missing_video_refs:
            print(f"⚠️ 跳过 {len(missing_video_refs)} 张丢失的视频参考图（继续提交）: {missing_video_refs}")
            req.image_urls = _kept_urls + _rest[9:]

    # 运动轨迹指令：调度图纸（俯视/轨迹）坐标翻译成自然语言，与参考图一起约束视频的人物走位与镜头运动
    # 入口净化：视频模型更不认识 AU 编码，混在正文里会直接让脸崩坏
    prompt = _clean_au_codes(req.prompt or "")
    if req.env_context:
        req.env_context = _clean_au_codes(req.env_context)
    # 时长校正：动作时序文案是建分镜时按当时的时长生成的，导演后来改了时长也不会重写——
    # 发视频时按实际时长重申时间轴，避免"总时长3秒"配7秒成片导致动作节奏错乱。
    # 写法教训：陈述句会被 i2v 模型当字幕烧进画面，写成简短括注且不带【】标签。
    if req.duration and req.duration > 0:
        prompt += f"\n（全片时长 {req.duration} 秒，动作节奏按 {req.duration} 秒匀速铺排。）"
    if req.plot_data:
        # dynamic=True：名单只约束初始帧，后续人物进出场按走位与镜头运动推演（视频有时间轴，与单帧分镜图不同）
        # 顶层镜头翻译：DeepSeek 按坐标+程序几何事实产出"镜头画面 JSON"（含运动意向），失败自动回退程序直译
        plot_txt = await build_plot_prompt_text(req.plot_data, prompt, dynamic=True)
        if plot_txt:
            prompt = (f"{prompt}\n\n【运动轨迹指令】（由导演定稿的调度图纸坐标翻译而来，"
                      f"人物走位、运动快慢、身体朝向与镜头运动必须严格遵守）\n{plot_txt}\n"
                      "【身份唯一规则】画面中每个角色全程只有一个身体、一份存在：上述走位是画面中已有角色的连续位移，"
                      "不是新角色入场；严禁出现复制人、分身、双胞胎——比如出现两个憨憨相向而行、一个走近镜头另一个还站在原地，都是错误；"
                      "角色从画面外走入时，走进来的就是他本人，画面里原本的他平滑移动过来即可。"
                      "走位只能通过人物身体在画面中的连续位移来表现，"
                      "严禁把走位路径画成地面上的红色虚线、箭头、标线、跑道线、光轨等任何地面标记——画面是真实世界，不是图纸。"
                      "白底俯视调度图纸类参考图仅用于理解走位方向与机位关系，"
                      "严禁把图纸本身、白底平面图、人偶棋子图标、扇形视锥、虚线箭头画进视频画面。")
            # 视野外陈设黑名单：同生图链路，防场景设定图里的门窗被视频抄进画面
            _hn = _hidden_props_note(req.plot_data or {})
            if _hn:
                prompt += "\n" + _hn

    # 首尾帧过渡自然性（实测：不给过渡指令时，两帧构图差异稍大模型就硬切/让人物凭空出现）
    if frame_mode in ("first", "last", "first_last"):
        if frame_mode == "first_last":
            prompt += ("\n\n【首尾帧过渡规则·最高优先级】首帧是视频第 1 帧，尾帧是最后一帧，中间的全部画面必须从首帧【连续演变】到尾帧：\n"
                       "①首帧里已有的人物全程连续存在——只能通过走位、转身、被镜头摇出画面等方式离开视野，严禁消失后再凭空出现；\n"
                       "②尾帧里有、而首帧里没有的人物或物体，必须用符合物理逻辑的方式带进画面：走入画面边缘、镜头摇/移过去带出、"
                       "人物从前景走过让出视野等，严禁凭空生成、严禁突然出现在画面中央；提示词正文明确写到的人物（宫人、路人甲等群演）按正文入画，"
                       "名单外且正文没写的无名人影任何时候都不得出现；\n"
                       "③首尾帧构图差异（机位朝向、人物站位变化）用连续的镜头运动（摇镜/推拉/跟随/环绕）与连续的人物走位完成，"
                       "中间允许有多个过渡状态，严禁前半段静止在首帧、后半段硬切到尾帧；\n"
                       "④过渡路径优先服从【运动轨迹指令】的走位与镜头运动方向；无轨迹指令时按两帧差异设计最自然的连续运镜；\n"
                       "⑤场景背景（天空、建筑、地面、光照）全程连续存在且与首帧一致：镜头移动时背景随透视自然变化，"
                       "严禁背景中途消失、变白、变成空白平面、或中途重绘成另一个场景。\n"
                       "全片是【单一连续镜头】（one continuous shot）：中间没有任何剪辑点、没有场景切换、没有画面跳变。")
        elif frame_mode == "first":
            prompt += ("\n\n【首帧连续规则】首帧是视频第 1 帧：画面从首帧出发自然连续地动起来，"
                       "首帧里的人物连续存在、连续位移，名单内的新角色必须从画面边缘走入或被镜头运动带进画面，"
                       "提示词正文明确写到的人物（宫人、路人甲等群演）按正文入画，"
                       "名单外且正文没写的无关人物任何时候都不得出现，严禁凭空生成、硬切、瞬移。")
        else:  # last
            prompt += ("\n\n【尾帧收束规则】尾帧是视频最后一帧：画面从初始状态自然连续地演变，最终精确收束到尾帧画面，"
                       "全程人物连续存在、连续位移，新出现的人物/物体必须从画面边缘走入或被镜头运动带进画面，严禁凭空生成、硬切、瞬移。")

    # 影子镜头保护（视频）：首帧是影子图时，视频模型极易让影子"站起来"变成人形/幽灵。
    # 用户实测教训：影子镜头的视频里冒出半透明人形。放 prompt 末尾服从度最高。
    if any(k in (req.prompt or "") for k in ("影子", "倒影", "剪影", "人影")):
        prompt += ("\n\n【影子镜头规则·最高优先级】本镜头的主体是投在地面/墙面上的影子/倒影/剪影：\n"
                   "①影子全程保持为紧贴地面/墙面的二维扁平暗色投影，随光源与动作变形，没有身体厚度、不发光、不悬空；\n"
                   "②严禁影子立起、站起、从地面剥离、变成立体人形、半透明人形、幽灵、鬼魂、灵体或第二个人；\n"
                   "③严禁任何人形/身体部位从影子中长出、浮现或站立——除非提示词明确要求人物本体出镜，画面中不得出现人物本体；\n"
                   "④全程画面里除了贴地的影子（以及提示词明确要求的人物本体），不能出现任何其他人形物体。")

    # 场景环境锁定放最后（模型对提示词末尾的服从度最高）：视频提示词通常只写动作与走位，
    # 不含环境描述，参考图模式对「主体一致」权重高、对环境无约束，模型会自行脑补场景
    # （实测：公园黄昏镜头被画成室内走廊）。第1张参考图就是分镜图，以其环境为权威基准。
    if (req.env_context or "").strip():
        prompt += ("\n\n【场景环境锁定·最高优先级】本视频的场景环境、美术风格与光线必须与第1张参考图（本镜头的分镜图）完全一致，"
                   "全程不得切换场景、不得重绘背景：\n" + req.env_context.strip())

    # 参考模式一致性锚点：参考图是画面基准而不是"必须是第 1 帧"（用户明确：镜头可以从
    # 别的画面开始、中途经过与参考图一致的状态，这正是参考模式区别于首帧模式的意义）。
    # 所以只锁"全程一致性"（场景/风格/光线/人物造型），不锁构图时刻与首帧等同。
    # 服装以第 1 张（本镜分镜图）为最高基准（用户实测事故：参考池混入角色设定图后，
    # 模型把设定图的军装和分镜图的便装混着穿——Seedance 对多图服装会自行融合，
    # 必须逐条点名裁决权，笼统写"与参考图一致"挡不住）。
    if frame_mode == "reference":
        prompt += ("\n\n【参考图一致性·服装裁决】人物的外貌、发型与服装以第1张参考图（本镜头分镜图）为最高基准："
                   "服装的款式、颜色、层数、材质必须与分镜图逐项一致，全程不得更换、增减或叠加任何衣物"
                   "（如披风、盔甲、外套等分镜图上没有的衣物一律禁止出现）；"
                   "第1张之后的设定图只用于核对人物面容与发型，其服装与分镜图不一致时一律以分镜图为准；"
                   "提示词正文明确要求的换装除外。"
                   "场景环境、美术风格、光线全程与参考图保持一致；"
                   "构图与时刻可以与参考图不同，运动中经过与参考图一致的画面也允许；"
                   "严禁换成完全不同的场景、改变人物造型或重绘背景。")

    # 画面纯净（视频专用）：提示词文本被模型当字幕烧进画面的实测事故已多次，
    # 除本条外，下方还会把全部【】标签整形为括注，从形态上消除"剧本感"。
    # 视频参考图分工说明（仅参考模式）：前端把视频提示词中括号引用到的角色（含指定服装）/
    # 场景/道具设定图也放进参考池，这里按序号声明各图用途。写成短括注（无【】标签），
    # 防模型把它当字幕的同时说明清楚"只锁外观，动作人数以提示词为准"。
    if req.ref_meta and frame_mode == "reference":
        notes = []
        for k, m in enumerate(req.ref_meta[:len(req.image_urls)]):
            kind = str((m or {}).get("kind") or "")
            nm = str((m or {}).get("name") or "")
            if kind in ("character", "scene", "prop") and nm:
                notes.append(f"第{k + 1}张={nm}设定图")
        if notes:
            prompt += ("\n（参考图分工：第1张=本镜分镜图，是服装与场景的最高基准；"
                       + "、".join(notes) + "，这些图只核对面容/发型，其服装与分镜图不一致时以分镜图为准；"
                       "人物动作、画面人数以提示词为准。）")

    prompt += "\n\n【画面纯净】成片是纯影视画面：任何文字、数字、水印、界面元素都不得出现在画面中。"

    # ---------- 视频提示词整形（防字幕烧录） ----------
    # 实测事故：H3 系 i2v 模型会把提示词里的【标签】结构当剧本/台词，挑字烧成画面字幕
    # （"本视频实际总时长为 5 秒""打开民女打扮""…画面基准"都被渲染过）。
    # 发送前把全部【】标签转为圆括号、去掉"最高优先级/内部指令"标签语，压掉"剧本感"；
    # 只影响发给模型的文本，不影响参考图选择（那在提交前已完成）。
    def _defuse_subtitles(s: str) -> str:
        s = re.sub(r"【([^】]*)】", r"（\1）", s)
        s = s.replace("·最高优先级", "").replace("·内部指令", "").replace("最高优先级", "").replace("内部指令", "")
        s = re.sub(r"\n{2,}", "\n", s)
        return s.strip()

    prompt = _defuse_subtitles(prompt)

    # 垫参考视频公网化：云端（Ark/AutoDL）无法访问本机/局域网地址，也不接受 base64。
    # 本地 /static/ 文件一律转成"公网域名+sharecode"直链（优先前端 page_origin，
    # 兜底后端已知公网域名 PUBLIC_BASE_URL）；data: URI 落盘成本站文件再转直链。
    if req.ref_video_urls:
        _pub = []
        for _u in req.ref_video_urls:
            _su = str(_u).strip()
            # data URI（历史遗留/异常入口）：解码落盘到 uploads，转成本站直链
            if _su.startswith("data:video"):
                try:
                    _b64 = _su.split(",", 1)[1] if "," in _su else ""
                    _vb = base64.b64decode(_b64)
                    if len(_vb) > 1000:
                        _vn = f"refvideo_{uuid.uuid4().hex}.mp4"
                        with open(os.path.join("static", "uploads", _vn), "wb") as _vf:
                            _vf.write(_vb)
                        _su = f"/static/uploads/{_vn}"
                        print(f"💾 垫视频 data URI 落盘: {_vn} ({len(_vb)//1024}KB)")
                except Exception as _e:
                    print(f"⚠️ 垫视频 data URI 落盘失败: {type(_e).__name__}")
            _pu = public_media_url(request, _su, req.share_code, req.page_origin)
            if not _pu and "/static/" in _su:
                # 兜底：前端没传 origin（HTML 缓存）或请求头是内网地址时，用后端已知的公网域名拼直链
                _pu = f"{PUBLIC_BASE_URL}/static/{_su.split('/static/')[-1].split('?')[0]}"
                if req.share_code:
                    _pu += f"?sharecode={req.share_code}"
            if _pu and _pu != _su:
                print(f"🌐 垫视频公网直链: {_su.split('/')[-1][:40]} → {_pu[:120]}")
            _pub.append(_pu or _su)
        req.ref_video_urls = _pub

    # 参考图公网化：官方"信任模型产物"审核以原始产物为准（同账号 30 天内 Seedream 5.0 产物免真人审核），
    # 文档明确"压缩或转发文件易引发信任失效"——所以方舟引擎优先用公网 URL 直传原始字节，
    # 让方舟拉到的就是生成时的原始文件；转不出公网地址（本地部署）再回落 base64。
    def _pub_img(u: str) -> str:
        _su = str(u).strip()
        if _su.startswith(("http://", "https://")):
            return _su
        _pu = public_media_url(request, _su, req.share_code, req.page_origin)
        if not _pu and "/static/" in _su:
            _pu = f"{PUBLIC_BASE_URL}/static/{_su.split('/static/')[-1].split('?')[0]}"
            if req.share_code:
                _pu += f"?sharecode={req.share_code}"
        return _pu or _su

    if req.image_urls:
        req.image_urls = [_pub_img(u) for u in req.image_urls]
    if req.first_frame_url:
        req.first_frame_url = _pub_img(req.first_frame_url)
    if req.last_frame_url:
        req.last_frame_url = _pub_img(req.last_frame_url)

    # 引擎分流：doubao=豆包 Seedance（火山方舟）走独立提交通道，不走 AutoDL ComfyUI 工作流
    if (req.video_engine or "autodl").lower() in ("doubao", "seedance", "ark"):
        print(f"🫘 视频引擎=豆包 Seedance, 模式={frame_mode}, 垫参考视频={len(req.ref_video_urls or [])}条")
        return await submit_doubao_video(req, prompt, frame_mode)

    target_workflow = req.workflow_id if req.workflow_id else AUTODL_WORKFLOW

    # ---------- 按参考图宽高比自动选横/竖屏档位 ----------
    # H3 系工作流默认档是竖屏（如 768p竖=768×1344），横屏参考图不改档位就会输出竖屏视频。
    # AutoDL 工作流详情接口（/api/v1/comfyui/workflows/{id}）的 input_rules 里有 resolution
    # 参数的官方枚举（如 "480p竖/480p横/480p(1:1)/768p竖/768p横/768p(1:1)"），据此选档最可靠。
    async def get_resolution_options(client, headers):
        if target_workflow in _WORKFLOW_RES_CACHE:
            return _WORKFLOW_RES_CACHE[target_workflow]
        options = []
        for attempt in (1, 2):
            try:
                r = await client.get(f"{AUTODL_BASE}/api/v1/comfyui/workflows/{target_workflow}", headers=headers)
                data = (r.json() or {}).get("data") or {}
                rules = data.get("input_rules") or {}
                options = [o.get("label") for o in ((rules.get("resolution") or {}).get("options") or []) if o.get("label")]
                if options:
                    break
                print(f"⚠️ 工作流 {target_workflow} 未返回 resolution 枚举（第{attempt}次）")
            except Exception as e:
                print(f"⚠️ 查询工作流 {target_workflow} 参数失败（第{attempt}次）: {type(e).__name__}: {e}")
            await asyncio.sleep(1.0)
        if options:
            _WORKFLOW_RES_CACHE[target_workflow] = options
        print(f"📋 工作流 {target_workflow} 支持 resolution 档位: {options or '（无此参数，用默认档）'}")
        return options

    def pick_resolution(options: list, aspect) -> str | None:
        """横图选"横"档，竖图选"竖"档，方图选"(1:1)"档；同方向优先 768p，其次 480p。"""
        if not options:
            return None
        want = None
        if aspect:
            if aspect > 1.05: want = "横"
            elif aspect < 0.95: want = "竖"
            else: want = "(1:1)"
        if want:
            for tier in ("768p", "480p"):
                for o in options:
                    if tier in o and want in o:
                        return o
            for o in options:  # 方向命中但档位不全时，取该方向任意档
                if want in o:
                    return o
        # 兜底（拿不到宽高比，如远程 URL 无法实测）：显式选"768p竖"，与工作流默认档一致。
        # 旧写法是"取第一个含 768p 的档"，但 zm 系枚举顺序里第一个是 768p横——碰运气，
        # 换支工作流（lightx2v 枚举里 768p竖 在前）就变竖屏，行为不可预测
        for o in options:
            if "768p竖" in o:
                return o
        for o in options:
            if "768p" in o:
                return o
        return options[0]

    # 首尾帧应用(U02)可能使用独立令牌：配置了 AUTODL_I2V_TOKEN 且非参考模式时用它
    _require_own_key("autodl")
    use_i2v_token = frame_mode != "reference" and _eff_key("autodl_i2v").strip()
    headers = {
        "Authorization": (_eff_key("autodl_i2v").strip() if use_i2v_token else _eff_key("autodl")),
        "Content-Type": "application/json",
    }
    if use_i2v_token:
        print("🔑 使用首尾帧应用独立令牌 (AUTODL_I2V_TOKEN)")

    async with httpx.AsyncClient(timeout=60) as client:
        # minimax_h3_lightx2v 官方参数：prompt / duration / first_frame / last_frame（均必填，URL/base64）
        # 单帧模式若被平台拒绝则用同一张图补齐另一帧兜底重试（起点=终点）

        def to_val(url: str) -> str:
            return local_url_to_data_uri(url) if "/static/" in url else url

        def build_payload(put_first: bool, put_last: bool, resolution: str | None = None) -> dict:
            p = {"prompt": prompt, "duration": req.duration}
            if frame_mode == "reference":
                for i, url in enumerate(req.image_urls[:9]):
                    p[f"ref_image_{i}"] = to_val(url)
            else:
                if put_first and req.first_frame_url:
                    p["first_frame"] = to_val(req.first_frame_url)
                if put_last and req.last_frame_url:
                    p["last_frame"] = to_val(req.last_frame_url)
            # resolution 档位（含横竖屏）：仅在查询到工作流支持该参数时才发送；
            # zm 系列老工作流不接受 resolution（传了报 RequestParameterIsWrong），保持默认档
            if resolution:
                p["resolution"] = resolution
            # 垫参考视频（引导运镜/节奏，不入成片）：ref_video_0..N 传给工作流；
            # 不支持该参数的工作流（平台报未定义参数）会在提交后去掉重试，不影响生成
            for i, u in enumerate((req.ref_video_urls or [])[:10]):
                u = str(u).strip()
                if not u:
                    continue
                p[f"ref_video_{i}"] = u if u.startswith(("http://", "https://")) else local_url_to_data_uri_video(u)
            return p

        target_workflow = req.workflow_id if req.workflow_id else AUTODL_WORKFLOW
        res_options = await get_resolution_options(client, headers)
        # 宽高比三级来源：前端浏览器探测 → 前端比例选择器兜底 → 后端 Pillow 本地实测。
        # 任一环成功即可正确选横/竖档，不再出现"探测失败就回落竖屏默认档"
        image_aspect = req.image_aspect
        if not image_aspect:
            for u in list(req.image_urls[:3]) + [req.first_frame_url, req.last_frame_url]:
                a = measure_local_image_aspect(u)
                if a:
                    image_aspect = a
                    print(f"📏 前端未传有效宽高比，后端本地实测参考图: {u} → {a:.3f}")
                    break
        chosen_res = pick_resolution(res_options, image_aspect)
        if chosen_res:
            print(f"🖼️ 参考图宽高比={image_aspect} → 自动选择 resolution 档位: {chosen_res}")

        async def submit(pl: dict):
            # 网络瞬断容错：自动重试 2 次，仍失败才报错（避免偶发 httpx.ReadError 直接 500）
            last_exc = None
            for attempt in (1, 2, 3):
                try:
                    return await client.post(
                        f"{AUTODL_BASE}/api/v1/comfyui/comfyui_workflow/{target_workflow}",
                        headers=headers,
                        json=pl,
                    )
                except httpx.HTTPError as e:
                    last_exc = e
                    print(f"⚠️ 连接 AutoDL 异常(第{attempt}次): {type(e).__name__}，重试…")
                    await asyncio.sleep(1.5)
            raise HTTPException(503, f"连接 AutoDL 失败（网络瞬断，已重试 3 次）：{type(last_exc).__name__}，请稍后再试")

        if frame_mode == "reference":
            payload = build_payload(False, False, chosen_res)
        elif frame_mode == "first":
            payload = build_payload(True, False, chosen_res)   # 只传首帧
        elif frame_mode == "last":
            payload = build_payload(False, True, chosen_res)   # 只传尾帧
        else:
            payload = build_payload(True, True, chosen_res)    # 首尾帧过渡
        print(f"📤 提交给 AutoDL: workflow={target_workflow}, 模式={frame_mode}, 图数量={len(req.image_urls[:9])}, resolution={chosen_res or '默认'}")
        print(f"📝 视频提示词: {prompt[:200]}{'…' if len(prompt) > 200 else ''}")
        log_prompt("生视频", prompt, f"workflow={target_workflow} 模式={frame_mode} duration={req.duration} resolution={chosen_res or '默认'} plot_data={'有' if req.plot_data else '无'}")

        resp = await submit(payload)
        data = resp.json() if resp.status_code == 200 else {}
        # 该工作流若不接受 resolution 参数（RequestParameterIsWrong 且提到 resolution），去掉后原样重试
        if chosen_res and data.get("code") == "RequestParameterIsWrong" and "resolution" in (data.get("msg") or ""):
            print("🔄 工作流不接受 resolution 参数，去掉该参数重试（使用工作流默认档位）")
            chosen_res = None
            payload = build_payload(frame_mode in ("first", "first_last"), frame_mode in ("last", "first_last"))
            resp = await submit(payload)
            data = resp.json() if resp.status_code == 200 else {}
        # 垫参考视频兜底：部分工作流不接受 ref_video_* 参数（报未定义参数），去掉后重试
        if any(k.startswith("ref_video_") for k in payload) and data.get("code") == "RequestParameterIsWrong" \
                and "ref_video" in (data.get("msg") or ""):
            print("🔄 工作流不接受垫参考视频（ref_video_*）参数，去掉后重试（垫视频仅作引导，不影响正常生成）")
            payload = {k: v for k, v in payload.items() if not k.startswith("ref_video_")}
            resp = await submit(payload)
            data = resp.json() if resp.status_code == 200 else {}
        # 首尾帧工作流文档要求 first_frame/last_frame 都必填：
        # 单帧模式若平台报"缺少必填参数"，用同一张分镜图补上另一帧重试（起点=终点，画面从图出发再回到图）
        if frame_mode in ("first", "last", "first_last") and data.get("code") == "RequestParameterIsWrong" \
                and "缺少必填参数" in (data.get("msg") or "") and not (req.first_frame_url and req.last_frame_url):
            missing = data.get("msg") or ""
            print(f"🔄 工作流要求首尾帧都必填（{missing}），用同一张分镜图补齐另一帧重试")
            # 用同一张图补齐空缺的另一个帧槽（起点=终点）
            if not req.last_frame_url:
                req.last_frame_url = req.first_frame_url
            if not req.first_frame_url:
                req.first_frame_url = req.last_frame_url
            payload = build_payload(True, True, chosen_res)
            resp = await submit(payload)
            data = resp.json() if resp.status_code == 200 else {}
        # 兜底：若参数名不被接受（报未定义参数），改用 ref_image_0 再试一次
        elif frame_mode in ("first", "last", "first_last") and data.get("code") == "RequestParameterIsWrong" \
                and "未定义的参数" in (data.get("msg") or ""):
            print("🔄 first_frame/last_frame 参数名不被接受，改用 ref_image_0 重试")
            payload = {"prompt": prompt, "duration": req.duration}
            if chosen_res:
                payload["resolution"] = chosen_res
            for i, url in enumerate(req.image_urls[:9]):
                payload[f"ref_image_{i}"] = to_val(url)
            resp = await submit(payload)
            data = resp.json() if resp.status_code == 200 else {}
        if resp.status_code != 200:
            print(f"❌ AutoDL 报错: {resp.text}")
            raise HTTPException(resp.status_code, f"AutoDL 提交失败：{resp.text}")
        # 平台参数校验错误也返回 HTTP 200（code=RequestParameterIsWrong），需检查业务码
        if data.get("code") not in (None, "Success"):
            msg = data.get("msg") or str(data)
            if "工作流不可用" in msg and frame_mode != "reference":
                raise HTTPException(503, f"工作流 {target_workflow} 不可用：请检查工作流 ID 拼写（首尾帧正确 ID 为 minimax_h3_lightx2v）。"
                                         "所有工作流共用同一个 ComfyUI 令牌，若 ID 无误请到 autodl.art 该应用页面确认其支持 API 调用。")
            if "工作流不可用" in msg:
                rid = data.get("request_id") or ""
                raise HTTPException(503, f"工作流 {target_workflow} 当前不可用（{msg}{'，request_id: ' + rid if rid else ''}）。请确认该应用与令牌匹配；参考模式可用 zm 系列工作流。")
            raise HTTPException(500, f"AutoDL 提交失败：{msg}")
        task_id = data.get("task_id") or (data.get("data") or {}).get("task_id") or data.get("id")
        if not task_id:
            raise HTTPException(500, f"未拿到 task_id：{data}")
        result = {"task_id": task_id, "raw": data}
        if missing_video_refs:
            result["warning"] = (
                f"已跳过 {len(missing_video_refs)} 张丢失的参考图（文件不在 static/uploads，多半是换电脑/搬项目时没拷 uploads 目录）："
                + "、".join(missing_video_refs)
                + "。任务已照常提交，但涉及的角色/场景可能与之前不一致；重新上传参考图可恢复。"
            )
        # 首尾帧差异预检：构图差异过大是中间帧崩坏（背景塌白/人物突变/硬切）的高发因素
        if frame_mode == "first_last" and req.first_frame_url and req.last_frame_url:
            div = await asyncio.to_thread(_frame_divergence, req.first_frame_url, req.last_frame_url)
            if div is not None:
                print(f"📊 首尾帧构图差异: {div:.3f}")
                if div >= 0.30:
                    result["warning"] = (result.get("warning") or "") + (f"首尾帧构图差异过大（{div:.0%}，相当于镜头大幅跨越）：中间过程极易崩坏"
                                         f"（背景消失、人物突变、硬切）。强烈建议拆成两镜接拍：先拍从首帧到中间状态，"
                                         f"再用「🎬 接拍下一镜」从尾帧接拍剩余动作。")
                elif div >= 0.15:
                    result["warning"] = (result.get("warning") or "") + (f"首尾帧构图差异较大（{div:.0%}）：中间过程可能出现不自然过渡。"
                                         f"若生成结果有跳变，建议把运镜描述写进提示词（如\"镜头缓缓推近\"），或拆成两镜接拍。")
        return result

# ---------- 5. AutoDL 查询视频结果 ----------
@app.get("/api/video-status/{task_id}")
async def video_status(task_id: str):
    # doubao: 前缀 = 豆包 Seedance 任务（火山方舟），走 Ark 查询通道
    if task_id.startswith(DOUBAO_TASK_PREFIX):
        _require_own_key("ark")
        return await get_doubao_task(task_id[len(DOUBAO_TASK_PREFIX):])
    _require_own_key("autodl")
    headers = {"Authorization": _eff_key("autodl")}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{AUTODL_BASE}/api/v1/comfyui/comfyui_workflow/result/{task_id}",
                headers=headers,
            )
    except httpx.HTTPError as e:
        # 网络偶发抖动：返回 UNKNOWN 让前端继续轮询，不中断流程
        print(f"⚠️ 查询任务状态网络异常（将自动重试）: {e}")
        return {"status": "UNKNOWN", "video_url": None, "raw": None}
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, f"查询失败：{resp.text}")
    data = resp.json()
    # 实测响应结构：{"code":"Success","data":{"status":"RUNNING","results":[...]}}
    inner = data.get("data") or {}
    status = str(inner.get("status") or data.get("status") or "").upper()
    video_url = None
    if status in ("SUCCESS", "COMPLETED", "SUCCEEDED"):
        results = inner.get("results") or data.get("results") or []
        for r in results:
            if isinstance(r, dict) and r.get("url"):
                video_url = r["url"]
                break
        if not video_url:
            video_url = (
                inner.get("video_url")
                or data.get("video_url")
                or data.get("output", {}).get("video_url")
            )
    return {"status": status, "video_url": video_url, "raw": data}

class SaveVideoRequest(BaseModel):
    task_id: str

@app.post("/api/save-video")
async def save_video(req: SaveVideoRequest):
    """把云端生成完成的视频下载到本地持久保存（AutoDL/火山方舟返回的签名链接都会过期）。"""
    # doubao: 前缀 = 豆包 Seedance 任务：先查 Ark 拿成片 URL，再下载落盘
    if req.task_id.startswith(DOUBAO_TASK_PREFIX):
        _require_own_key("ark")
        info = await get_doubao_task(req.task_id[len(DOUBAO_TASK_PREFIX):])
        if info["status"] not in ("SUCCESS", "COMPLETED", "SUCCEEDED"):
            raise HTTPException(400, f"任务尚未完成（当前状态：{info['status']}）")
        video_url = info.get("video_url")
        if not video_url:
            raise HTTPException(404, "任务完成但未找到视频文件")
        async with httpx.AsyncClient(timeout=120) as client:
            vresp = await client.get(video_url)
            if vresp.status_code != 200:
                raise HTTPException(500, "视频下载失败")
            filename = f"video_{uuid.uuid4().hex}.mp4"
            filepath = os.path.join("static/uploads", filename)
            with open(filepath, "wb") as f:
                f.write(vresp.content)
        return {"video_url": f"/static/uploads/{filename}"}

    _require_own_key("autodl")
    headers = {"Authorization": _eff_key("autodl")}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(
            f"{AUTODL_BASE}/api/v1/comfyui/comfyui_workflow/result/{req.task_id}",
            headers=headers,
        )
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"查询失败：{resp.text}")
        data = resp.json()
        inner = data.get("data") or {}
        status = str(inner.get("status") or "").upper()
        if status not in ("SUCCESS", "COMPLETED", "SUCCEEDED"):
            raise HTTPException(400, f"任务尚未完成（当前状态：{status}）")
        video_url = None
        for r in (inner.get("results") or []):
            if isinstance(r, dict) and r.get("url"):
                video_url = r["url"]
                break
        if not video_url:
            raise HTTPException(404, "任务完成但未找到视频文件")
        vresp = await client.get(video_url)
        if vresp.status_code != 200:
            raise HTTPException(500, "视频下载失败")
        filename = f"video_{uuid.uuid4().hex}.mp4"
        filepath = os.path.join("static/uploads", filename)
        with open(filepath, "wb") as f:
            f.write(vresp.content)
        return {"video_url": f"/static/uploads/{filename}"}

@app.get("/api/videos")
async def list_videos():
    """列出所有已保存到本地的视频（static/uploads/video_*.mp4，按时间倒序）。"""
    videos = []
    d = "static/uploads"
    if os.path.exists(d):
        files = [f for f in os.listdir(d) if f.startswith("video_") and f.endswith(".mp4")]
        files.sort(key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True)
        for f in files:
            p = os.path.join(d, f)
            videos.append({
                "url": f"/static/uploads/{f}",
                "name": f,
                "size_mb": round(os.path.getsize(p) / 1048576, 1),
                "mtime": int(os.path.getmtime(p) * 1000),
            })
    return {"videos": videos}

@app.delete("/api/videos/{name}")
async def delete_video(name: str):
    """删除一个已保存的本地视频文件（严格校验文件名，防止路径穿越）。"""
    if not re.fullmatch(r"video_[\w\-]+\.mp4", name):
        raise HTTPException(400, "非法文件名")
    p = os.path.join("static", "uploads", name)
    if not os.path.exists(p):
        raise HTTPException(404, "文件不存在")
    os.remove(p)
    return {"ok": True}

class VideoLastFrameRequest(BaseModel):
    video_url: str  # 本地视频地址（/static/uploads/video_*.mp4，带域名前缀也可）

@app.post("/api/video-last-frame")
async def video_last_frame(req: VideoLastFrameRequest):
    """抽取视频最后一帧存为图片——「接拍」用：上一镜没拍完，拿尾帧当下一镜的首帧参考。
    仅接受本项目生成的本地 video_*.mp4（严格校验文件名，防路径穿越）。"""
    fname = (req.video_url or "").split("?")[0].rstrip("/").split("/")[-1]
    if not re.fullmatch(r"video_[\w\-]+\.mp4", fname):
        raise HTTPException(400, "仅支持本项目生成的 video_*.mp4 本地视频")
    path = os.path.join("static", "uploads", fname)
    if not os.path.exists(path):
        raise HTTPException(404, f"找不到本地视频文件 {fname}（可能已被删除，请先在画廊里重新保存）")
    try:
        import cv2
    except ImportError:
        raise HTTPException(500, "服务器缺少 opencv-python-headless，请在 venv 里执行：pip install opencv-python-headless")
    cap = cv2.VideoCapture(path)
    frame = None
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        # 优先跳到最后整数帧读取；seek 失败（某些编码元数据不准）则顺序解码兜底
        if total > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, max(total - 2, 0))
            ok, frame = cap.read()
        if frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            while True:
                ok, f2 = cap.read()
                if not ok:
                    break
                frame = f2
    finally:
        cap.release()
    if frame is None:
        raise HTTPException(500, "抽帧失败：视频可能已损坏或为空")
    os.makedirs("static/uploads", exist_ok=True)
    out_name = f"frame_{uuid.uuid4().hex[:10]}.jpg"
    cv2.imwrite(os.path.join("static", "uploads", out_name), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return {"image_url": f"/static/uploads/{out_name}", "source_video": fname}

# ---------- 6. 清理临时文件（可选后台任务） ----------
def clean_temp_files():
    """清理超过 24 小时的临时图片文件"""
    import time
    now = time.time()
    temp_dir = "static/temp"
    if os.path.exists(temp_dir):
        for f in os.listdir(temp_dir):
            file_path = os.path.join(temp_dir, f)
            if os.path.isfile(file_path) and now - os.path.getmtime(file_path) > 86400:
                try:
                    os.remove(file_path)
                except Exception as e:
                    print(f"清理失败: {e}")

# ---------- 挂载前端 ----------
# 先把 /static/* 精确映射到 static 目录（/static/uploads/xxx.jpg -> static/uploads/xxx.jpg）
# ---------- 7. 设置页面：查看/修改 API 配置 ----------
class SettingsRequest(BaseModel):
    deepseek_api_key: str | None = None
    ark_api_key: str | None = None
    doubao_model: str | None = None
    doubao_base: str | None = None
    autodl_token: str | None = None
    autodl_i2v_token: str | None = None  # 可选：首尾帧应用(U02)独立令牌
    autodl_workflow_id: str | None = None
    autodl_base: str | None = None

def _write_env_file(updates: dict):
    """更新 .env 文件：改已有键的值、追加新键，保留注释与其他行"""
    lines = []
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as f:
            lines = f.readlines()
    remaining = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if ("=" in stripped and not stripped.startswith("#")) else None
        if key and key in remaining:
            out.append(f"{key}={remaining.pop(key)}\n")
        else:
            out.append(line)
    for k, v in remaining.items():
        out.append(f"{k}={v}\n")
    with open(".env", "w", encoding="utf-8") as f:
        f.writelines(out)

def _mask_secret(v: str) -> str:
    """密钥脱敏：只露前 4 位和后 4 位，未配置返回空串。"""
    v = (v or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return v[:2] + "****"
    return f"{v[:4]}****{v[-4:]}"

@app.get("/api/settings")
async def get_settings(request: Request):
    # 安全：API Key / Token 只返回脱敏摘要，永不明文传给前端；更换时在输入框填新值即可
    # 分区口令登录时：返回该分区自定义密钥（若有），并带 custom 标记 + is_admin=false
    me = _owner_of(request)
    is_admin = me is None
    overrides = {}
    if not is_admin:
        with _db() as conn:
            rows = conn.execute("SELECT key, value FROM user_settings WHERE owner=?", (me,)).fetchall()
        overrides = {r["key"]: r["value"] for r in rows}

    def _eff(db_key: str, global_val: str) -> str:
        return overrides.get(db_key) or (global_val or "")

    deepseek_val = _eff("deepseek_api_key", os.getenv("DEEPSEEK_API_KEY", ""))
    ark_val = _eff("ark_api_key", os.getenv("ARK_API_KEY", ""))
    autodl_val = _eff("autodl_token", AUTODL_TOKEN)
    return {
        "deepseek_api_key": _mask_secret(deepseek_val),
        "ark_api_key": _mask_secret(ark_val),
        "doubao_model": DOUBAO_MODEL,
        "doubao_base": DOUBAO_BASE,
        "autodl_token": _mask_secret(autodl_val),
        "autodl_i2v_token": _mask_secret(AUTODL_I2V_TOKEN),
        "autodl_workflow_id": AUTODL_WORKFLOW,
        "autodl_base": AUTODL_BASE,
        "custom": {
            "deepseek": "deepseek_api_key" in overrides,
            "ark": "ark_api_key" in overrides,
            "autodl": "autodl_token" in overrides,
        },
        "is_admin": is_admin,
    }

@app.post("/api/settings")
async def update_settings(request: Request, req: SettingsRequest):
    global DOUBAO_MODEL, DOUBAO_BASE, AUTODL_TOKEN, AUTODL_I2V_TOKEN, AUTODL_WORKFLOW, AUTODL_BASE, deepseek_client
    updates = {}

    # 分区口令保存：只写入自己的自定义密钥（互不影响，也不改全局 .env）
    if _owner_of(request) is not None and VALID_CODES:
        me = _owner_of(request)
        saved = {}
        for db_key, val in (
            ("deepseek_api_key", req.deepseek_api_key),
            ("ark_api_key", req.ark_api_key),
            ("autodl_token", req.autodl_token),
        ):
            if val is not None and val.strip():
                saved[db_key] = val.strip()
        if saved:
            with _db() as conn:
                for k, v in saved.items():
                    conn.execute("""INSERT INTO user_settings(owner,key,value) VALUES(?,?,?)
                                    ON CONFLICT(owner,key) DO UPDATE SET value=excluded.value""", (me, k, v))
            print(f"⚙️ 分区 {me} 已保存自定义密钥: {list(saved.keys())}")
        return {"saved": True, "updated": list(saved.keys()), "scope": "partition"}

    def set_env(env_key: str, value: str):
        updates[env_key] = value
        os.environ[env_key] = value

    if req.deepseek_api_key is not None and req.deepseek_api_key.strip():
        key = req.deepseek_api_key.strip()
        set_env("DEEPSEEK_API_KEY", key)
        deepseek_client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
    if req.ark_api_key is not None and req.ark_api_key.strip():
        set_env("ARK_API_KEY", req.ark_api_key.strip())
    if req.doubao_model is not None and req.doubao_model.strip():
        DOUBAO_MODEL = req.doubao_model.strip()
        set_env("DOUBAO_MODEL", DOUBAO_MODEL)
    if req.doubao_base is not None and req.doubao_base.strip():
        DOUBAO_BASE = req.doubao_base.strip()
        set_env("DOUBAO_BASE", DOUBAO_BASE)
    if req.autodl_token is not None and req.autodl_token.strip():
        AUTODL_TOKEN = req.autodl_token.strip()
        set_env("AUTODL_TOKEN", AUTODL_TOKEN)
    if req.autodl_i2v_token is not None and req.autodl_i2v_token.strip():
        AUTODL_I2V_TOKEN = req.autodl_i2v_token.strip()
        set_env("AUTODL_I2V_TOKEN", AUTODL_I2V_TOKEN)
    if req.autodl_workflow_id is not None and req.autodl_workflow_id.strip():
        AUTODL_WORKFLOW = req.autodl_workflow_id.strip()
        set_env("AUTODL_WORKFLOW_ID", AUTODL_WORKFLOW)
    if req.autodl_base is not None and req.autodl_base.strip():
        AUTODL_BASE = req.autodl_base.strip()
        set_env("AUTODL_BASE", AUTODL_BASE)

    if updates:
        _write_env_file(updates)
        print(f"⚙️ 设置已更新并写入 .env: {list(updates.keys())}")
    return {"saved": True, "updated": list(updates.keys())}

# ---------- 项目导出/导入：项目 JSON + 引用到的 static/uploads 媒体文件打包成 zip ----------
class ExportProjectRequest(BaseModel):
    name: str = "未命名项目"
    data: Dict[str, Any]

def _collect_upload_refs(obj: Any, found: set) -> None:
    """递归收集项目数据里引用的所有 /static/uploads/ 文件名。"""
    if isinstance(obj, str):
        for m in re.findall(r"/static/uploads/([A-Za-z0-9_.\-]+)", obj):
            found.add(m)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_upload_refs(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _collect_upload_refs(v, found)

@app.post("/api/export-project")
async def export_project(req: ExportProjectRequest):
    blob = json.dumps(req.data, ensure_ascii=False)
    found: set = set()
    _collect_upload_refs(req.data, found)
    packed = 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.json", blob)
        for name in sorted(found):
            # 防路径穿越：只要纯文件名
            safe_name = os.path.basename(name)
            p = os.path.join("static", "uploads", safe_name)
            if safe_name and os.path.isfile(p):
                zf.write(p, f"uploads/{safe_name}")
                packed += 1
    buf.seek(0)
    safe_stem = re.sub(r'[\\/:*?"<>|\s]+', "_", req.name).strip("_") or "project"
    filename = f"{safe_stem}.aianime.zip"
    print(f"📤 导出项目「{req.name}」：媒体文件 {packed} 个，包大小 {buf.getbuffer().nbytes / 1024 / 1024:.1f} MB")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=\"export.aianime.zip\"; filename*=UTF-8''{quote(filename)}"},
    )

@app.post("/api/import-project")
async def import_project(file: UploadFile = File(...)):
    try:
        zf = zipfile.ZipFile(io.BytesIO(await file.read()))
    except zipfile.BadZipFile:
        raise HTTPException(400, "不是有效的项目导出包（应为 .aianime.zip）")
    with zf:
        names = zf.namelist()
        if "project.json" not in names:
            raise HTTPException(400, "导出包缺少 project.json，无法导入")
        try:
            project = json.loads(zf.read("project.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(400, "project.json 解析失败，导出包可能已损坏")
        if not isinstance(project, dict):
            raise HTTPException(400, "project.json 结构异常，无法导入")
        restored = 0
        for n in names:
            if n.startswith("uploads/") and not n.endswith("/"):
                fn = os.path.basename(n)  # 防路径穿越，只取纯文件名
                if not fn or fn != n.split("/", 1)[1]:
                    continue
                dst = os.path.join("static", "uploads", fn)
                if not os.path.exists(dst):  # 同名文件已存在则跳过，不覆盖
                    with open(dst, "wb") as f:
                        f.write(zf.read(n))
                    restored += 1
    print(f"📥 导入项目「{project.get('name', '')}」：恢复媒体文件 {restored} 个")
    return {"project": project, "restored_files": restored}

# ============================================================
# 手机版新增 1：SQLite 项目持久化层
# 项目数据不再存浏览器 localStorage（iOS 7 天不访问会被清空），
# 统一存到用户主目录 ~/.anime_studio/studio.db（重发布不会覆盖，见下方数据保护说明）。
# ============================================================
import sqlite3
import time as _time
import subprocess as _subprocess
from fastapi.responses import JSONResponse

# ============================================================
# 持久数据保护（2026-11-13 事故修复）：发布平台每次部署会用上传目录整体覆盖线上，
# 曾把线上新建的项目（studio.db + static/uploads 素材）全部冲掉。
# 修复：DB 与生成素材一律存到用户主目录 ~/.anime_studio/（沙箱重发布不触碰），
# 首次运行自动迁入目录内旧数据；static/uploads 用目录链接指过去，代码无需改路径。
# ============================================================
_DATA_HOME = os.path.join(os.path.expanduser("~"), ".anime_studio")
os.makedirs(_DATA_HOME, exist_ok=True)

# --- studio.db：优先主目录；目录内旧库只在没有主目录库时迁入（线上库永不被上传覆盖） ---
DB_PATH = os.path.join(_DATA_HOME, "studio.db")
if not os.path.exists(DB_PATH):
    _legacy_db = os.path.join(os.getcwd(), "studio.db")
    if os.path.exists(_legacy_db):
        try:
            shutil.copy2(_legacy_db, DB_PATH)
            print(f"[数据保护] 已迁移旧数据库 {_legacy_db} → {DB_PATH}")
        except Exception as _e:
            print(f"[数据保护] 旧数据库迁移失败，改用目录内库: {_e}")
            DB_PATH = _legacy_db


def _sync_uploads():
    """uploads 双向同步（不依赖符号链接，任何环境都可用）：
    1) 应用目录里主目录没有的文件 → 并入主目录（本次发布上传带来的新文件，线上已有同名文件以线上为准）
    2) 主目录里应用目录缺的文件 → 补回应用目录（上次运行生成、被发布覆盖掉的素材，恢复可访问）
    运行期写入仍走应用目录（代码零改动），重启时再并入主目录持久保存。"""
    home = os.path.join(_DATA_HOME, "uploads")
    os.makedirs(home, exist_ok=True)
    app_dir = os.path.join(os.getcwd(), "static", "uploads")
    os.makedirs(app_dir, exist_ok=True)
    merged = restored = 0
    for n in os.listdir(app_dir):
        dst = os.path.join(home, n)
        if not os.path.exists(dst):
            try:
                shutil.move(os.path.join(app_dir, n), dst)
                merged += 1
            except Exception:
                pass
    for n in os.listdir(home):
        dst = os.path.join(app_dir, n)
        if not os.path.exists(dst):
            try:
                shutil.copy2(os.path.join(home, n), dst)
                restored += 1
            except Exception:
                pass
    print(f"[数据保护] uploads 同步完成：并入 {merged} 个，补回 {restored} 个（主目录: {home}）")


_sync_uploads()

ACCESS_CODE = (os.getenv("ACCESS_CODE") or "").strip()  # .env 里配置，未配置则不启用口令
# 多口令分区：ACCESS_CODES 里每个口令一个独立项目分区（互相不可见）；
# ACCESS_CODE 为主口令，可见所有分区。示例：ACCESS_CODES=朋友甲的口令,朋友乙的口令
EXTRA_CODES = [c.strip() for c in (os.getenv("ACCESS_CODES") or "").split(",") if c.strip()]
VALID_CODES = set(c for c in [ACCESS_CODE] + EXTRA_CODES if c)

def _owner_of(request) -> str | None:
    """返回当前请求的项目分区 key：None=主口令/未启用守卫（可见全部）；否则为口令本身（只可见自己的分区）。"""
    code = (request.headers.get("x-access-code") or "").strip()
    if not VALID_CODES:
        return None
    if ACCESS_CODE and code == ACCESS_CODE:
        return None
    if code in EXTRA_CODES:
        return code
    return None

def _owner_label_of(owner) -> str:
    """分区显示名：主口令/历史项目 → 管理员；分区口令 → OWNER_LABELS 里的名字＋「分区」后缀。"""
    if not owner or owner == ACCESS_CODE:
        return "管理员"
    labels = {}
    for pair in (os.getenv("OWNER_LABELS") or "").split(","):
        if ":" in pair:
            k, v = pair.split(":", 1)
            labels[k.strip()] = v.strip()
    return f"{labels.get(owner, owner)}分区"

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    with _db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS projects(
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '未命名项目',
            data TEXT NOT NULL DEFAULT '{}',
            updated_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL DEFAULT 0)""")
        # 多口令分区：owner=口令（主口令/历史项目为 NULL）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
        if "owner" not in cols:
            conn.execute("ALTER TABLE projects ADD COLUMN owner TEXT")
        # 分区自定义 API 密钥：每个分区口令可存自己的 DeepSeek/ARK/AutoDL 密钥
        conn.execute("""CREATE TABLE IF NOT EXISTS user_settings(
            owner TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY(owner,key))""")
_init_db()

class LoginRequest(BaseModel):
    code: str = ""

@app.post("/api/login")
async def login(req: LoginRequest):
    """校验访问口令。主口令 + 各分区口令均可登录。.env 未配置任何口令时视为不设防（本地调试）。"""
    if not VALID_CODES:
        return {"ok": True, "protected": False}
    if req.code.strip() in VALID_CODES:
        return {"ok": True, "protected": True}
    raise HTTPException(403, "口令不正确")

class ProjectCreateRequest(BaseModel):
    name: str = "未命名项目"
    data: Dict[str, Any] | None = None

class ProjectSaveRequest(BaseModel):
    name: str = "未命名项目"
    data: Dict[str, Any]
    base_updated_at: int | None = None  # 客户端打开项目/上次保存时拿到的 updated_at（版本戳）

@app.get("/api/projects")
async def list_projects(request: Request):
    """项目列表（按更新时间倒序）。主口令可见全部分区；分区口令只可见自己的项目。"""
    owner = _owner_of(request)
    with _db() as conn:
        if owner is None:
            rows = conn.execute("SELECT id, name, data, updated_at, owner FROM projects ORDER BY updated_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT id, name, data, updated_at, owner FROM projects WHERE owner=? ORDER BY updated_at DESC", (owner,)).fetchall()
    out = []
    for r in rows:
        d = r["data"]
        m = re.search(r"/static/uploads/(img_[A-Za-z0-9_.\-]+)", d)
        out.append({
            "id": r["id"],
            "name": r["name"],
            "updated_at": int(r["updated_at"] * 1000),
            "cover": f"/static/uploads/{m.group(1)}" if m else None,
            "board_count": d.count('"shot_number"'),
            "owner": r["owner"] or ACCESS_CODE or "",
            "owner_label": _owner_label_of(r["owner"]),
        })
    return {"projects": out}

@app.post("/api/projects")
async def create_project(request: Request, req: ProjectCreateRequest):
    pid = uuid.uuid4().hex[:12]
    now = _time.time()
    data = req.data if req.data is not None else {"name": req.name}
    owner = _request_code(request)
    with _db() as conn:
        conn.execute("INSERT INTO projects(id,name,data,updated_at,created_at,owner) VALUES(?,?,?,?,?,?)",
                     (pid, req.name, json.dumps(data, ensure_ascii=False), now, now, owner))
    return {"id": pid, "name": req.name, "data": data,
            "owner": owner or ACCESS_CODE or "", "updated_at": int(now * 1000)}

@app.get("/api/projects/{pid}")
async def get_project(pid: str, request: Request):
    with _db() as conn:
        r = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    if not r or not _can_touch(request, r["owner"]):
        raise HTTPException(404, "项目不存在")
    return {"id": r["id"], "name": r["name"],
            "updated_at": int(r["updated_at"] * 1000), "data": json.loads(r["data"]),
            "owner": r["owner"] or ACCESS_CODE or ""}

def _can_touch(request, row_owner) -> bool:
    """主口令（owner=None 请求方）可操作任何项目；分区口令只能操作自己名下的。"""
    me = _owner_of(request)
    return me is None or me == row_owner

def _request_code(request) -> str | None:
    """返回请求者的原始口令（无守卫时 None）。与 _owner_of 的区别：主口令返回主口令本身而非 None。"""
    code = (request.headers.get("x-access-code") or "").strip()
    return code if code in VALID_CODES else None

@app.post("/api/projects/{pid}")
async def save_project(pid: str, request: Request, req: ProjectSaveRequest):
    """前端自动保存入口：整个项目 JSON upsert 进 SQLite。

    多端同步规则：客户端带 base_updated_at（打开项目/上次保存时的版本戳）。
    若云端在此之后已被其他设备更新（updated_at 更新），拒绝写入返回 409，
    客户端据此拉取云端最新版本——保证「后保存的、基于最新版本的数据」生效，
    旧页面的过期数据永远覆盖不了新数据。
    """
    now = _time.time()
    blob = json.dumps(req.data, ensure_ascii=False)
    with _db() as conn:
        r = conn.execute("SELECT id, owner, updated_at FROM projects WHERE id=?", (pid,)).fetchone()
        if r:
            if not _can_touch(request, r["owner"]):
                raise HTTPException(404, "项目不存在")
            server_ms = int(r["updated_at"] * 1000)
            # 版本冲突：云端比客户端的基准新（留 1 秒容差吸收毫秒取整误差）→ 拒绝覆盖
            if req.base_updated_at is not None and server_ms > req.base_updated_at + 1000:
                return JSONResponse(status_code=409, content={
                    "detail": "项目已在其他设备更新",
                    "server_updated_at": server_ms,
                })
            conn.execute("UPDATE projects SET name=?, data=?, updated_at=? WHERE id=?",
                         (req.name, blob, now, pid))
            owner = r["owner"]
        else:
            owner = _request_code(request)
            conn.execute("INSERT INTO projects(id,name,data,updated_at,created_at,owner) VALUES(?,?,?,?,?,?)",
                         (pid, req.name, blob, now, now, owner))
    return {"saved": True, "id": pid, "name": req.name, "data": req.data,
            "owner": owner or ACCESS_CODE or "", "updated_at": int(now * 1000)}

# ---------- 删除同步：界面删除图片/视频/项目时，服务器文件一并清理 ----------
# 命名白名单：只允许删除本应用生成的上传文件（img_/video_/diagram_*），防路径穿越与误删
_UPLOAD_RE = re.compile(r"^(img|video|refvideo|diagram_topview|diagram_trajectory|voice|dub)_[0-9a-f]{6,40}\.(jpg|jpeg|png|webp|gif|mp4|webm|mov|m4v|wav|mp3|m4a|aac|ogg|flac)$", re.I)

def _collect_upload_urls(obj, out):
    """递归收集 JSON 数据里引用的所有 /static/uploads/ 文件 URL"""
    if isinstance(obj, str):
        if obj.startswith("/static/uploads/"):
            out.add(obj.split("?")[0])
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_upload_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_upload_urls(v, out)

def _referenced_urls(exclude_pid=None):
    """数据库里所有项目（可排除某个）引用的上传文件 URL 集合"""
    refs = set()
    with _db() as conn:
        if exclude_pid:
            rows = conn.execute("SELECT data FROM projects WHERE id != ?", (exclude_pid,)).fetchall()
        else:
            rows = conn.execute("SELECT data FROM projects").fetchall()
    for (data,) in rows:
        try:
            _collect_upload_urls(json.loads(data), refs)
        except Exception:
            pass
    return refs

def _prune_upload_files(urls, refs):
    """安全删除：只删 static/uploads 下符合命名白名单、且不再被任何项目引用的文件。
    引用检查保证「复制出的项目共用同一批文件」时不会误删另一个项目还在用的文件。"""
    deleted = []
    updir = os.path.realpath("static/uploads")
    for u in urls or []:
        name = os.path.basename(str(u).split("?")[0])
        if not _UPLOAD_RE.match(name):
            continue
        p = os.path.realpath(os.path.join(updir, name))
        if not p.startswith(updir + os.sep) or not os.path.isfile(p):
            continue
        if f"/static/uploads/{name}" in refs:
            continue  # 仍被其他项目引用，保护不删
        try:
            os.remove(p)
            deleted.append(name)
        except OSError as e:
            print(f"[files] 删除 {name} 失败: {e}")
    return deleted

class DeleteFilesRequest(BaseModel):
    urls: List[str]

@app.post("/api/delete-files")
async def delete_files(req: DeleteFilesRequest):
    """界面删除图片/视频后同步清理服务器文件。
    只删除「已不被任何项目引用」且命名符合白名单的上传文件，安全兜底。"""
    refs = _referenced_urls()
    deleted = _prune_upload_files(req.urls, refs)
    if deleted:
        print(f"[files] 已清理 {len(deleted)} 个服务器文件: {', '.join(deleted[:5])}{'…' if len(deleted) > 5 else ''}")
    return {"ok": True, "deleted": deleted}

@app.delete("/api/projects/{pid}")
async def delete_project(pid: str, request: Request):
    with _db() as conn:
        r = conn.execute("SELECT owner, data FROM projects WHERE id=?", (pid,)).fetchone()
        if not r or not _can_touch(request, r["owner"]):
            raise HTTPException(404, "项目不存在")
        urls = set()
        try:
            _collect_upload_urls(json.loads(r["data"]), urls)
        except Exception:
            pass
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    # 项目删除后：该项目引用、且不再被其他项目使用的图片/视频文件一并从服务器清理
    refs = _referenced_urls()
    deleted = _prune_upload_files(urls, refs)
    print(f"[files] 项目 {pid} 已删除，同步清理服务器文件 {len(deleted)} 个")
    return {"ok": True, "deleted_files": len(deleted)}

# ============================================================
# 手机版新增 2：访问口令守卫（公网部署必备，防止陌生人用你的 key 烧钱）
# .env 里写 ACCESS_CODE=你的口令（主口令，可见全部项目）
# 可选：ACCESS_CODES=口令甲,口令乙 → 每个口令一个独立项目分区，互相不可见
# 前端登录一次后记住。
# ============================================================
@app.middleware("http")
async def access_code_guard(request, call_next):
    path = request.url.path
    if VALID_CODES and path.startswith("/api") and path != "/api/login":
        if (request.headers.get("x-access-code") or "").strip() not in VALID_CODES:
            return JSONResponse({"detail": "需要访问口令"}, status_code=401)
    return await call_next(request)

# ---------- 分区密钥注入：把分区口令保存的自定义密钥挂到请求上下文，生成环节自动优先使用 ----------
@app.middleware("http")
async def partition_keys_ctx(request, call_next):
    owner = _owner_of(request)
    ov = {}
    if owner:
        try:
            with _db() as conn:
                rows = conn.execute("SELECT key, value FROM user_settings WHERE owner=?", (owner,)).fetchall()
            ov = {r["key"]: r["value"] for r in rows if r["key"] and r["value"]}
        except Exception as e:
            print(f"[keys] 读取分区自定义密钥失败: {e}")
    _REQ_KEYS.set(ov)
    _REQ_OWNER.set(owner)
    return await call_next(request)

# ---------- 前端页面响应头：禁止缓存 index.html，避免浏览器用旧页面看不到新功能 ----------
@app.middleware("http")
async def no_cache_html(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    elif path.startswith("/static/uploads"):
        response.headers["Cache-Control"] = "public, max-age=604800"  # 生成物缓存 7 天
    elif path.endswith(".js") or path.endswith(".css"):
        # 前端代码文件：每次使用前必须向服务器校验新版本，防止手机浏览器用旧缓存
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

# ---------- 根路径：手机版界面（桌面版完整入口保留在 /static/index.html） ----------
from fastapi.responses import FileResponse

@app.get("/", include_in_schema=False)
async def root_page():
    return FileResponse("static/mobile.html")

app.mount("/static", StaticFiles(directory="static"), name="assets")
# 再把根路径映射到前端页面（保持 html=True 以便访问 / 时自动返回 index.html）
app.mount("/", StaticFiles(directory="static", html=True), name="static")

# ---------- 配音微服务自动拉起 ----------
# 背景：voice_server.py 是独立进程（VoxCPM 依赖装在单独的 venv 里），
# 直接 `python main.py` 启动时它不会跟着起来，配音/声音克隆就会静默没有声音。
# 这里在主服务启动时探测端口，没起来就用「装好 voxcpm 的解释器」拉起一个，
# 让单条命令也能用全部功能。launch.py 会传 VOICE_AUTOSTART=0 避免重复启动。
def _port_listening(host: str, port: int, timeout: float = 0.8) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        try: s.close()
        except Exception: pass


def _voice_python_candidates():
    """候选解释器：VOICE_PYTHON 环境变量 → 邻近 VoxCPM venv → 项目内 venv → 当前解释器"""
    env_py = (os.getenv("VOICE_PYTHON") or "").strip()
    if env_py:
        yield env_py
    base = os.path.dirname(os.path.abspath(__file__))
    for rel in (("..", "VoxCPM-main", "venv", "Scripts", "python.exe"),
                ("..", "VoxCPM-main", "venv", "bin", "python"),
                ("venv", "Scripts", "python.exe"),
                ("venv", "bin", "python")):
        yield os.path.normpath(os.path.join(base, *rel))
    yield sys.executable


def maybe_autostart_voice():
    """配音服务不在线时自动拉起（失败只提示，不影响主服务）"""
    if (os.getenv("VOICE_AUTOSTART") or "1").strip() in ("0", "false", "False", "no"):
        return
    from urllib.parse import urlparse
    u = urlparse(VOICE_SERVER_URL)
    host = u.hostname or "127.0.0.1"
    port = u.port or 8021
    if host not in ("127.0.0.1", "localhost", "0.0.0.0"):
        return  # 远端配音服务，不本地拉起
    if _port_listening("127.0.0.1", port):
        print(f"[voice] 配音服务已在线（:{port}）", flush=True)
        return
    import subprocess
    for py in _voice_python_candidates():
        if not (py and os.path.isfile(py)):
            continue
        try:
            ok = subprocess.run([py, "-c", "import voxcpm, soundfile"],
                                capture_output=True, timeout=180).returncode == 0
        except Exception:
            continue
        if not ok:
            continue
        try:
            logf = open("voice_server.log", "ab", buffering=0)
            subprocess.Popen(
                [py, "-u", "voice_server.py"],
                cwd=os.path.dirname(os.path.abspath(__file__)) or ".",
                stdout=logf, stderr=subprocess.STDOUT,
                env={**os.environ, "VOXCPM_PORT": str(port)},
            )
            print(f"[voice] 已自动拉起配音服务（:{port}，模型加载约 1-2 分钟）", flush=True)
        except Exception as e:
            print(f"[voice] 配音服务自动启动失败: {type(e).__name__}: {e}", flush=True)
        return
    print("[voice] 未找到装好 voxcpm 的解释器，配音需手动启动 voice_server.py（见设置页说明）", flush=True)


if __name__ == "__main__":
    import uvicorn
    # 启动时清理一次旧文件
    clean_temp_files()
    maybe_autostart_voice()
    # 云托管沙箱通过 PORT 环境变量注入端口；本地默认 8010
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8010")))