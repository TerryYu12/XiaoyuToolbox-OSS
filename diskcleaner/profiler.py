# -*- coding: utf-8 -*-
"""智能文件夹画像与归属溯源引擎 (Folder Profiler & Origin Inspector)

功能：
1. 文件夹所有者与应用识别（这是谁在用：Gradle、Chrome、Python、Windows系统等）
2. 缓存性质与技术用途（这是哪里的缓存：发行版运行时、离线静态资源、显卡着色器、依赖库等）
3. 真实项目动态溯源（如 Gradle Wrapper 具体被哪个本地项目引用、node_modules 属于哪个工程）
4. 进程占用与运行状态实时探活（如 Chrome 正在运行，提醒避免文件锁定）
5. 清理影响与重建代价评估（删了会怎样，下次启动/构建如何重建，要多久）
6. 人话清理建议（放心清理 / 建议确认 / 谨慎操作）
"""
from __future__ import annotations
import os
import re
import glob
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set, Tuple

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


@dataclass
class FolderProfile:
    owner_app: str = "未知应用"          # 所属软件或组件
    cache_type: str = "临时缓存"         # 缓存类型
    purpose: str = ""                   # 详细技术与业务用途
    recreate_cost: str = ""             # 清理后果与重新生成成本
    advice: str = ""                    # 清理建议
    insight: str = ""                   # 智能溯源高价值洞察（如关联的项目、模型名、进程占用）
    active_refs: List[str] = field(default_factory=list)  # 关联的项目路径或实体列表


# 静态画像知识库（覆盖核心系统/开发/浏览器/AI规则）
STATIC_PROFILES: Dict[str, dict] = {
    # ---- 构建与包管理工具 ----
    "gradle-dists": {
        "owner_app": "Gradle 构建工具",
        "cache_type": "Wrapper 发行版二进制运行时",
        "purpose": "各个 Android / Java 项目通过 gradlew 自动拉取并解压的完整 Gradle 发行版程序包。",
        "recreate_cost": "无损工程代码。下次对应项目首次执行 gradlew 构建时会自动从网络拉取（约 150MB，需联网 10-30 秒）。",
        "advice": "推荐清理长期不维护项目的旧版本；近期常开发的项目建议保留以防重复下载。",
    },
    "gradle-cache": {
        "owner_app": "Gradle 构建工具",
        "cache_type": "依赖库与任务构建产物缓存",
        "purpose": "存放从 Maven/Google/JCenter 下载的第三方 jar/aar 依赖包以及编译任务中间转换制品。",
        "recreate_cost": "无损源代码。清理后下次项目构建需要重新从 Maven 仓库拉取依赖，首次构建较慢。",
        "advice": "体积膨胀数十 GB 时推荐清理；构建频繁且网络较慢时可保留。",
    },
    "npm-cache": {
        "owner_app": "Node.js (npm)",
        "cache_type": "npm 全局依赖包 Tarball 归档",
        "purpose": "npm install 下载的第三方 npm 模块 .tgz 压缩包全局副本，用于二次安装时离线加速。",
        "recreate_cost": "完全无损。不影响任何已有项目运行；下次全新安装相同模块时会从 npm 镜像重新下载。",
        "advice": "放心清理。属于可安全清空的纯离线缓存。",
    },
    "npm-cache-roaming": {
        "owner_app": "Node.js (npm Roaming)",
        "cache_type": "npm 漫游包缓存",
        "purpose": "漫游用户目录下的 npm 安装包缓存备份。",
        "recreate_cost": "完全无损。下次安装自动重新拉取。",
        "advice": "放心清理。",
    },
    "pnpm-store": {
        "owner_app": "pnpm 包管理器",
        "cache_type": "全局内容寻址硬链接存储库",
        "purpose": "pnpm 在全盘跨工程共享依赖时使用的硬链接内容寻址存储库，所有 pnpm 项目共用一份文件。",
        "recreate_cost": "已安装的项目若未损坏不受影响；新安装项目或孤立依赖重新下载。",
        "advice": "建议审阅。可通过 pnpm store prune 清理未引用孤立包，整删会导致各项目需重新安装。",
    },
    "yarn-cache": {
        "owner_app": "Yarn (经典版)",
        "cache_type": "Yarn 全局依赖缓存",
        "purpose": "Yarn 包管理器在下载 npm 模块时保留的全局压缩包缓存。",
        "recreate_cost": "完全无损。下次 yarn add 或 install 时重新下载。",
        "advice": "放心清理。",
    },
    "pip-cache": {
        "owner_app": "Python (pip)",
        "cache_type": "Wheel / Sdist 安装包本地缓存",
        "purpose": "曾经通过 pip install 安装过的 Python 模块 .whl 轮子和源码包本地副本。",
        "recreate_cost": "完全无损。已安装的 Python 环境与虚拟环境丝毫不受影响；下次安装同版本包时从镜像重新下载。",
        "advice": "放心清理。特别是 Python 模块频繁更新后，旧版 wheel 纯属占位。",
    },
    "uv-cache": {
        "owner_app": "uv (高性能 Python 包管理器)",
        "cache_type": "预编译轮子与下载缓存",
        "purpose": "uv 在极速构建与安装 Python 依赖时保留的轮子、解压树与 metadata 缓存。",
        "recreate_cost": "完全无损。不影响任何正在运行的项目；下次 uv pip install 会按需高速重建。",
        "advice": "放心清理。",
    },
    "uv-cache-alt": {
        "owner_app": "uv (Roaming)",
        "cache_type": "uv 漫游缓存",
        "purpose": "uv 在 roaming 路径下的缓存残留。",
        "recreate_cost": "完全无损。",
        "advice": "放心清理。",
    },
    "cargo-cache": {
        "owner_app": "Rust / Cargo",
        "cache_type": "Crates 依赖压缩包归档",
        "purpose": "Cargo 构建 Rust 项目时从 crates.io 下载的第三方 crate 依赖包压缩归档。",
        "recreate_cost": "无损。下次 cargo build 时若缺少会自动联网重新拉取。",
        "advice": "建议清理。长时间开发的机器该目录常达数 GB。",
    },
    "cargo-src": {
        "owner_app": "Rust / Cargo",
        "cache_type": "Crates 解压源码树",
        "purpose": "已解压缩的第三方 crate 源码文件，供 rustc 编译和 IDE 语法分析跳转使用。",
        "recreate_cost": "无损。下次编译对应项目时 Cargo 会自动重新解压。",
        "advice": "建议清理。",
    },
    "go-mod-cache": {
        "owner_app": "Go 语言工具链",
        "cache_type": "Go 模块只读源码与版本归档",
        "purpose": "go build / go get 下载的第三方 Go module 源代码包（默认设为只读属性）。",
        "recreate_cost": "无损。下次构建对应 Go 项目时会自动重新下载。",
        "advice": "建议清理。清理需通过 go clean -modcache 或本工具带权限清理。",
    },
    "nuget-cache": {
        "owner_app": ".NET / NuGet",
        "cache_type": "NuGet 全局包缓存",
        "purpose": "C# / .NET 项目在 Visual Studio 或 dotnet build 时下载的第三方 NuGet 程序包。",
        "recreate_cost": "无损。下次 dotnet restore 会自动还原下载。",
        "advice": "建议清理长期不用的旧版包。",
    },
    "electron-builder-cache": {
        "owner_app": "Electron 打包工具链",
        "cache_type": "Electron 二进制发布包缓存",
        "purpose": "打包桌面应用时预先下载的各操作系统架构 Electron 预编译 runtime zip 文件。",
        "recreate_cost": "无损。下次运行 electron-builder 打包时会自动重新从 GitHub 下载。",
        "advice": "放心清理。单个包常达 100~300MB，非常占地。",
    },

    # ---- 浏览器缓存 ----
    "chrome-cache": {
        "owner_app": "Google Chrome 浏览器",
        "cache_type": "网页静态资源与离线缓存 (Disk Cache)",
        "purpose": "浏览网页时保存在本地的图片、视频、CSS/JS 脚本及 HTML 页面，用于二次打开网页时提速。",
        "recreate_cost": "完全无损。绝不删除书签、保存的密码或浏览历史；下次打开网页会重新从网络加载最新资源。",
        "advice": "放心清理。长期积累很容易吃掉数 GB，建议定期清理。",
    },
    "chrome-code-cache": {
        "owner_app": "Google Chrome 浏览器",
        "cache_type": "V8 引擎编译代码缓存 (Code Cache)",
        "purpose": "Chrome V8 引擎将常用网页 JavaScript / WebAssembly 预先编译好的字节码与机器码缓存。",
        "recreate_cost": "完全无损。下次打开复杂网页时 V8 会再次自动编译。",
        "advice": "放心清理。",
    },
    "chrome-crashpad": {
        "owner_app": "Google Chrome 浏览器",
        "cache_type": "浏览器崩溃与错误转储 (Crashpad)",
        "purpose": "Chrome 渲染进程或插件偶发崩溃时生成的诊断转储文件与统计日志。",
        "recreate_cost": "完全无损。纯历史错误诊断记录。",
        "advice": "放心清理。",
    },
    "edge-cache": {
        "owner_app": "Microsoft Edge 浏览器",
        "cache_type": "网页静态资源离线缓存",
        "purpose": "Edge 浏览网页时下载的图片、脚本、视频等网页临时缓存文件。",
        "recreate_cost": "完全无损。不影响收藏夹、密码和登录状态；下次上网时重新向服务器请求。",
        "advice": "放心清理。",
    },
    "edge-crashpad": {
        "owner_app": "Microsoft Edge 浏览器",
        "cache_type": "Edge 崩溃转储日志",
        "purpose": "Edge 进程异常退出时保存的崩溃排查文件。",
        "recreate_cost": "完全无损。",
        "advice": "放心清理。",
    },
    "firefox-cache": {
        "owner_app": "Mozilla Firefox 浏览器",
        "cache_type": "火狐离线网页缓存 (cache2)",
        "purpose": "火狐浏览器下载的网页图片和资源缓存。",
        "recreate_cost": "完全无损。不影响书签与密码。",
        "advice": "放心清理。",
    },

    # ---- Windows 系统与驱动 ----
    "win-temp": {
        "owner_app": "Windows 系统服务",
        "cache_type": "系统临时交换目录 (C:\\Windows\\Temp)",
        "purpose": "Windows Update、驱动安装程序、系统级服务在解压和安装更新时使用的临时中转空间。",
        "recreate_cost": "完全无损。安装已完成的临时残留；正在被系统进程占用的文件会自动安全跳过。",
        "advice": "放心清理。需管理员权限（以管理员运行可彻底释放）。",
    },
    "user-temp": {
        "owner_app": "各类应用程序与日常操作",
        "cache_type": "当前用户临时文件 (%TEMP%)",
        "purpose": "各类软件（解压软件、安装程序、Office、浏览器、编译器）在运行期间写入的临时文件。",
        "recreate_cost": "完全无损。已退出软件的遗留垃圾；正在运行的软件持有的句柄会自动安全跳过。",
        "advice": "放心清理。全盘最常见的垃圾重灾区，建议全选清空。",
    },
    "win-update-cache": {
        "owner_app": "Windows Update (系统更新)",
        "cache_type": "已安装系统补丁安装包残留",
        "purpose": "Windows 更新自动下载并完成安装的更新包安装源文件 (.cab/.esd/.msu)。",
        "recreate_cost": "完全无损。更新补丁早已应用到系统文件树，这些是安装完毕后未自动清理的残留包。",
        "advice": "强烈推荐清理。通常能瞬间释放 3~15 GB 系统盘宝贵空间。",
    },
    "prefetch": {
        "owner_app": "Windows 系统预取机制",
        "cache_type": "程序冷启动预取轨迹文件 (.pf)",
        "purpose": "记录常用软件启动时读取的文件顺序，用于 Windows 启动和软件首次打开时预读提速。",
        "recreate_cost": "无损。Windows 会在软件再次运行时自动为常用程序重新生成；长期积累包含大量已卸载软件的失效 pf。",
        "advice": "推荐清理。特别是频繁安装卸载软件的电脑，清理能减轻磁盘索引负担。",
    },
    "crash-dumps": {
        "owner_app": "Windows 崩溃转储服务",
        "cache_type": "程序崩溃快照 (.dmp)",
        "purpose": "应用程序发生闪退或内存访问越界时由系统保存的完整或小型内存 Dump 文件。",
        "recreate_cost": "完全无损。仅供 C++/Rust 等原生开发者使用 WinDbg 调试排查代码崩溃，普通用户毫无用处。",
        "advice": "放心清理。经常占用数 GB 内存镜像，建议立即清空。",
    },
    "win-minidump": {
        "owner_app": "Windows 蓝屏诊断服务",
        "cache_type": "蓝屏崩溃转储 (BSOD Minidump)",
        "purpose": "电脑蓝屏死机时记录的内核堆栈与驱动错误日志。",
        "recreate_cost": "完全无损。如果不准备找微软或硬件售后分析蓝屏 dmp，可直接清空。",
        "advice": "放心清理。",
    },
    "wer-reports": {
        "owner_app": "Windows Error Reporting (WER)",
        "cache_type": "系统错误报告队列与日志",
        "purpose": "Windows 系统向微软上报未响应和异常时打包的排查日志。",
        "recreate_cost": "完全无损。",
        "advice": "放心清理。",
    },
    "wer-user-reports": {
        "owner_app": "Windows Error Reporting (WER)",
        "cache_type": "用户错误报告队列",
        "purpose": "当前用户层错误报告暂存包。",
        "recreate_cost": "完全无损。",
        "advice": "放心清理。",
    },
    "thumb-cache": {
        "owner_app": "Windows 资源管理器 (Explorer)",
        "cache_type": "缩略图数据库 (thumbcache_*.db)",
        "purpose": "资源管理器为图片、视频、PDF 等文件预先渲染生成的缩略图预览小图数据库。",
        "recreate_cost": "无损。再次打开有图片的文件夹时，系统会自动后台重新生成预览小图。",
        "advice": "可清理。如果资源管理器经常卡顿或缩略图显示黑块/错乱，清理可彻底修复。",
    },
    "icon-cache": {
        "owner_app": "Windows 资源管理器 (Explorer)",
        "cache_type": "程序图标缓存数据库 (iconcache_*.db)",
        "purpose": "桌面上和开始菜单中各个应用程序图标缓存。",
        "recreate_cost": "无损。重启资源管理器或电脑时系统会自动重建最新图标。",
        "advice": "可清理。桌面图标变白块或显示错误时清理即可修复。",
    },
    "delivery-opt": {
        "owner_app": "Windows 传递优化服务",
        "cache_type": "P2P 局域网更新共享缓存",
        "purpose": "Windows Update 与 Microsoft Store 在局域网电脑之间共享下载补丁时保留的缓存。",
        "recreate_cost": "完全无损。",
        "advice": "放心清理。",
    },
    "inet-cache": {
        "owner_app": "Windows 网络组件 / IE",
        "cache_type": "系统级网络离线缓存 (INetCache)",
        "purpose": "通过系统 API 调起网络请求的应用程序或旧版 WebView 产生的临时下载文件。",
        "recreate_cost": "完全无损。",
        "advice": "放心清理。",
    },
    "d3dscache": {
        "owner_app": "DirectX / 显卡驱动",
        "cache_type": "Direct3D 着色器编译缓存",
        "purpose": "DirectX 3D 游戏或图形渲染软件编译好的 GPU 着色器二进制，用于消除游戏掉帧卡顿。",
        "recreate_cost": "无损。下次进游戏时显卡会自动重新编译，首次进入关卡可能稍有微小加载耗时。",
        "advice": "放心清理。显卡驱动更新或游戏大版本升级后清理可解决很多图形花屏与闪退异常。",
    },
    "nvidia-glcache": {
        "owner_app": "NVIDIA 显卡驱动 (GeForce / RTX)",
        "cache_type": "OpenGL 着色器缓存 (GLCache)",
        "purpose": "NVIDIA 显卡驱动为 OpenGL 应用程序编译并保留的机器码着色器缓存。",
        "recreate_cost": "无损。下次启动对应 3D 软件时驱动会自动重新编译。",
        "advice": "放心清理。",
    },
    "nvidia-dxcache": {
        "owner_app": "NVIDIA 显卡驱动 (GeForce / RTX)",
        "cache_type": "DirectX 着色器缓存 (DXCache)",
        "purpose": "NVIDIA 驱动为各类 3A 游戏预编译的 DX11/DX12 着色器指令缓存。",
        "recreate_cost": "无损。下次启动游戏时自动按需重新编译生成。",
        "advice": "放心清理。常年不清理很容易积累 4~10 GB 零碎文件。",
    },
    "amd-shader-cache": {
        "owner_app": "AMD Radeon 显卡驱动",
        "cache_type": "Radeon DX 着色器缓存",
        "purpose": "AMD 驱动编译的游戏着色器指令缓存。",
        "recreate_cost": "无损。重新进游戏会自动编译。",
        "advice": "放心清理。",
    },
    "amd-glcache": {
        "owner_app": "AMD Radeon 显卡驱动",
        "cache_type": "Radeon OpenGL 着色器缓存",
        "purpose": "AMD 驱动 OpenGL 着色器缓存。",
        "recreate_cost": "无损。",
        "advice": "放心清理。",
    },
    "recycle-bin": {
        "owner_app": "Windows 系统回收站",
        "cache_type": "软删除文件暂存空间 ($Recycle.Bin)",
        "purpose": "用户在各个磁盘中使用常规 Delete 键删除的文件集合，仍在实质占用物理磁盘空间。",
        "recreate_cost": "⚠️ 不可逆操作！清空回收站后里面的文件将无法通过常规途径还原，需专业恢复软件。",
        "advice": "建议确认。请确认回收站内没有误删的个人重要文档后再执行清空。",
    },
    "font-cache": {
        "owner_app": "Windows 字体驱动子系统",
        "cache_type": "系统字体渲染字体字形缓存",
        "purpose": "加速屏幕字体文字渲染的字形栅格化预渲染位图缓存。",
        "recreate_cost": "无损。系统会自动重新生成。",
        "advice": "可清理。",
    },

    # ---- AI 研发与大模型工具 ----
    "hf-cache": {
        "owner_app": "HuggingFace (AI 模型库)",
        "cache_type": "开源大模型与扩散模型权重缓存 (Hub)",
        "purpose": "通过 transformers、diffusers 等 Python 库自动从 HuggingFace 镜像下载的模型本体 (safetensors/bin/tokenizer)。",
        "recreate_cost": "代价昂贵！单个模型权重体积通常在 3GB 至 30GB 不等，清理后若代码再次运行需重新耗时下载大量流量。",
        "advice": "谨慎操作！请在智能详情中确认具体的模型 ID，只有确定不再使用的模型才建议删除。",
    },
    "torch-hub": {
        "owner_app": "PyTorch (Torch Hub)",
        "cache_type": "经典预训练模型 Checkpoint 权重",
        "purpose": "torch.hub.load() 自动下载的计算机视觉（ResNet/YOLO）或 NLP 预训练模型权重文件。",
        "recreate_cost": "无损工程代码。下次运行对应推理脚本时若网络畅通会自动从 GitHub/PyTorch 重新拉取。",
        "advice": "如果当前不跑对应历史脚本，可清理释放空间。",
    },
    "whisper-models": {
        "owner_app": "OpenAI Whisper 语音模型",
        "cache_type": "语音转文字模型权重 (.pt)",
        "purpose": "Whisper 自动转写语音时下载到本地的 base/small/medium/large 语音模型权重。",
        "recreate_cost": "无损。下次执行 whisper 转写脚本时会自动重新从云端下载。",
        "advice": "长期不用语音转写功能可清理。",
    },
    "comfy-cache": {
        "owner_app": "ComfyUI 节点管理器",
        "cache_type": "节点插件与下载临时包",
        "purpose": "ComfyUI Manager 安装自定义节点与模型时下载的暂存包。",
        "recreate_cost": "无损。已安装的节点组件不受影响。",
        "advice": "放心清理。",
    },
    "ollama-models": {
        "owner_app": "Ollama 本地大模型",
        "cache_type": "GGUF 语言模型 Blob 存储",
        "purpose": "通过 ollama run/pull 下载保存在本机进行本地离线推理的大语言模型文件。",
        "recreate_cost": "代价高昂！模型往往达数 GB，删掉后如果还需要需重新 ollama pull。",
        "advice": "谨慎操作！建议优先在终端中使用 `ollama rm <模型名>` 精确卸载不用的模型，避免整块清空。",
    },
    "playwright-browsers": {
        "owner_app": "Playwright 自动化测试",
        "cache_type": "专用无头浏览器二进制环境",
        "purpose": "Python/Node.js 跑自动化爬虫与端到端测试时所依赖的独立 Chromium、Firefox 与 WebKit 完整内核。",
        "recreate_cost": "完全无损普通上网。下次运行自动化测试脚本时若缺失，可通过 playwright install 一键重新下载。",
        "advice": "推荐清理。通常包含 2~3 个完整浏览器内核，体积常达 2~5 GB，不写测试用例时很占空间。",
    },
    "puppeteer-browsers": {
        "owner_app": "Puppeteer 爬虫与自动化",
        "cache_type": "Puppeteer Chrome/Chromium 二进制包",
        "purpose": "Node.js Puppeteer 脚本下载的专用 Chrome 浏览器测试环境。",
        "recreate_cost": "无损。下次安装或运行时可重新下载。",
        "advice": "推荐清理。",
    },
}


class OriginInspector:
    """智能归属与上下文探针聚合器"""

    def __init__(self):
        self._gradle_mapping: Optional[Dict[str, List[str]]] = None
        self._running_procs: Optional[Set[str]] = None
        self._project_name_cache: Dict[str, Tuple[str, str]] = {}

    def get_running_processes(self) -> Set[str]:
        """获取当前正在运行的进程名小写集合（进程级缓存，避免重复遍历）"""
        if self._running_procs is not None:
            return self._running_procs
        procs = set()
        if _HAS_PSUTIL:
            try:
                for p in psutil.process_iter(["name"]):
                    name = p.info.get("name")
                    if name:
                        procs.add(name.lower())
            except Exception:
                pass
        self._running_procs = procs
        return procs

    def get_gradle_projects_mapping(self) -> Dict[str, List[str]]:
        """快速解析本机 Gradle 发行版与工程的对应关系（通过 daemon 构建日志 + 工作区扫描）"""
        if self._gradle_mapping is not None:
            return self._gradle_mapping
        mapping: Dict[str, Set[str]] = {}
        # 1. 解析 ~/.gradle/daemon 日志（最精准：记录了近期谁真正在跑这个 daemon）
        daemon_base = os.path.expanduser("~/.gradle/daemon")
        if os.path.isdir(daemon_base):
            try:
                for v in os.listdir(daemon_base):
                    v_dir = os.path.join(daemon_base, v)
                    if not os.path.isdir(v_dir):
                        continue
                    # 规范版本号：如 8.14.4 -> 8.14.4
                    log_files = glob.glob(os.path.join(v_dir, "*.log"))[:8]
                    for lf in log_files:
                        try:
                            with open(lf, "r", encoding="utf-8", errors="ignore") as f:
                                for line in f:
                                    if "currentDir=" in line:
                                        m = re.search(r"currentDir=([^,\}\r\n]+)", line)
                                        if m:
                                            p = m.group(1).strip()
                                            if os.path.exists(p):
                                                mapping.setdefault(v, set()).add(p)
                                            break
                        except Exception:
                            pass
            except Exception:
                pass

        # 2. 检查常见工作区内已声明该版本的 gradle-wrapper.properties
        # 重点检查 F:\AI-hub\workroom、用户目录
        candidate_roots = [
            r"F:\AI-hub\workroom",
            os.path.expanduser("~"),
        ]
        for c_root in candidate_roots:
            if not os.path.isdir(c_root):
                continue
            # 浅层遍历常用工程根
            try:
                for root, dirs, files in os.walk(c_root):
                    # 避开深度目录
                    dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "venv", "__pycache__", "AppData")]
                    if "gradle-wrapper.properties" in files:
                        wp = os.path.join(root, "gradle-wrapper.properties")
                        try:
                            with open(wp, "r", encoding="utf-8", errors="ignore") as wf:
                                content = wf.read()
                                vm = re.search(r"gradle-([0-9]+\.[0-9]+(?:\.[0-9]+)?(?:-[a-zA-Z0-9]+)?)-(?:all|bin)\.zip", content)
                                if vm:
                                    ver = vm.group(1)
                                    # 提取对应项目根目录（wrapper 通常在 /gradle/wrapper 或 /android/gradle/wrapper）
                                    proj_dir = os.path.dirname(os.path.dirname(root))
                                    mapping.setdefault(ver, set()).add(proj_dir)
                        except Exception:
                            pass
                    # 控制深度，避免全盘搜太久
                    rel = os.path.relpath(root, c_root)
                    if rel.count(os.sep) >= 4:
                        dirs.clear()
            except Exception:
                pass

        self._gradle_mapping = {k: sorted(list(v)) for k, v in mapping.items()}
        return self._gradle_mapping

    def probe_project_owner(self, path: str) -> Optional[Tuple[str, str]]:
        """向上追溯目录归属的本地工程项目（如 node_modules / build / target 属于哪个工程）"""
        cur = os.path.abspath(path)
        # 往上追查最多 4 级
        for _ in range(4):
            cur = os.path.dirname(cur)
            if not cur or cur == os.path.dirname(cur):
                break
            if cur in self._project_name_cache:
                return self._project_name_cache[cur]
            # 检查常见工程标志文件
            pkg_json = os.path.join(cur, "package.json")
            if os.path.isfile(pkg_json):
                try:
                    with open(pkg_json, "r", encoding="utf-8", errors="ignore") as f:
                        m = re.search(r'"name"\s*:\s*"([^"]+)"', f.read()[:1000])
                        pname = m.group(1) if m else os.path.basename(cur)
                        res = (f"前端工程: {pname}", cur)
                        self._project_name_cache[cur] = res
                        return res
                except Exception:
                    pass
            cargo_toml = os.path.join(cur, "Cargo.toml")
            if os.path.isfile(cargo_toml):
                try:
                    with open(cargo_toml, "r", encoding="utf-8", errors="ignore") as f:
                        m = re.search(r'name\s*=\s*"([^"]+)"', f.read()[:500])
                        pname = m.group(1) if m else os.path.basename(cur)
                        res = (f"Rust 工程: {pname}", cur)
                        self._project_name_cache[cur] = res
                        return res
                except Exception:
                    pass
            settings_gradle = os.path.join(cur, "settings.gradle")
            if not os.path.exists(settings_gradle):
                settings_gradle = os.path.join(cur, "settings.gradle.kts")
            if os.path.isfile(settings_gradle):
                try:
                    with open(settings_gradle, "r", encoding="utf-8", errors="ignore") as f:
                        m = re.search(r'rootProject\.name\s*=\s*[\'"]([^\'"]+)[\'"]', f.read()[:500])
                        pname = m.group(1) if m else os.path.basename(cur)
                        res = (f"Gradle 工程: {pname}", cur)
                        self._project_name_cache[cur] = res
                        return res
                except Exception:
                    pass
        return None

    def profile_candidate(self, cand_path: str, rule_id: str, title: str, description: str) -> FolderProfile:
        """为候选扫描项生成完整的智能画像"""
        np = cand_path.replace("\\", "/").lower()
        base_tmpl = STATIC_PROFILES.get(rule_id, {})
        prof = FolderProfile()

        # 1. 基础画像填充（来自静态知识库）
        if base_tmpl:
            prof.owner_app = base_tmpl.get("owner_app", prof.owner_app)
            prof.cache_type = base_tmpl.get("cache_type", prof.cache_type)
            prof.purpose = base_tmpl.get("purpose", "")
            prof.recreate_cost = base_tmpl.get("recreate_cost", "")
            prof.advice = base_tmpl.get("advice", "")
        else:
            # Winapp2 规则识别
            if rule_id.startswith("wa2-") or "winapp2" in description.lower():
                # 尝试提取 winapp2 标题里的软件名
                clean_title = title.replace("[", "").replace("]", "").strip()
                prof.owner_app = clean_title.split("-")[0].strip() if "-" in clean_title else clean_title
                prof.cache_type = "第三方应用程序缓存/日志"
                prof.purpose = f"由 {prof.owner_app} 在日常运行中产生的临时缓存文件、历史日志或备份数据。"
                prof.recreate_cost = "完全无损。下次启动该软件时会自动重新生成基础运行文件。"
                prof.advice = "安全清理。属于 Winapp2 社区规则收录的无害清理项。"
            else:
                # 动态路径特征推断
                self._infer_by_path(cand_path, prof)

        # 2. 动态智能探针（项目关联与上下文挖掘）
        # A. Gradle 发行版与项目真实归属关联
        if "gradle" in rule_id and ("dists" in np or "wrapper" in np):
            # 提取版本号（如 8.14.4、8.10.2、8.14.3）
            vm = re.search(r"gradle-([0-9]+\.[0-9]+(?:\.[0-9]+)?)", cand_path)
            if vm:
                ver = vm.group(1)
                mapping = self.get_gradle_projects_mapping()
                # 寻找匹配的项目（可能是 8.14.4 或 8.14.4-bin）
                matched_projs = []
                for k, projs in mapping.items():
                    if ver in k or k in ver:
                        matched_projs.extend(projs)
                if matched_projs:
                    prof.active_refs = matched_projs
                    proj_names = [os.path.basename(p) for p in matched_projs]
                    summary_str = ", ".join(proj_names[:3])
                    prof.insight = f"📌 正被本机项目使用: {summary_str} ({matched_projs[0]})"
                    prof.advice = f"若近期不维护 {summary_str}，可安全清理释放空间；下次项目构建时会自动重新下载。"
                else:
                    prof.insight = f"⚠️ 未检测到近期活跃项目引用此版本 (属于历史构建残留或已删除项目的弃用版本)"
                    prof.advice = "强烈建议清理：本地无活跃工程引用此版本 Gradle，白白占用磁盘。"

        # B. 本地工程项目归属（node_modules、build、dist 等）
        if any(x in np for x in ["node_modules", "/target/", "/dist/", "/build/", "/.next/"]):
            proj_info = self.probe_project_owner(cand_path)
            if proj_info:
                pname, proot = proj_info
                prof.owner_app = pname
                prof.insight = f"📂 归属工程路径: {proot}"
                if "node_modules" in np:
                    prof.cache_type = "前端依赖包库 (node_modules)"
                    prof.purpose = f"项目 {pname} 所依赖的全部 npm 模块目录。"
                    prof.recreate_cost = "可通过在该项目根目录下执行 `npm install` 完整复原。"
                    prof.advice = "非活跃开发项目可删除以腾出数万碎片小文件；活跃项目建议保留以避免重复安装。"

        # C. HuggingFace 模型名称探针
        if "huggingface" in rule_id or "huggingface/hub" in np:
            if os.path.isdir(cand_path):
                models_found = []
                try:
                    for item in os.listdir(cand_path):
                        if item.startswith("models--"):
                            mname = item.replace("models--", "").replace("--", "/")
                            models_found.append(mname)
                except Exception:
                    pass
                if models_found:
                    prof.active_refs = models_found
                    prof.insight = f"🤖 包含大模型权重: {', '.join(models_found[:3])}"
                    prof.advice = "谨慎操作！包含上述大模型权重文件，确认代码不再使用这些模型时再勾选清理。"

        # 3. 进程占用探针（检测关联软件是否正在运行）
        running = self.get_running_processes()
        running_warn = self._check_process_running(prof.owner_app, np, running)
        if running_warn:
            if prof.insight:
                prof.insight += " · " + running_warn
            else:
                prof.insight = running_warn

        return prof

    def _infer_by_path(self, path: str, prof: FolderProfile):
        """当没有匹配的静态规则时，根据路径结构做智能推测"""
        norm_p = path.replace("\\", "/").lower()
        if "appdata/local/temp" in norm_p or "windows/temp" in norm_p:
            prof.owner_app = "Windows 临时系统"
            prof.cache_type = "运行临时中转文件"
            prof.purpose = "程序运行时临时创建的临时交换数据或解压缓存。"
            prof.recreate_cost = "完全无损。"
            prof.advice = "放心清理。"
        elif "appdata/local/crashdumps" in norm_p:
            prof.owner_app = "Windows 错误转储"
            prof.cache_type = "程序崩溃转储"
            prof.purpose = "程序闪退时由系统保存的内存排查快照。"
            prof.recreate_cost = "完全无损。"
            prof.advice = "放心清理。"
        elif "wechat" in norm_p or "weixin" in norm_p:
            prof.owner_app = "微信客户端 (WeChat)"
            prof.cache_type = "聊天媒体与小程序缓存"
            prof.purpose = "微信朋友圈小视频、浏览网页临时缓存及接收的临时文件。"
            prof.recreate_cost = "不包含文字聊天记录；再次查看图片/小视频时从网络拉取。"
            prof.advice = "建议确认后再清理。"
        elif "tencent" in norm_p or "qq" in norm_p:
            prof.owner_app = "腾讯 QQ"
            prof.cache_type = "QQ 临时缓存"
            prof.purpose = "QQ 客户端运行缓存与表情包临时文件。"
            prof.recreate_cost = "无损。"
            prof.advice = "可清理。"
        elif "jetbrains" in norm_p or "pycharm" in norm_p or "idea" in norm_p:
            prof.owner_app = "JetBrains IDE"
            prof.cache_type = "IDE 索引与系统缓存"
            prof.purpose = "IDEA / PyCharm 建立的代码符号索引与本地历史记录缓存。"
            prof.recreate_cost = "无损工程。下次打开工程时 IDE 会自动后台重新建索引。"
            prof.advice = "建议在关闭 IDE 后清理。"
        elif "vscode" in norm_p or "code" in norm_p:
            prof.owner_app = "Visual Studio Code"
            prof.cache_type = "编辑器缓存与扩展日志"
            prof.purpose = "VS Code 运行日志、插件下载缓存与已编译缓存。"
            prof.recreate_cost = "无损代码与设置。"
            prof.advice = "放心清理。"
        else:
            # 提取最后两段有意义的目录名作为应用参考
            parts = [p for p in path.replace("/", "\\").split("\\") if p]
            if len(parts) >= 2:
                prof.owner_app = parts[-2]
                prof.cache_type = "应用程序数据/缓存"
                prof.purpose = f"位于 {parts[-2]} 目录下的文件或缓存项。"
                prof.recreate_cost = "视具体应用而定。"
                prof.advice = "建议核实该文件夹用途后再做清理。"

    def _check_process_running(self, owner_app: str, norm_path: str, running: Set[str]) -> Optional[str]:
        """检测所属软件是否当前正在运行"""
        owner_lower = owner_app.lower()
        if "chrome" in owner_lower or "google" in owner_lower or "chrome" in norm_path:
            if "chrome.exe" in running:
                return "⚠️ 软件正在运行中 (Google Chrome 正在运行，清理可能遇文件锁定，建议关闭浏览器后清理)"
        if "edge" in owner_lower or "msedge" in norm_path:
            if "msedge.exe" in running:
                return "⚠️ 软件正在运行中 (Microsoft Edge 正在运行，建议关闭浏览器后清理)"
        if "firefox" in owner_lower or "firefox" in norm_path:
            if "firefox.exe" in running:
                return "⚠️ 软件正在运行中 (Firefox 正在运行，建议退出后清理)"
        if "wechat" in owner_lower or "微信" in owner_app or "wechat" in norm_path:
            if "wechat.exe" in running or "weixin.exe" in running:
                return "⚠️ 软件正在运行中 (微信客户端正在运行，建议退出微信后清理)"
        if "code" in owner_lower or "vscode" in norm_path:
            if "code.exe" in running:
                return "⚠️ VS Code 正在运行中 (建议保存工程后清理)"
        if "idea" in owner_lower or "pycharm" in owner_lower:
            if any(x in running for x in ["idea64.exe", "pycharm64.exe"]):
                return "⚠️ JetBrains IDE 正在运行中"
        return None


# 单例全局探针实例
_INSPECTOR = OriginInspector()


def enrich_candidate(cand, inspector: Optional[OriginInspector] = None):
    """就地丰富单个 Candidate 对象的智能画像与溯源属性"""
    insp = inspector or _INSPECTOR
    rule_id = getattr(cand, "rule_id", "")
    title = getattr(cand, "title", "")
    desc = getattr(cand, "description", "")
    path = getattr(cand, "path", "")

    profile = insp.profile_candidate(path, rule_id, title, desc)

    # 注入到 Candidate
    cand.owner_app = profile.owner_app
    cand.cache_type = profile.cache_type
    cand.purpose = profile.purpose
    cand.recreate_cost = profile.recreate_cost
    cand.advice = profile.advice
    cand.insight = profile.insight
    cand.active_refs = profile.active_refs
    return cand


def enrich_candidates(cands: list) -> list:
    """批量高效丰富候选清单（复用进程与工作区探针结果）"""
    insp = OriginInspector()
    # 提前预热进程与 gradle 映射
    insp.get_running_processes()
    insp.get_gradle_projects_mapping()
    for c in cands:
        enrich_candidate(c, inspector=insp)
    return cands
