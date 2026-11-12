"""
云端一体化启动入口 —— 发布平台以 `python launch.py` 拉起。

职责：
1. 检查配音依赖（torch/torchaudio/voxcpm/soundfile/librosa），缺什么装什么
   （torch 用 CPU 版索引，避免拉 2GB+ 的 CUDA 包）
2. 后台启动 voice_server.py（配音微服务，端口 8021；
   模型权重由 voice_server 自行下载/预热，不阻塞主服务）
3. 前台运行 main.py（工作台主服务，立即就绪）
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)


def log(msg: str):
    print(f"[launch] {msg}", flush=True)


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _pip(*args: str):
    cmd = [sys.executable, "-m", "pip", "install", "--no-input", *args]
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        log(f"pip 退出码 {r.returncode}（继续尝试启动，失败模块会在日志里暴露）")


def ensure_voice_deps():
    # 1) torch / torchaudio：CPU 版（顺序很重要：先装 torch，voxcpm 依赖检测到就不再拉 CUDA 版）
    if not (_has("torch") and _has("torchaudio")):
        log("安装 PyTorch CPU 版（约 200MB，首次 2-5 分钟）...")
        _pip("torch", "torchaudio", "--index-url", "https://download.pytorch.org/whl/cpu")
    # 2) voxcpm 本体（PyPI 包，含 transformers 等依赖）
    if not _has("voxcpm"):
        log("安装 voxcpm...")
        _pip("voxcpm")
    # 3) 音频解码
    for m in ("soundfile", "librosa"):
        if not _has(m):
            log(f"安装 {m}...")
            _pip(m)


def _voice_python():
    """优先复用已装好 voxcpm 的解释器：VOICE_PYTHON → 邻近 VoxCPM venv → 当前解释器。
    本地开发机上 VoxCPM venv 里依赖齐全，直接复用可省掉一次 200MB+ 的 pip 安装。"""
    env_py = (os.getenv("VOICE_PYTHON") or "").strip()
    if env_py and os.path.isfile(env_py):
        return env_py
    for rel in (("..", "VoxCPM-main", "venv", "Scripts", "python.exe"),
                ("..", "VoxCPM-main", "venv", "bin", "python"),
                ("venv", "Scripts", "python.exe"),
                ("venv", "bin", "python")):
        p = os.path.normpath(os.path.join(BASE, *rel))
        if os.path.isfile(p):
            return p
    return sys.executable


def main():
    # 配音服务：后台子进程，装依赖 → 启动；权重下载/预热在其内部异步进行
    voice_py = _voice_python()

    def _start_voice():
        try:
            if voice_py == sys.executable:
                ensure_voice_deps()
            else:
                log(f"复用已装好的语音解释器：{voice_py}")
            log("启动 voice_server.py（配音微服务 :8021，日志 → voice_server.log）...")
            logf = open(os.path.join(BASE, "voice_server.log"), "ab", buffering=0)
            subprocess.Popen(
                [voice_py, "-u", "voice_server.py"],
                cwd=BASE,
                stdout=logf,
                stderr=subprocess.STDOUT,
                env={**os.environ, "VOXCPM_PORT": os.environ.get("VOXCPM_PORT", "8021")},
            )
        except Exception as e:
            log(f"配音服务启动失败（工作台其余功能不受影响）: {type(e).__name__}: {e}")

    import threading
    threading.Thread(target=_start_voice, daemon=True).start()

    # 主服务：前台运行（部署平台按本进程的退出码判断存活）
    # VOICE_AUTOSTART=0：配音由本脚本负责，别让 main.py 再拉一个
    log("启动 main.py（工作台主服务）...")
    code = subprocess.call([sys.executable, "-u", "main.py"], cwd=BASE,
                           env={**os.environ, "VOICE_AUTOSTART": "0"})
    log(f"main.py 退出，code={code}")
    sys.exit(code)


if __name__ == "__main__":
    main()
