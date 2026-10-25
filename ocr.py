# -*- coding: utf-8 -*-
"""OCR 截图翻译模块。

链路：GDI 截图 -> 2-3x 放大 -> Windows.Media.Ocr（中英双引擎） -> 后端翻译。

实测结论（2026-09-17，本机）：
  - 系统只自带 zh-Hans-CN 包，英文包需 Add-WindowsCapability 加装
    Language.OCR~~~en-US~0.0.1.0（一次性提权，已装）。
  - 中文引擎认英文=乱码，必须按语言分引擎。
  - 小字直接认准确率低，3x LANCZOS 放大后大幅提升。
  - winrt 包 import 名是下划线式：winrt._winrt_windows_media_ocr，不是点分。
"""
import asyncio
import ctypes
import io
from ctypes import wintypes

from PIL import Image

from translate_backend import strip_think

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# GDI 签名
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
gdi32.GetDeviceCaps.restype = ctypes.c_int
gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
SM_CXSCREEN = 0
SM_CYSCREEN = 1

gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.UINT]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]

SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


def capture_region(x, y, w, h):
    """GDI BitBlt 截屏指定区域，返回 PIL.Image（RGB）。"""
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, SRCCOPY)
    # 读像素
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # 顶到底
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 24
    bmi.bmiHeader.biCompression = BI_RGB
    buf_len = ((w * 3 + 3) // 4) * 4 * h
    buf = ctypes.create_string_buffer(buf_len)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)
    # create_string_buffer 索引返回 bytes，必须先转 bytes 再按 int 读
    px = bytes(buf)
    # BGR -> RGB，考虑 4 字节对齐 stride
    stride = ((w * 3 + 3) // 4) * 4
    raw = bytearray(h * w * 3)
    for row in range(h):
        src_off = row * stride
        dst_off = row * w * 3
        for col in range(w):
            b = px[src_off + col * 3]
            g = px[src_off + col * 3 + 1]
            r = px[src_off + col * 3 + 2]
            raw[dst_off + col * 3] = r
            raw[dst_off + col * 3 + 1] = g
            raw[dst_off + col * 3 + 2] = b
    return Image.frombytes("RGB", (w, h), bytes(raw))


def capture_fullscreen():
    w = user32.GetSystemMetrics(SM_CXSCREEN)
    h = user32.GetSystemMetrics(SM_CYSCREEN)
    return capture_region(0, 0, w, h)


# ---------------------------------------------------------------- OCR
def _get_winrt():
    try:
        from winrt import _winrt_windows_media_ocr as ocr
        from winrt import _winrt_windows_globalization as glo
        from winrt import _winrt_windows_graphics_imaging as gi
        from winrt import _winrt_windows_storage_streams as st
        return ocr, glo, gi, st
    except ImportError:
        return None


def _pil_to_software_bitmap(pil_img, gi, st):
    """PIL -> WinRT SoftwareBitmap（经 BMP 编码流）。"""
    buf = io.BytesIO()
    pil_img.save(buf, "BMP")
    bmp = buf.getvalue()
    s = st.InMemoryRandomAccessStream()
    w = st.DataWriter(s)
    w.write_bytes(bmp)

    async def _store():
        await w.store_async()
        w.detach_stream()
        w.close()
        dec = await gi.BitmapDecoder.create_async(s)
        return await dec.get_software_bitmap_async()
    return asyncio.run(_store())


def ocr_image(pil_img, lang="auto", upscale=3):
    """识别 PIL 图片文字。

    lang: 'auto'（中英双引擎，结果长的为准）/ 'zh' / 'en'。
    upscale: 放大倍数（小字识别前放大，实测 3x 效果明显）。
    返回 (text, engine_used)。引擎不可用时抛 RuntimeError。
    """
    mods = _get_winrt()
    if mods is None:
        raise RuntimeError("OCR 引擎不可用：winrt 包未安装")
    ocr, glo, gi, st = mods

    if upscale and upscale > 1:
        pil_img = pil_img.resize(
            (pil_img.width * upscale, pil_img.height * upscale), Image.LANCZOS)
    sb = _pil_to_software_bitmap(pil_img.convert("RGB"), gi, st)

    async def _run_one(tag):
        eng = ocr.OcrEngine.try_create_from_language(glo.Language(tag))
        if eng is None:
            return ""
        res = await eng.recognize_async(sb)
        return res.text or ""

    async def _run():
        if lang == "zh":
            return await _run_one("zh-Hans-CN"), "zh"
        if lang == "en":
            return await _run_one("en-US"), "en"
        zh = await _run_one("zh-Hans-CN")
        en = await _run_one("en-US")
        # 双引擎：取更长的（通常=识别更全的）
        if len(en.strip()) >= len(zh.strip()):
            return en, "en"
        return zh, "zh"

    return asyncio.run(_run())


def dpi_scale():
    """物理像素 / 逻辑像素 比值（150% 缩放 -> 1.5）。

    实测坑（2026-09-17）：本进程是 DPI-unaware（venv python 无清单声明），
    Tk 事件坐标与 GDI 截屏坐标差 = 该比值；GetDpiForSystem() 在 unaware
    进程里恒返回 96 不可用，GetDeviceCaps 的 DESKTOPHORZRES(118)/HORZRES(8)
    才是可靠来源。
    """
    try:
        hdc = user32.GetDC(0)
        try:
            phys = gdi32.GetDeviceCaps(hdc, 118)   # DESKTOPHORZRES
            logical = gdi32.GetDeviceCaps(hdc, 8)  # HORZRES
        finally:
            user32.ReleaseDC(0, hdc)
        if logical > 0 and phys >= logical:
            s = phys / logical
            print(f"[ocr] dpi probe: phys={phys} logical={logical} "
                  f"scale={s:.3f}", flush=True)
            return s
    except Exception as e:
        print(f"[ocr] dpi probe failed: {e}", flush=True)
    return 1.0


def ocr_region(x, y, w, h, lang="auto"):
    """截屏指定区域并识别，返回 (text, engine)。

    x/y/w/h 传 Tk 事件坐标（逻辑），内部按 DPI 换算成物理像素再截。
    """
    s = dpi_scale()
    if s != 1.0:
        x, y, w, h = int(x * s), int(y * s), int(w * s), int(h * s)
    print(f"[ocr] capture at {x},{y} {w}x{h} (dpi_scale={s})", flush=True)
    return ocr_image(capture_region(x, y, w, h), lang=lang)


if __name__ == "__main__":
    # 冒烟：全屏截图 -> OCR（只打印前 200 字，不翻译）
    img = capture_fullscreen()
    print("screenshot:", img.size)
    text, eng = ocr_image(img, lang="auto")
    print("engine:", eng)
    print("text[:200]:", text[:200])
