# -*- coding: utf-8 -*-
"""开源版分发包组装：从 dist/XiaoyuToolbox-OSS.exe + README.md + LICENSE 打包为 Zip，并生成 SHA256 校验文件。"""
import argparse
import hashlib
import os
import sys
import zipfile

# 保证在 Windows CI (cp1252) 控制台下打印中文不报 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")



def resolve_oss_root(specified=None):
    if specified and os.path.isdir(specified):
        return os.path.abspath(specified)
    cwd = os.path.abspath(os.getcwd())
    if os.path.isfile(os.path.join(cwd, "XiaoyuToolbox-OSS.spec")):
        return cwd
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    if os.path.isfile(os.path.join(parent, "XiaoyuToolbox-OSS.spec")):
        return parent
    peer = os.path.join(os.path.dirname(parent), "XiaoyuToolbox-OSS")
    if os.path.isdir(peer):
        return peer
    return cwd


def get_version(oss_root):
    v_path = os.path.join(oss_root, "VERSION")
    if not os.path.isfile(v_path):
        return "unknown"
    with open(v_path, "r", encoding="utf-8") as f:
        return f.read().strip()


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="XiaoyuToolbox-OSS 分发包组装工具")
    parser.add_argument("--root", default=None, help="XiaoyuToolbox-OSS 仓库根目录")
    args = parser.parse_args()

    oss_root = resolve_oss_root(args.root)
    exe_path = os.path.join(oss_root, "dist", "XiaoyuToolbox-OSS.exe")
    out_dir = os.path.join(oss_root, "dist")
    os.makedirs(out_dir, exist_ok=True)

    if not os.path.isfile(exe_path):
        print(f"[package] 错误：未找到编译产物 {exe_path}")
        return 1

    ver = get_version(oss_root)
    zip_name = f"XiaoyuToolbox-OSS-v{ver}-win-x64.zip"
    zip_path = os.path.join(out_dir, zip_name)

    files_to_pack = [
        (exe_path, "XiaoyuToolbox-OSS.exe"),
        (os.path.join(oss_root, "README.md"), "README.md"),
        (os.path.join(oss_root, "LICENSE"), "LICENSE"),
    ]

    print(f"[package] 目标仓库: {oss_root}")
    print(f"[package] 正在创建分发压缩包: {zip_path}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for src, arc in files_to_pack:
            if not os.path.isfile(src):
                print(f"[package] 缺少文件: {src}")
                return 1
            print(f"  + {arc} ({os.path.getsize(src):,} bytes)")
            zf.write(src, arc)

    zip_size = os.path.getsize(zip_path)
    zip_hash = sha256_file(zip_path)
    exe_size = os.path.getsize(exe_path)
    exe_hash = sha256_file(exe_path)

    # 1. 单个 zip sha256
    with open(zip_path + ".sha256", "w", encoding="utf-8") as f:
        f.write(f"{zip_hash}  {zip_name}\n")

    # 2. 单个 exe sha256
    with open(exe_path + ".sha256", "w", encoding="utf-8") as f:
        f.write(f"{exe_hash}  XiaoyuToolbox-OSS.exe\n")

    # 3. 汇总 checksums.txt
    checksums_path = os.path.join(out_dir, "checksums.txt")
    with open(checksums_path, "w", encoding="utf-8") as f:
        f.write(f"{zip_hash}  {zip_name}\n")
        f.write(f"{exe_hash}  XiaoyuToolbox-OSS.exe\n")

    # 4. Release 说明表格
    table_path = os.path.join(out_dir, "release_assets_table.md")
    table_content = (
        "### 📦 资产下载与校验\n\n"
        "| 文件名 | 说明 | 大小 | SHA256 |\n"
        "|---|---|---|---|\n"
        f"| `{zip_name}` | 绿色免安装压缩包（推荐，解压直接运行） | ~{zip_size / (1024 * 1024):.2f} MB | `{zip_hash}` |\n"
        f"| `XiaoyuToolbox-OSS.exe` | 独立单文件可执行程序（内置全部运行时） | ~{exe_size / (1024 * 1024):.2f} MB | `{exe_hash}` |\n"
    )
    with open(table_path, "w", encoding="utf-8") as f:
        f.write(table_content)

    print(f"[package] 打包完成！")
    print(f"  Zip 文件: {zip_path} ({zip_size / (1024 * 1024):.2f} MB)")
    print(f"  Zip SHA256: {zip_hash}")
    print(f"  Exe 文件: {exe_path} ({exe_size / (1024 * 1024):.2f} MB)")
    print(f"  Exe SHA256: {exe_hash}")
    print(f"  校验清单: {checksums_path}")
    print(f"  Release 说明表格: {table_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
