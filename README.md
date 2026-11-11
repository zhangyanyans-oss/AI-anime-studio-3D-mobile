# AI 动画工作室（AI-anime-studio-3D-mobile）

一句话：从故事创意到成片的 **AI 分镜动画工作台**——写梗概 → 生成分镜 → 逐镜生图 → 一键生成视频，全程网页操作（手机/电脑自适应）。

## 功能特性

- **剧本生成**：剧情梗概、画风圣经（Style Bible）可编辑，支持一键按最新梗概/画风重写全部分镜提示词
- **分镜工作台**：分镜卡片化编辑、拖拽排序、调度图（俯视走位/轨迹）辅助
- **AI 生图**：角色/场景/道具参考图锚定人物一致性，影子镜头自动剔除角色参考，环境/光线/影调段落式提示词
- **AI 生视频**：参考图模式 / 首尾帧模式，调度轨迹注入、场景环境锁定、出场人物封锁（禁止路人入画）
- **多版本管理**：同一分镜可生成多个视频版本，逐版本保存/删除；删除界面文件时服务器同步清理（带跨项目引用保护）
- **技术栈**：FastAPI + SQLite + 原生 JS（three.js / marked.js 前端依赖），支持 AutoDL 云端渲染

## 快速开始

```bash
pip install -r requirements.txt

# 必需的私密配置（参考 .env.example 填写，不会入库）
cp .env.example .env

python main.py
# 默认监听 http://127.0.0.1:8010，可用 PORT 环境变量改端口
```

> `.env` 中需配置访问口令与 AI 服务密钥（DeepSeek / AutoDL 等），已通过 `.gitignore` 排除，不会推送到仓库。

## 目录结构

```
main.py            # FastAPI 后端（剧本/分镜/生图/生视频/文件管理全部 API）
au_lexicon.py      # AU 词典/术语辅助
static/
  app.js           # 移动端/桌面自适应前端主逻辑
  director3d.js    # 3D 调度相关前端逻辑
  mobile.html/css  # 界面
  vendor/          # three.js、marked 等本地依赖
  style-previews/  # 画风预览图资源
fonts/             # 字体资源
```

## License

仅供学习交流使用。
