import os
import sys

# XiaoyuToolbox-OSS.spec —— 开源版专用打包清单（由 XiaoyuToolbox.spec 派生，2026-10-25）。
# 与主库 spec 的差异：
#   1. 前端资源剔除：Agent 美化四件套（v1.0 暂缓）、私有专区交互脚本、
#      额度监控条品牌小图标（额度监控条随私有模块一并裁剪）；
#   2. hiddenimports 剔除全部私有反代/美化模块与 tornado 整包收集；
#   3. 打包对象是 tools/sync_oss.py 产出的开源目录（裁剪后的源码），不是主库源码。
# 打包环境与主库一致：项目 .venv，且必须设置 TCL_LIBRARY/TK_LIBRARY（见 AGENTS.md §2.3）。

_tcl = os.path.join(sys.base_prefix, 'tcl', 'tcl8.6')
_tk = os.path.join(sys.base_prefix, 'tcl', 'tk8.6')
if os.path.isdir(_tcl) and 'TCL_LIBRARY' not in os.environ:
    os.environ['TCL_LIBRARY'] = _tcl
if os.path.isdir(_tk) and 'TK_LIBRARY' not in os.environ:
    os.environ['TK_LIBRARY'] = _tk

# 仅打包运行时需要的前端文件（开源版清单；icons/app_logo.svg 必带，漏了左上角破图）。
from PyInstaller.utils.hooks import collect_submodules
# 诊断：hiddenimports 曾漏收这两个包（modulegraph 未随依赖图找到），确认 collect 结果非空
_diag_s2t = collect_submodules('send2trash')
_diag_zst = collect_submodules('zstandard')
print('[oss-spec] collect send2trash:', _diag_s2t)
print('[oss-spec] collect zstandard:', _diag_zst)
assert _diag_s2t and _diag_zst, 'collect_submodules 为空：PyInstaller 进程 import 不到这两个包'

webui_files = [
    ('index.html', 'webui'),
    ('app.js', 'webui'),
    ('style.css', 'webui'),
    ('sample_data.js', 'webui'),
    ('zp.css', 'webui'),
    ('cleaner.css', 'webui'),
    ('games.css', 'webui'),
    ('games.js', 'webui'),
    ('icons/app_logo.svg', 'webui/icons'),
]
webui_datas = [
    (os.path.join('webui', rel), dest)
    for rel, dest in webui_files
    if os.path.isfile(os.path.join('webui', rel))
]

if os.path.isfile('VERSION'):
    webui_datas.append(('VERSION', '.'))

import webview
_wv_lib = os.path.join(os.path.dirname(webview.__file__), 'lib')
if os.path.isdir(_wv_lib):
    for _dll in ['Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll',
                 'WebBrowserInterop.x64.dll', 'WebBrowserInterop.x86.dll']:
        _p = os.path.join(_wv_lib, _dll)
        if os.path.isfile(_p):
            webui_datas.append((_p, '.'))
    for _arch in ['win-x64', 'win-x86', 'win-arm64']:
        _arch_dll = os.path.join(_wv_lib, 'runtimes', _arch, 'native', 'WebView2Loader.dll')
        if os.path.isfile(_arch_dll):
            webui_datas.append((_arch_dll, _arch))

# winrt 纯 Python 包装包手写清单（同主库 spec 的坑：静态分析看不到，必须逐个登记）。
winrt_wrappers = [
    'winrt',
    'winrt.runtime', 'winrt.runtime._internals',
    'winrt.system',
    'winrt.windows.foundation', 'winrt.windows.globalization',
    'winrt.windows.graphics.imaging', 'winrt.windows.media.ocr',
    'winrt.windows.storage.streams',
]

a = Analysis(
    ['toolbox_main.py'],
    pathex=[],
    binaries=[],
    datas=webui_datas,
    hiddenimports=[
        'tkinter',
        'win32clipboard', 'win32con',
        'winrt._winrt_windows_media_ocr',
        'winrt._winrt_windows_globalization',
        'winrt._winrt_windows_graphics_imaging',
        'winrt._winrt_windows_storage_streams',
        'winrt._winrt_windows_foundation',
        'ocr', 'popup', 'selection', 'tray_icon',
        'translate_backend', 'stats_server', 'stats_api',
        'ai_hub', 'ai_media_rankings',
        'codex_switcher',       # OpenAI 专区（开源保留的 Agent 模块）
        'clipboard_utils',      # 公共剪贴板（独立零依赖实现）
        'clipboard_history',
        'module_runtime',
        'PIL',                  # OCR 图像解码依赖（ocr.py 延迟 import）
        'epic_claimer',
        'clash_node_check',
        'clash_inspector',
        'yaml',
        'win32crypt', 'cryptography',
        'cryptography.hazmat.primitives.ciphers.aead',
        'urllib.request', 'sqlite3', 'psutil',
        'zstandard',            # 仪表盘 DSH 来源解 zstd 会话文件（stats_api 延迟 import）
        'diskcleaner', 'diskcleaner.api', 'diskcleaner.cli',
        'diskcleaner.scanner', 'diskcleaner.analyzer', 'diskcleaner.rules',
        'diskcleaner.cleaner', 'diskcleaner.winapp2', 'diskcleaner.perm',
        'diskcleaner.profiler',
        'send2trash',
        'webview', 'webview.platforms.winforms', 'webview.platforms.edgechromium',
        'clr_loader', 'clr_loader.netfx', 'pythonnet',
    ] + collect_submodules('send2trash') + collect_submodules('zstandard') \
      + winrt_wrappers,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='XiaoyuToolbox-OSS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['qwen_toolbox.ico'],
)
