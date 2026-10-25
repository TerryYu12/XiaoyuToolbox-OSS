# 小宇工具箱开源版（XiaoyuToolbox-OSS）

一个 Windows 桌面实用工具合集：划词翻译、OCR、剪贴板历史、磁盘清理、Clash 节点检测、AI 资讯与 Epic 喜加一，pywebview 原生窗口 + 本地服务 + 托盘常驻，绿色便携。

## 功能一览

- **划词翻译**：全局热键取词 / 框选 OCR / 弹窗即译；云端多源接力（Google → 百度 → MyMemory），或自配任意 OpenAI 兼容 API（OpenAI / DeepSeek 等）。
- **OCR 识别**：Windows.Media.Ocr 原生投影，无第三方在线服务。
- **剪贴板历史**：后台轮询记录最近 100 条文本，快捷键唤起、一键写回。
- **磁盘清理**：winapp2 社区规则驱动的靶向扫描、风险分级与回收站安全清理。
- **节点检测**：专为 Clash Verge Rev 深度适配——自动寻径内核、隔离实例逐节点探测出口 IP 质量与风险画像、分级写回 Merge.yaml（先备份、必确认、写前内核校验）。
- **AI 资讯**：多源 RSS 实时快讯、模型天梯榜、模型对比与每日简报。
- **Epic 喜加一**：每周免费游戏检测与半自动领取。
- **OpenAI 专区**：Codex Desktop 多账号登录态切换与冷重启保护。

## 环境要求

- Windows 10 19045+ / Windows 11
- Python 3.11（推荐 uv 管理的 standalone Python）
- Node.js（仅 `node --check` 语法校验与前端开发用，运行不需要）

## 从源码运行

```bat
git clone <本仓库> && cd XiaoyuToolbox-OSS
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python toolbox_main.py
```

启动后托盘常驻，WebUI 与本地服务在 `http://127.0.0.1:8790`。

## 打包为 onefile exe

```bat
set TCL_LIBRARY=<你的 Python>\tcl\tcl8.6
set TK_LIBRARY=<你的 Python>\tcl\tk8.6
.venv\Scripts\pyinstaller XiaoyuToolbox-OSS.spec --noconfirm
```

> `TCL_LIBRARY` / `TK_LIBRARY` 必须设置（tkinter 的 tcl 布局坑，不设则 exe 启动即 `ModuleNotFoundError: tkinter`），产物在 `dist\XiaoyuToolbox-OSS.exe`。

## 运行数据

全部运行数据（配置、缓存）落在 exe 同目录，绿色便携；外观配置例外存于 `%LOCALAPPDATA%\XiaoyuToolbox\appearance.json`。凭据类配置（如翻译 API Key）只存在本机 `config.json`，不进仓库。

## 节点检测说明

节点检测**专为 [Clash Verge Rev](https://github.com/clash-verge-rev/clash-verge-rev) 深度适配**：自动推导顺序为「正在运行的 `verge-mihomo.exe` 进程 → Verge UI 进程安装目录内的内核副本 → 常规安装路径」。使用服务模式（clash-verge-service）时其内核目录受 ACL 保护、不可读，会自动跳过；找不到内核时在设置里手填路径即可。检测本身只读；向 `Merge.yaml` 写回分级结果时先做时间戳备份并通过 `mihomo -t` 校验。

## 开源依赖与致谢

| 项目 | 用途 |
|---|---|
| [pywebview](https://github.com/r0x0r/pywebview) | 原生窗口宿主（WebView2） |
| [PyInstaller](https://pyinstaller.org) | onefile 打包 |
| [psutil](https://github.com/giampaolo/psutil) | 进程嗅探、系统监控 |
| [PyYAML](https://pyyaml.org) | Clash 配置解析 |
| [Pillow](https://python-pillow.org) | 图像处理 |
| [send2trash](https://github.com/arsenetar/send2trash) | 回收站安全删除 |
| [zstandard](https://github.com/indygreg/python-zstandard) | DSH 会话文件解压 |
| [winsdk](https://github.com/doloopwhile/pywinrt)（WinRT 投影） | Windows.Media.Ocr 原生 OCR |
| [winapp2](https://github.com/Mosca1337/Winapp2) 社区规则库 | 磁盘清理规则（运行时按需下载） |
| [Clash Verge Rev](https://github.com/clash-verge-rev/clash-verge-rev) | 节点检测的适配对象（非运行依赖） |

完整依赖版本见 [requirements.txt](requirements.txt)。

## 许可证

[MIT](LICENSE) © 2026 TerryYu12
