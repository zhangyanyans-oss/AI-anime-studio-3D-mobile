"""
VoxCPM 配音微服务 —— 为 AI 动画工作室提供 文本转语音 / 声音克隆 能力。

本地启动（torch 等重依赖在 VoxCPM venv 里）：
  C:/Users/ZYY/Downloads/VoxCPM-main/venv/Scripts/python.exe voice_server.py

云端（launch.py）会用系统 python 直接跑本文件；依赖缺失时由 launch.py 提前装好。
模型 openbmb/VoxCPM-0.5B：本地已缓存则离线加载；未缓存（云端首次）自动从 hf-mirror 下载，
下载完成后服务启动即后台预热，/health 的 warming=true 表示预热进行中。
"""
import base64
import os
import re

import tempfile
import threading
import uuid

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ---- torchaudio.load 兼容补丁 ----
# torch 2.15 的 torchaudio.load 走 torchcodec，需要 FFmpeg full-shared DLL（本机没有）。
# 改用 soundfile/librosa 解码（wav/mp3/flac/ogg 均可），返回 (ch, frames) 张量与采样率，
# 与 torchaudio.load 的返回约定一致。必须在 voxcpm 模型调用前生效。
def _patch_torchaudio_load():
    import torch
    import torchaudio
    # 限制线程数：降低内存/CPU 开销，避免在小内存容器里被 OOM 杀掉
    try:
        torch.set_num_threads(int(os.getenv("VOXCPM_THREADS", "2")))
    except Exception:
        pass
    if getattr(torchaudio, "_voxcpm_patched", False):
        return
    def _load(filepath, *args, **kwargs):
        try:
            data, sr = sf.read(str(filepath), dtype="float32", always_2d=True)  # (frames, ch)
        except Exception:
            import librosa
            data, sr = librosa.load(str(filepath), sr=None, mono=False)
            if data.ndim == 1:
                data = data[:, None]  # (frames, 1)
        wav = torch.from_numpy(np.ascontiguousarray(data.T))  # (ch, frames)
        return wav, int(sr)
    torchaudio.load = _load
    torchaudio._voxcpm_patched = True

_patch_torchaudio_load()

MODEL_ID = os.getenv("VOXCPM_MODEL", "openbmb/VoxCPM-0.5B")
DEVICE = os.getenv("VOXCPM_DEVICE", "cpu")          # 本机 torch 为 CPU 版
PORT = int(os.getenv("VOXCPM_PORT", "8021"))
MAX_TEXT = 500                                       # 单次合成文本上限（字）

# 本地模型目录（随发布包上传 / 或沙箱内从 ModelScope 下载后落到这里）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_MODEL_DIR = os.path.join(BASE_DIR, "models", MODEL_ID.split("/")[-1])
MS_BASE = f"https://modelscope.cn/models/{MODEL_ID}/resolve/master/"
MS_FILES = ["config.json", "pytorch_model.bin", "audiovae.pth",
            "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"]


def _local_dir_complete() -> bool:
    return os.path.isdir(LOCAL_MODEL_DIR) and all(
        os.path.isfile(os.path.join(LOCAL_MODEL_DIR, f)) for f in MS_FILES)


def _download_modelscope():
    """ModelScope 国内直连下载（支持 Range 断点续传，跨进程重启累积进度）"""
    import time
    import urllib.request
    os.makedirs(LOCAL_MODEL_DIR, exist_ok=True)
    for fn in MS_FILES:
        dest = os.path.join(LOCAL_MODEL_DIR, fn)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            continue
        url = MS_BASE + fn
        tmp = dest + ".part"
        done = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
        headers = {"Range": f"bytes={done}-"} if done else {}
        t0 = time.time()
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0)) + done
            with open(tmp, "ab") as out:
                got = done
                last_log = 0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
                    if got - last_log >= 100 * 1024 * 1024:
                        last_log = got
                        speed = (got - done) / max(1e-6, time.time() - t0) / 1024 / 1024
                        print(f"[voice] modelscope {fn}: {got//1024//1024}/{total//1024//1024}MB（{speed:.1f}MB/s）", flush=True)
        if os.path.getsize(tmp) < 100:
            os.remove(tmp)
            raise RuntimeError(f"modelscope 下载 {fn} 内容异常")
        os.replace(tmp, dest)
        print(f"[voice] modelscope 完成 {fn}（{os.path.getsize(dest)//1024//1024}MB）", flush=True)

app = FastAPI(title="VoxCPM Dubbing Service")
_tts = None                                          # 惰性加载的 VoxCPM 实例
_gen_lock = threading.Lock()                         # 模型推理非线程安全，串行化
_model_lock = threading.Lock()                       # 模型加载/下载串行化
_warming = False                                     # 预热（下载+加载）进行中


def _model_cached() -> bool:
    """模型权重是否已在本地 HF 缓存"""
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(MODEL_ID, local_files_only=True)
        return True
    except Exception:
        return False


def _strip_proxy():
    """离线加载模式下清掉代理环境变量：任何残留的网络请求都会被代理 502 打断（ProxyError）"""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def _ensure_weights():
    """确保权重可用：本地目录 → HF 缓存（离线）→ hf-mirror 下载 → ModelScope 直连下载"""
    global _warming
    _warming = True
    try:
        if _local_dir_complete():
            print(f"[voice] 使用本地模型目录 {LOCAL_MODEL_DIR}", flush=True)
            return
        if _model_cached():
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            _strip_proxy()
            print("[voice] 模型权重已缓存，离线加载", flush=True)
            return
        try:
            print(f"[voice] 权重未缓存，从 ModelScope 直连下载（断点续传）{MODEL_ID}...", flush=True)
            _download_modelscope()
            print("[voice] ModelScope 权重下载完成", flush=True)
            return
        except Exception as me:
            print(f"[voice] ModelScope 下载失败: {type(me).__name__}: {me}，改走 hf-mirror...", flush=True)
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        print(f"[voice] 尝试从 {os.environ.get('HF_ENDPOINT', 'HF')} 下载 {MODEL_ID}...", flush=True)
        from huggingface_hub import snapshot_download
        snapshot_download(MODEL_ID, max_workers=4)
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        print("[voice] 权重下载完成", flush=True)
    except Exception as e:
        print(f"[voice] 权重准备失败（首次 /tts 会重试）: {type(e).__name__}: {e}", flush=True)
    finally:
        _warming = False


def _preload():
    """启动即后台预热：下载（如需）+ 加载模型，避免首个请求等太久"""
    _ensure_weights()
    try:
        get_tts()
    except Exception as e:
        print(f"[voice] 预热加载失败: {type(e).__name__}: {e}", flush=True)


@app.on_event("startup")
def _startup_preload():
    threading.Thread(target=_preload, daemon=True).start()


def get_tts():
    global _tts
    if _tts is None:
        with _model_lock:
            if _tts is None:
                from voxcpm import VoxCPM
                use_local = _local_dir_complete()
                if use_local:
                    # 本地目录加载：强制离线，避免 from_pretrained 去 HF Hub 在线解析 revision
                    os.environ["HF_HUB_OFFLINE"] = "1"
                    os.environ["TRANSFORMERS_OFFLINE"] = "1"
                    _strip_proxy()
                model_ref = LOCAL_MODEL_DIR if use_local else MODEL_ID
                print(f"[voice] 加载模型 {model_ref}（首次约 20-60 秒）...", flush=True)
                _tts = VoxCPM.from_pretrained(
                    model_ref,
                    local_files_only=use_local,
                    load_denoiser=False,   # 降噪器是 ModelScope 另一个模型，按需关掉避免额外下载
                    optimize=False,        # torch.compile 在 Windows CPU 上又慢又脆，关掉
                    device=DEVICE,
                )
                print("[voice] 模型就绪", flush=True)
    return _tts


def _audio_duration(path) -> float:
    """读音频时长（秒）；解不出来返回 0，调用方跳过校验"""
    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        return len(data) / float(sr)
    except Exception:
        pass
    try:
        import librosa
        y, sr = librosa.load(str(path), sr=None, mono=True)
        return len(y) / float(sr)
    except Exception:
        return 0.0


def _prepare_prompt_audio(src: str):
    """参考音频预处理：单声道 → 16k → 裁掉首尾静音 → 峰值归一 → 淡入淡出。

    手机录音普遍带长段开头静音和音量不均，直接喂给模型会明显拉低音色相似度
    （模型会把静音也当成"音色"的一部分来学）。返回 (新路径, 统计信息)。
    """
    import librosa
    sr = 16000
    try:
        y, _ = librosa.load(str(src), sr=sr, mono=True)
    except Exception:
        return src, {}
    if y is None or y.size == 0:
        return src, {}
    orig_sec = len(y) / float(sr)
    # 裁静音：top_db=32 只切掉明显低于峰值 32dB 的部分，避免切掉气口和辅音
    try:
        yt, idx = librosa.effects.trim(y, top_db=32, frame_length=1024, hop_length=256)
        if yt.size > sr * 0.5:
            y = yt
    except Exception:
        pass
    ref_sec = len(y) / float(sr)
    # 峰值归一，避免录得太小声导致模型学不到音色
    peak = float(np.max(np.abs(y))) or 0.0
    if peak > 0:
        y = y * (0.9 / peak)
    # 头尾 10ms 淡入淡出，去掉边界爆点
    n_fade = min(int(0.01 * sr), len(y) // 2)
    if n_fade > 1:
        ramp = np.linspace(0.0, 1.0, n_fade, dtype=np.float32)
        y[:n_fade] *= ramp
        y[-n_fade:] *= ramp[::-1]
    out = os.path.join(tempfile.gettempdir(), f"voxcpm_ref_{uuid.uuid4().hex}.wav")
    sf.write(out, np.ascontiguousarray(y, dtype=np.float32), sr, format="WAV")
    return out, {"src_sec": round(orig_sec, 2), "ref_sec": round(ref_sec, 2)}


class TTSRequest(BaseModel):
    text: str
    prompt_wav_b64: str | None = None   # 声音克隆参考音频（wav/mp3 等二进制的 base64）
    prompt_text: str | None = None      # 参考音频里说的那句话（克隆必填）
    cfg_value: float = 2.0              # 引导强度，越大越贴参考音色（1-4）
    inference_timesteps: int = 10       # 越大越稳越慢（4-25）


class TTSResponse(BaseModel):
    audio_b64: str
    sample_rate: int


@app.get("/health")
def health():
    return {"ready": _tts is not None, "warming": _warming, "model": MODEL_ID, "device": DEVICE}


@app.post("/tts", response_model=TTSResponse)
def tts(req: TTSRequest):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "text 不能为空")
    if len(text) > MAX_TEXT:
        raise HTTPException(400, f"文本过长（最多 {MAX_TEXT} 字），请拆分合成")

    # 参考音频：解 base64 落临时文件（torchaudio.load 支持 wav/mp3/flac 等）
    prompt_wav_path = None
    if req.prompt_wav_b64:
        try:
            raw = base64.b64decode(req.prompt_wav_b64)
        except Exception:
            raise HTTPException(400, "prompt_wav_b64 不是有效的 base64")
        if len(raw) < 1000:
            raise HTTPException(400, "参考音频内容为空或损坏")
        if not (req.prompt_text or "").strip():
            raise HTTPException(400, "声音克隆必须提供 prompt_text（参考音频的文字内容）")
        prompt_wav_path = os.path.join(tempfile.gettempdir(), f"voxcpm_prompt_{uuid.uuid4().hex}.wav")
        with open(prompt_wav_path, "wb") as f:
            f.write(raw)
        # 参考文本必须与音频内容一致：错位会让音色提取失败，
        # 听感就是「随机音色 + 破音」。按正常中文语速 3-7 字/秒做粗校验。
        dur = _audio_duration(prompt_wav_path)
        n_chars = len(re.sub(r"[\s\W_]+", "", req.prompt_text))
        if dur >= 4.0:
            cps = n_chars / dur
            if cps < 1.2 or cps > 15:
                try: os.remove(prompt_wav_path)
                except OSError: pass
                raise HTTPException(400, (
                    f"参考文本和音频对不上：音频 {dur:.1f} 秒，文本只有 {n_chars} 个字"
                    f"（正常说话约 3-7 字/秒）。"
                    "「音色参考文本」要填这段参考音频里实际说的那句话，不是你这次想合成的台词；"
                    "填错会让音色克隆失败，听起来就像随机音色还破音。"
                ))

    prepared_ref = None
    try:
        m = get_tts()
        cfg = float(min(4.0, max(1.0, req.cfg_value)))
        if prompt_wav_path and cfg <= 2.05:
            cfg = 2.6   # 克隆场景：默认引导强度偏弱，提一点更贴参考音色
        kwargs = dict(
            text=text,
            cfg_value=cfg,
            inference_timesteps=int(min(25, max(4, req.inference_timesteps))),
        )
        if prompt_wav_path:
            prepared_ref, ref_info = _prepare_prompt_audio(prompt_wav_path)
            ref_sec = ref_info.get("ref_sec")
            if ref_sec is not None and ref_sec < 2.5:
                raise HTTPException(400, f"参考音频有效人声只有 {ref_sec} 秒，太短了：请提供 5-20 秒的清晰人声（模型至少需要 3 秒才能认出音色）")
            print(f"[voice] 参考音频预处理：{ref_info} → cfg={cfg}", flush=True)
            kwargs["prompt_wav_path"] = prepared_ref
            kwargs["prompt_text"] = req.prompt_text.strip()
        with _gen_lock:
            wav = m.generate(**kwargs)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"合成失败：{type(e).__name__}: {e}")
    finally:
        for _p in {prompt_wav_path, prepared_ref}:
            if _p and os.path.exists(_p):
                try: os.remove(_p)
                except OSError: pass

    wav = np.asarray(wav).squeeze()
    if wav.ndim != 1 or wav.size < 160:
        raise HTTPException(500, "合成结果异常（空音频），请重试")
    # ---- 防破音后处理 ----
    # 模型输出经常贴到满刻度（peak≈1.0），写进 16bit WAV 就是削波爆音。
    wav = wav.astype(np.float32)
    wav -= float(np.mean(wav))                       # 去直流偏置，消除整体偏移导致的"闷/爆"
    target = 0.84                                    # -1.5dBFS，留 headroom
    peak = float(np.max(np.abs(wav))) or 0.0
    if peak > target:
        wav = wav * (target / peak)                  # 只压不放，避免把底噪一起放大
    # 首尾 5ms 淡入淡出，去掉开头/结尾的"咔哒"声
    n_fade = min(int(0.005 * m.tts_model.sample_rate), len(wav) // 2)
    if n_fade > 1:
        ramp = np.linspace(0.0, 1.0, n_fade, dtype=np.float32)
        wav[:n_fade] *= ramp
        wav[-n_fade:] *= ramp[::-1]
    import io
    out = io.BytesIO()
    sf.write(out, wav, samplerate=m.tts_model.sample_rate, format="WAV")
    return TTSResponse(
        audio_b64=base64.b64encode(out.getvalue()).decode("ascii"),
        sample_rate=int(m.tts_model.sample_rate),
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
