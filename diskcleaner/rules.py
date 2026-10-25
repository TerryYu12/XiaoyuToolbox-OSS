# -*- coding: utf-8 -*-
"""规则引擎：把路径分类成「可清理项」，带风险分级。

设计借鉴：
- WindowsCleaner: 系统垃圾位置清单
- oh-my-disk-cleaner: 平台规则 + 风险分级
- 本机实战: ComfyUI/AE/优酷/项目产物等真实垃圾位置

风险分级：
- safe    确定是垃圾（缓存/临时文件/回收站），删了无损失
- review  很可能是垃圾（旧构建产物/旧版本安装包/过期备份），建议看一眼
- caution 谨慎（可能还有用），默认不勾选
"""
from __future__ import annotations
import os
import re
import fnmatch
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

SEP_RE = re.compile(r"[/\\]+")


def norm(p: str) -> str:
    """归一化路径：统一分隔符、去尾分隔符、Windows 下小写。"""
    p = SEP_RE.sub("/", p.strip())
    if os.name == "nt":
        p = p.lower()
    return p.rstrip("/")


def glob_to_regex(pattern: str) -> re.Pattern:
    """glob -> regex。

    语义：
    - `**/x`  匹配任意深度下的 x（含零层，即 x 本身在根）
    - `x/**`  匹配 x 自身及其所有子孙（目录规则的关键语义）
    - `*`     段内通配（不跨 /）
    - `?`     单字符（不跨 /）
    大小写按平台（Windows 不敏感）。
    """
    pattern = norm(pattern)
    trailing_dstar = pattern.endswith("/**")
    if trailing_dstar:
        pattern = pattern[:-3]
    i = 0
    out = []
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                # ** 跨段：(.*/)?  要求段边界，避免 mywindows 误配 windows
                out.append("(.*/)?")
                i += 2
                if i < len(pattern) and pattern[i] == "/":
                    i += 1  # 吃掉后面的 /
                continue
            else:
                out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c in ".+^${}()|[]":
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    tail = "(/.*)?$" if trailing_dstar else "$"
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile("^" + "".join(out) + tail, flags)


# ============================================================
# 盘符解耦：占位符 + 盘符作用域
# ============================================================
def _sysdrive_letter() -> str:
    """真实系统盘符（小写，不含冒号）。绝大多数是 c，但不写死。"""
    sd = os.environ.get("SystemDrive", "C:")
    return (sd[0] if sd else "c").lower()


def expand_placeholders(pattern: str) -> str:
    """展开规则模式里的占位符：
    - {sys}  -> 系统盘符（如 c:）
    - {users}-> 用户目录根（如 c:/users）
    让系统级规则不写死盘符，换机器/换系统盘也能用。
    """
    sys_letter = _sysdrive_letter()
    return (pattern
            .replace("{users}", f"{sys_letter}:/users")
            .replace("{sys}", f"{sys_letter}:"))


_DRIVE_RE = re.compile(r"^([a-z]):/")


def rule_drive(pattern: str) -> Optional[str]:
    """解析规则的盘符作用域：
    - '?'    : 任意单盘符（如 ?:/\\$recycle.bin，每盘都有回收站）
    - 'c'等  : 绑定具体盘符（如 c:/windows、f:/temp flies）
    - None   : 盘符无关（**/node_modules、相对路径），任何盘都适用
    """
    p = norm(expand_placeholders(pattern))
    if p.startswith("?:/"):
        return "?"
    m = re.match(r"^([a-z]):(?:/|$)", p)
    if m:
        return m.group(1)
    return None


@dataclass
class Rule:
    id: str
    category: str            # system / browser / dev / ai / app / project / media / backup / other
    risk: str                # safe / review / caution
    title: str               # 人话名称
    pattern: str             # glob，匹配完整路径（norm 后）
    description: str = ""
    match_dir: bool = True   # 是否匹配目录
    match_file: bool = True  # 是否匹配文件
    min_age_days: int = 0    # >0 时仅匹配修改时间超过 N 天的
    allow_in_protected: bool = False   # True=即使在保护区内也允许命中（如 Chrome 缓存）
    list_children_only: bool = False   # True=目录自身不作为候选，只列其内部子项（temp/npm-cache 这类整根易被占用）
    needs_admin: bool = False          # True=需要管理员权限才能删（非管理员环境不进候选，避免整批 WinError 5）
    exclude_patterns: list = field(default_factory=list)  # glob 排除模式（winapp2 ExcludeKey 用）
    _re: Optional[re.Pattern] = field(default=None, repr=False)
    _exclude_res: list = field(default=None, repr=False)
    _prefix: str = field(default="", repr=False)      # 字面量前缀（快速预筛）
    _token: str = field(default="", repr=False)       # 必含片段（快速预筛）

    def compile(self):
        if self._re is None:
            expanded = expand_placeholders(self.pattern)
            self._re = glob_to_regex(expanded)
            self._prefix, self._token = _extract_prefilter(expanded)
            if self.exclude_patterns:
                self._exclude_res = [glob_to_regex(expand_placeholders(x))
                                     for x in self.exclude_patterns]
        return self._re

    @property
    def drive(self) -> Optional[str]:
        """规则盘符作用域（懒计算缓存）。'?'=任意单盘，None=盘符无关。"""
        if not hasattr(self, "_drive"):
            object.__setattr__(self, "_drive", rule_drive(self.pattern))
        return self._drive

    def matches(self, path: str, is_dir: bool, mtime: float = 0.0,
                now: float = 0.0) -> bool:
        """path 须为已 norm 的路径。now 用于 min_age 判断（批量调用时传入避免重复取时间）。"""
        if is_dir and not self.match_dir:
            return False
        if not is_dir and not self.match_file:
            return False
        if self.min_age_days > 0 and mtime > 0:
            base = now or time.time()
            if (base - mtime) / 86400 < self.min_age_days:
                return False
        rx = self.compile()
        # 快速预筛：字面量前缀 / 必含片段，先短路掉绝大多数不相关路径
        if self._prefix and not path.startswith(self._prefix):
            return False
        if self._token and self._token not in path:
            return False
        if not rx.match(path):
            return False
        if self._exclude_res:
            for erx in self._exclude_res:
                if erx.match(path):
                    return False
        return True


# ============================================================
# 保护区：这些子树内的路径一律不进候选（除 allow_in_protected 规则）
#
# 教训（2026-10-02 C 盘清理事故）：
# `**/__pycache__/**` 这类盘符无关规则命中了 Program Files\NVIDIA Nsight、
# uv 管理的 Python 解释器、应用内置运行时内部的字节码缓存 —— 都是
# "删了能跑但不该碰"的程序自身组成部分。清理工具只该清"缓存/临时物"，
# 不该进已安装软件和运行时的地盘。
# ============================================================
PROTECTED_ZONES = [
    # 系统目录：只有明确列出的系统垃圾规则（win-temp/prefetch 等）可以进
    "{sys}/windows/**",
    "{sys}/program files/**",
    "{sys}/program files (x86)/**",
    "{sys}/programdata/**",
    # Python 运行时与 uv 工具环境：删它们的 __pycache__ 无收益还拖慢启动
    "{users}/*/appdata/roaming/uv/python/**",
    "{users}/*/appdata/roaming/uv/tools/**",
    "{users}/*/appdata/local/python/**",
    "{users}/*/.workbuddy/**",
    # AI 编码工具自身的运行时/插件（node_modules、内置依赖不是垃圾）
    "{users}/*/.zcode/**",
    "{users}/*/.codex/**",
    "{users}/*/.cache/codex-runtimes/codex-primary-runtime/**",
    "{users}/*/appdata/local/openai/codex/**",
    # AppData 下的 node_modules 全是应用自带依赖（用户项目的 node_modules
    # 在普通工作目录，不受影响）；roaming/npm 是全局安装的命令行工具
    "{users}/*/appdata/**/node_modules/**",
    "{users}/*/appdata/roaming/npm/**",
    "{users}/*/.config/mimocode/**",
    # 浏览器数据目录：书签/密码/扩展住这里，只有 cache 类规则可以进
    "{users}/*/appdata/local/google/chrome/user data/**",
    "{users}/*/appdata/local/microsoft/edge/user data/**",
    "{users}/*/appdata/local/mozilla/firefox/**",
    # Windows 自身的应用数据（缩略图缓存/inetcache/错误报告等规则可以进）
    "{users}/*/appdata/local/microsoft/windows/**",
    # PyInstaller onefile 运行时解压目录（工具箱集成后，Temp 下的 _MEIxxxxx
    # 是正在运行的程序本体——删了当场拆自己。见 docs/改善方案 P0-1）
    "{users}/*/appdata/local/temp/_mei*/**",
    "{users}/*/appdata/local/temp/pyinstaller-*/**",
]

_PROTECTED_RES: list = []


def _protected_res() -> list:
    """编译保护区模式（懒加载，进程内缓存）。"""
    global _PROTECTED_RES
    if not _PROTECTED_RES:
        _PROTECTED_RES = [glob_to_regex(expand_placeholders(z)) for z in PROTECTED_ZONES]
    return _PROTECTED_RES


def in_protected_zone(path: str) -> bool:
    """path（原始路径）是否落在保护区内。"""
    np = norm(path)
    return any(rx.match(np) for rx in _protected_res())


# ============================================================
# 通用规则库（跨机器适用）
# ============================================================
COMMON_RULES = [
    # ---- Windows 系统垃圾 ----
    # needs_admin=True 的位置实测普通用户删除会整批 WinError 5（见 2026-10-02
    # 清理日志"失败 272 项"），非管理员环境下由 analyze 直接滤除
    Rule("win-temp", "system", "safe", "Windows 临时文件",
         "{sys}/windows/temp/**", "系统临时目录",
         allow_in_protected=True, list_children_only=True, needs_admin=True),
    Rule("user-temp", "system", "safe", "用户临时文件",
         "{users}/*/appdata/local/temp/**", "%TEMP%，只列内部子项逐个删（根目录常有占用文件）",
         list_children_only=True),
    Rule("prefetch", "system", "review", "预取文件",
         "{sys}/windows/prefetch/**", "Prefetch，删后系统会重建（首次启动稍慢）",
         allow_in_protected=True, needs_admin=True),
    Rule("win-update-cache", "system", "safe", "Windows 更新缓存",
         "{sys}/windows/softwaredistribution/download/**", "已安装更新的下载残留",
         allow_in_protected=True, list_children_only=True, needs_admin=True),
    Rule("crash-dumps", "system", "safe", "崩溃转储",
         "{users}/*/appdata/local/crashdumps/**", "程序崩溃 dump"),
    Rule("win-minidump", "system", "safe", "系统小型转储",
         "{sys}/windows/minidump/**", "蓝屏 minidump",
         allow_in_protected=True, needs_admin=True),
    Rule("thumb-cache", "system", "safe", "缩略图缓存",
         "{users}/*/appdata/local/microsoft/windows/explorer/thumbcache_*.db",
         "资源管理器缩略图，删后重建", allow_in_protected=True),
    Rule("icon-cache", "system", "safe", "图标缓存",
         "{users}/*/appdata/local/microsoft/windows/explorer/iconcache_*.db",
         "资源管理器图标缓存，删后重建", allow_in_protected=True),
    Rule("win-logs-cbs", "system", "review", "CBS 日志",
         "{sys}/windows/logs/cbs/**", "系统组件日志",
         allow_in_protected=True, needs_admin=True),
    Rule("delivery-opt", "system", "safe", "传递优化缓存",
         "{sys}/windows/softwaredistribution/deliveryoptimization/**", "",
         allow_in_protected=True, list_children_only=True, needs_admin=True),
    Rule("inet-cache", "system", "safe", "IE/系统网络缓存",
         "{users}/*/appdata/local/microsoft/windows/inetcache/**", "",
         allow_in_protected=True),
    Rule("wer-reports", "system", "safe", "系统错误报告",
         "{sys}/programdata/microsoft/windows/wer/**", "Windows Error Reporting",
         allow_in_protected=True, list_children_only=True, needs_admin=True),
    Rule("wer-user-reports", "system", "safe", "用户错误报告",
         "{users}/*/appdata/local/microsoft/windows/wer/**", "当前用户的错误报告队列",
         allow_in_protected=True, list_children_only=True),

    # ---- 回收站 ----
    Rule("recycle-bin", "system", "review", "回收站",
         "?:/$recycle.bin/**", "各盘回收站（里面可能有你还想恢复的文件，确认后再勾）"),

    # ---- 浏览器缓存 ----
    Rule("chrome-cache", "browser", "safe", "Chrome 缓存",
         "{users}/*/appdata/local/google/chrome/user data/*/cache/**", "",
         allow_in_protected=True),
    Rule("chrome-code-cache", "browser", "safe", "Chrome 代码缓存",
         "{users}/*/appdata/local/google/chrome/user data/*/code cache/**", "",
         allow_in_protected=True),
    Rule("edge-cache", "browser", "safe", "Edge 缓存",
         "{users}/*/appdata/local/microsoft/edge/user data/*/cache/**", "",
         allow_in_protected=True),
    Rule("firefox-cache", "browser", "safe", "Firefox 缓存",
         "{users}/*/appdata/local/mozilla/firefox/profiles/*/cache2/**", "",
         allow_in_protected=True),
    Rule("chrome-crashpad", "browser", "safe", "Chrome 崩溃转储",
         "{users}/*/appdata/local/google/chrome/user data/*/crashpad/**",
         "浏览器崩溃转储，删后无损失", allow_in_protected=True,
         list_children_only=True),
    Rule("edge-crashpad", "browser", "safe", "Edge 崩溃转储",
         "{users}/*/appdata/local/microsoft/edge/user data/*/crashpad/**",
         "浏览器崩溃转储，删后无损失", allow_in_protected=True,
         list_children_only=True),

    # ---- 开发工具缓存 ----
    Rule("npm-cache", "dev", "safe", "npm 缓存",
         "{users}/*/appdata/local/npm-cache/**", "", list_children_only=True),
    Rule("npm-cache-roaming", "dev", "safe", "npm 缓存(Roaming)",
         "{users}/*/appdata/roaming/npm-cache/**", "", list_children_only=True),
    Rule("pnpm-store", "dev", "review", "pnpm 全局存储",
         "**/.pnpm-store/**", "pnpm 内容寻址存储，删后重装依赖需重新下载"),
    Rule("pip-cache", "dev", "safe", "pip 缓存",
         "{users}/*/appdata/local/pip/cache/**", "", list_children_only=True),
    Rule("uv-cache", "dev", "safe", "uv 缓存",
         "{users}/*/appdata/local/uv/cache/**", "", list_children_only=True),
    Rule("uv-cache-alt", "dev", "safe", "uv 缓存( roaming )",
         "{users}/*/appdata/roaming/uv/cache/**", "", list_children_only=True),
    Rule("gradle-cache", "dev", "review", "Gradle 缓存",
         "{users}/*/.gradle/caches/**", "删后下次构建重新下载依赖"),
    Rule("cargo-cache", "dev", "review", "Cargo 缓存",
         "{users}/*/.cargo/registry/cache/**", ""),
    Rule("nuget-cache", "dev", "review", "NuGet 缓存",
         "{users}/*/.nuget/packages/**", ""),
    Rule("yarn-cache", "dev", "safe", "Yarn 缓存",
         "{users}/*/appdata/local/yarn/cache/**", "", list_children_only=True),
    Rule("npx-cache", "dev", "safe", "npx 包缓存",
         "{users}/*/appdata/local/npm-cache/_npx/**", "npx 临时执行的包缓存，删后重新下载",
         list_children_only=True),
    Rule("poetry-cache", "dev", "safe", "Poetry 缓存",
         "{users}/*/appdata/local/pypoetry/cache/**", "", list_children_only=True),
    Rule("conda-pkgs", "dev", "review", "conda 包缓存",
         "{users}/*/.conda/pkgs/**", "conda 下载的包缓存，删后重装环境需重新下载"),
    Rule("conda-pkgs-alt", "dev", "review", "conda 包缓存(miniconda)",
         "{users}/*/miniconda3/pkgs/**", "同上"),
    Rule("conda-pkgs-alt2", "dev", "review", "conda 包缓存(anaconda)",
         "{users}/*/anaconda3/pkgs/**", "同上"),
    Rule("go-mod-cache", "dev", "review", "Go 模块缓存",
         "{users}/*/go/pkg/mod/cache/**", "删后重新下载依赖"),
    Rule("cargo-src", "dev", "review", "Cargo 源码缓存",
         "{users}/*/.cargo/registry/src/**", "已解压的 crate 源码，删后重新下载"),
    Rule("gradle-dists", "dev", "review", "Gradle 发行版",
         "{users}/*/.gradle/wrapper/dists/**", "gradle wrapper 下载的发行版，删后构建时重新下载"),
    Rule("yarn-berry-cache", "dev", "review", "Yarn Berry 缓存",
         "**/.yarn/cache/**", "项目内 zero-install 缓存，删后重新下载"),

    # ---- AI 工具缓存 ----
    Rule("hf-cache", "ai", "review", "HuggingFace 缓存",
         "{users}/*/.cache/huggingface/**", "模型缓存，删后重新下载"),
    Rule("comfy-cache", "ai", "safe", "ComfyUI 下载缓存",
         "**/comfyui-cache/**", "ComfyUI 安装包/下载缓存"),
    Rule("ollama-blobs-tmp", "ai", "review", "Ollama 临时",
         "{users}/*/.ollama/tmp/**", ""),
    Rule("ollama-models", "ai", "caution", "Ollama 模型本体",
         "{users}/*/.ollama/models/blobs/**", "模型文件很大，删了重新下载很贵——确认不用了再勾"),
    Rule("torch-hub", "ai", "review", "Torch Hub 模型缓存",
         "{users}/*/.cache/torch/hub/checkpoints/**", "预训练权重，删后重新下载"),
    Rule("whisper-models", "ai", "review", "Whisper 模型缓存",
         "{users}/*/.cache/whisper/**", "预训练权重，删后重新下载"),
    Rule("playwright-browsers", "dev", "review", "Playwright 浏览器",
         "{users}/*/appdata/local/ms-playwright/**", "测试浏览器二进制（数 GB），删后测试时重新下载"),
    Rule("puppeteer-browsers", "dev", "review", "Puppeteer 浏览器",
         "{users}/*/.cache/puppeteer/**", "同上"),
    Rule("electron-builder-cache", "dev", "safe", "Electron 打包缓存",
         "{users}/*/appdata/local/electron-builder/cache/**", "下载的 Electron 发行包，删后打包时重新下载"),
    Rule("d3dscache", "system", "safe", "DirectX 着色器缓存",
         "{users}/*/appdata/local/d3dscache/**", "游戏/3D 应用的着色器缓存，删后首次启动重新编译", list_children_only=True),
    Rule("nvidia-glcache", "system", "safe", "NVIDIA 着色器缓存",
         "{users}/*/appdata/local/nvidia/glcache/**", "同上", list_children_only=True),
    Rule("nvidia-dxcache", "system", "safe", "NVIDIA DXCache",
         "{users}/*/appdata/local/nvidia/dxcache/**", "同上", list_children_only=True),
    Rule("amd-shader-cache", "system", "safe", "AMD 着色器缓存",
         "{users}/*/appdata/local/amd/dxcache/**", "同上", list_children_only=True),
    Rule("amd-glcache", "system", "safe", "AMD GLCache",
         "{users}/*/appdata/local/amd/glcache/**", "同上", list_children_only=True),
    Rule("font-cache", "system", "safe", "系统字体缓存",
         "{sys}/windows/serviceprofiles/localservice/appdata/local/fontcache/**",
         "字体缓存，删后重建", allow_in_protected=True, needs_admin=True,
         list_children_only=True),
    Rule("defender-scan-history", "system", "review", "Defender 扫描历史",
         "{sys}/programdata/microsoft/windows defender/scans/history/**",
         "Defender 扫描记录；服务占用，多半要先进安全模式或停服务",
         allow_in_protected=True, needs_admin=True, list_children_only=True),
    Rule("nvidia-ota-artifacts", "system", "safe", "NVIDIA App 驱动安装包缓存",
         "{sys}/programdata/nvidia corporation/nvidia app/updateframework/ota-artifacts/**",
         "NVIDIA App 下载安装后的驱动安装包残留（数 GB），安装完成后可安全清除",
         allow_in_protected=True, list_children_only=True),

    # ---- 现代 AI 桌面端与开发运行态 ----
    Rule("claude-old-3p", "ai", "safe", "Claude 旧版废弃镜像",
         "{users}/*/appdata/local/packages/claude_*/localcache/local/claude-3p/**",
         "Claude 桌面端 9 月旧版遗留的废弃 Linux 虚拟机路径，当前已不再使用",
         allow_in_protected=True),
    Rule("claude-vm-bundles", "ai", "review", "Claude Coworker VM 镜像",
         "{users}/*/appdata/local/packages/claude_*/localcache/**/vm_bundles/claudevm.bundle/rootfs.vhdx",
         "Claude Desktop 本地代码沙箱的 Linux 虚拟盘（约 8GB），若不用 Coworker 可清理",
         match_dir=False, allow_in_protected=True),
    Rule("claude-vm-zst", "ai", "safe", "Claude VM 解压残留压缩包",
         "{users}/*/appdata/local/packages/claude_*/localcache/**/vm_bundles/claudevm.bundle/*.zst",
         "Claude Desktop 虚拟机镜像解压后遗留的原始 .zst 压缩包（1.2GB+），完全无用",
         match_dir=False, allow_in_protected=True),
    Rule("codex-runtime-install", "dev", "safe", "Codex CLI 安装残留",
         "{users}/*/.cache/codex-runtimes/codex-runtime-install-*/**",
         "Codex CLI 历史版本安装失败或升级后残留的临时解压包与 tar.gz",
         allow_in_protected=True),

    # ---- 即时通讯应用缓存 ----
    Rule("wechat-xweb-cache", "app", "safe", "微信内置浏览器缓存",
         "{users}/*/appdata/roaming/tencent/wechat/xweb/crashpad/**",
         "微信 XWeb 崩溃转储与临时缓存", allow_in_protected=True, list_children_only=True),
    Rule("qq-nt-cache", "app", "safe", "QQ NT 聊天临时缓存",
         "{users}/*/appdata/roaming/tencent/qq/nt_qq/nt_data/chattemp/**",
         "QQ NT 聊天临时文件缓存", allow_in_protected=True, list_children_only=True),

    # ---- 项目构建产物 ----
    Rule("node-modules", "project", "review", "node_modules",
         "**/node_modules/**", "依赖目录，npm/pnpm install 可重建"),
    Rule("py-venv", "project", "review", "Python 虚拟环境",
         "**/*-env/**", "*-env 命名的 venv（如 facefix-env），可重建"),
    Rule("py-cache", "project", "safe", "__pycache__",
         "**/__pycache__/**", "Python 字节码缓存"),
    Rule("pyc-files", "project", "safe", ".pyc 文件",
         "**/*.pyc", match_dir=False),
    Rule("gradle-build", "project", "review", "Gradle build 产物",
         "**/app/build/intermediates/**", "Android 构建中间产物"),
    Rule("electron-dist-old", "project", "review", "Electron 旧安装包",
         "**/dist/*.exe", "dist 下的 exe 安装包（保留最新，旧的删）",
         match_dir=False),

    # ---- 备份/快照 ----
    Rule("pre-update-zip", "backup", "review", "更新前备份 zip",
         "**/pre-update-*.zip", "更新成功后的备份压缩包"),
    Rule("state-snapshots", "backup", "review", "状态快照",
         "**/state-snapshots/**", ""),
]

# ============================================================
# 本机定制规则（今天实战发现的真实垃圾位置）
# ============================================================
LOCAL_RULES = [
    Rule("ae-cache", "app", "safe", "After Effects 磁盘缓存",
         "f:/temp flies/ae/**", "AE 媒体缓存，删后自动重建"),
    Rule("youku-cache", "app", "safe", "优酷离线缓存",
         "f:/youku files/youkudisk/ngdata/**", "优酷缓存的视频 .dat 数据"),
    Rule("youku-xcdn", "app", "safe", "优酷 XCDN 缓存",
         "f:/youku files/youkudisk/ngxcdn/**", ""),
    Rule("comfy-shared-cache", "ai", "safe", "ComfyUI 安装包缓存",
         "f:/comfy-desktop/comfyui-cache/**", "v0.29.0 安装包等下载残留"),
    Rule("hermes-uv", "dev", "safe", "Hermes uv 缓存",
         "f:/ai flies/hermes/cache/uv/**", ""),
    Rule("hermes-scratch", "dev", "review", "Hermes scratch 临时",
         "f:/ai flies/hermes/cache/scratch/**", "agent 临时工作文件"),
    Rule("hermes-backups-old", "backup", "review", "Hermes 旧备份",
         "f:/ai flies/hermes/backups/pre-update-*.zip", ""),
    Rule("hermes-snapshots", "backup", "review", "Hermes 状态快照",
         "f:/ai flies/hermes/state-snapshots/**", ""),
    Rule("music-dist-old", "project", "review", "163music 旧构建产物",
         "f:/ai flies/hermes work/163 music/dist/*.exe",
         "dist 下旧版本 exe（保留最新 5.2.0）", match_dir=False),
    Rule("xy-old-apk", "project", "review", "XY reader 旧 APK",
         "f:/ai flies/hermes work/xy reader/outputs/*.apk",
         "outputs 下旧版 APK（保留最新两版）", match_dir=False),
    Rule("facefix-env", "project", "review", "facefix 实验环境",
         "f:/ai flies/hermes work/facefix-env/**", "人脸修复实验 venv，可重建"),
    Rule("gfpgan-models", "ai", "review", "GFPGAN 模型残留",
         "f:/ai flies/hermes work/models-gfpgan/**", ""),
    Rule("tmp-dir", "system", "safe", "F 盘 tmp",
         "f:/tmp/**", "", list_children_only=True),
]

ALL_RULES = COMMON_RULES + LOCAL_RULES
RULES_BY_ID = {r.id: r for r in ALL_RULES}

CATEGORY_NAMES = {
    "system": "系统垃圾", "browser": "浏览器缓存", "dev": "开发工具缓存",
    "ai": "AI 工具", "app": "应用缓存", "project": "项目构建产物",
    "backup": "备份/快照", "media": "媒体文件", "other": "其他",
}

RISK_NAMES = {"safe": "✅ 安全", "review": "⚠️ 建议确认", "caution": "🛑 谨慎"}


def _extract_prefilter(pattern: str) -> tuple:
    """从 glob 模式提取快速预筛用的 (字面量前缀, 必含片段)。

    - 前缀：模式开头到第一个通配符为止的字面量段（如 c:/users/*/... -> "c:/users/"）。
      若开头就是 **（跨段），前缀为空。
    - 片段：模式中最长的纯字面量片段（>=3字符），用于 `in` 预判（如 "appdata/local/temp"）。
    两者都只用于"提前排除"，不影响最终正则匹配的准确性。
    """
    p = norm(pattern)
    if p.endswith("/**"):
        p = p[:-3]
    # 最长字面量片段（按通配符切分）
    literals = re.split(r"\*\*|\*|\?", p)
    literals = [s.strip("/") for s in literals if len(s.strip("/")) >= 3]
    token = max(literals, key=len) if literals else ""
    # 前缀：开头连续的字面量（到第一个通配符）
    m = re.match(r"^([^*?]*)", p)
    prefix = m.group(1) if m else ""
    # 前缀以 / 结尾才安全（避免 "c:/users" 误排除 "c:/users2"）；
    # 若前缀就是整个 p（无通配），保留完整前缀
    if prefix and not prefix.endswith("/") and prefix != p:
        prefix = prefix[:prefix.rfind("/") + 1]
    # ** 开头 -> 无前缀
    if p.startswith("**") or p.startswith("*"):
        prefix = ""
    return prefix, token


def matches_any(path: str, is_dir: bool, mtime: float, rules,
                now: float = 0.0) -> list:
    """返回所有匹配的规则。path 会被 norm 一次；now 传入避免每规则重复取时间。"""
    np = norm(path)
    base = now or time.time()
    return [r for r in rules if r.matches(np, is_dir, mtime, base)]


class RuleIndex:
    """规则前缀索引：O(路径深度) 定位候选规则，替代逐条扫描。

    背景：接入 winapp2.ini 后规则数从几十条涨到数千条，每个路径
    逐条 matches 会慢到不可用。绝大多数规则的 pattern 开头是字面量
    （_extract_prefilter 已提取 _prefix），按前缀分桶后，匹配时沿
    路径的段前缀查 dict 即可。无字面量前缀的模式（**/xxx）进
    _always 桶逐条跑——这类规则数量少。
    """

    def __init__(self, rules: list):
        self._by_prefix: dict = {}
        self._always: list = []
        for r in rules:
            r.compile()  # 确保预筛字段已初始化
            if r._prefix:
                self._by_prefix.setdefault(r._prefix, []).append(r)
            else:
                self._always.append(r)

    def match(self, path: str, is_dir: bool, mtime: float = 0.0,
              now: float = 0.0) -> list:
        """返回该路径命中的规则（已过 Rule.matches 全量校验）。path 须已 norm。"""
        out = list(self._always)
        segs = path.split("/")
        acc = ""
        seen = set()
        for s in segs:
            acc = acc + s if not acc else acc + "/" + s
            # 两种前缀形态：段边界本身（无尾斜杠，字面量完整模式）
            # 和 带尾斜杠（后面还有通配段）
            for key in (acc, acc + "/"):
                if key in seen:
                    continue
                seen.add(key)
                lst = self._by_prefix.get(key)
                if lst:
                    out.extend(lst)
        base = now or time.time()
        return [r for r in out if r.matches(path, is_dir, mtime, base)]


def classify(path: str, is_dir: bool, mtime: float = 0.0,
             rules=None) -> Optional[Rule]:
    """返回第一个匹配的规则，无匹配返回 None。"""
    rules = rules or ALL_RULES
    np = norm(path)
    base = time.time()
    for r in rules:
        if r.matches(np, is_dir, mtime, base):
            return r
    return None


def rules_for_drive(drive: str, rules=None) -> list:
    """返回适用于指定盘符的规则子集（多盘清理的关键）。

    drive: 'd' / 'd:' / 'D:\\' / 'd:/x' 均可。
    规则盘符作用域 rule.drive：
    - None : 盘符无关（**/node_modules 等），任何盘都适用 -> 保留
    - '?'  : 任意单盘（回收站，每盘都有）-> 保留
    - 'c'等: 绑定具体盘符 -> 仅当等于 drive 时保留
    这样扫 D 盘时不会拿 c:/windows、f:/temp 之类规则空跑或误报。
    """
    rules = rules or ALL_RULES
    d = norm(str(drive))[:1]
    return [r for r in rules if r.drive in (None, "?", d)]


def drive_of(path: str) -> str:
    """取路径所属盘符（小写单字母）；非 Windows 绝对路径返回 ''。

    容忍 'd:'（无斜杠，norm 去尾分隔符后的形态）与 'd:/...'。
    """
    p = norm(path)
    m = re.match(r"^([a-z]):(?:/|$)", p)
    return m.group(1) if m else ""
