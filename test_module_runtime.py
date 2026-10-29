# -*- coding: utf-8 -*-
"""module_runtime 自测：路由映射 / 安装表读写 / gate / AI 联动 / 数据删除白名单 / 线程启停。

运行：python test_module_runtime.py
不触碰 8790 端口与任何真实外部服务：外部服务动作（agent/qcode/nodes）
在测试中替换为记录器；appearance.json 指向临时目录；删除测试在临时沙盒内。
"""
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import module_runtime as mr

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]' if cond else '[FAIL]'} {name} {detail}")


def _sandbox_appearance(tmp):
    """把安装表指向临时文件并清缓存。返回写入函数。"""
    mr._appearance_path = lambda: str(tmp / "appearance.json")
    mr._cache_sig = None
    mr._cache_ids = None

    def write(modules_value):
        p = tmp / "appearance.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        raw = {"theme": "dark"}
        if modules_value is not None:
            raw["modules"] = modules_value
        p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        mr._cache_sig = None
        mr._cache_ids = None

    return write


def test_route_map():
    print("== 路由映射 ==")
    cases = [
        ("/api/cleaner/scan", "cleaner"),
        ("/api/clashnodes/state", "nodes"),
        ("/api/clash/inspector/overview", "nodes"),
        ("/api/clipboard/history", "clipboard"),
        ("/api/clipboard/action", "clipboard"),
        ("/api/translate", "translate"),
        ("/api/translate/config", "translate"),
        ("/api/ai/news", "ai-news"),
        ("/api/ai/news/refresh", "ai-news"),
        ("/api/ai/briefings", "ai-briefing"),
        ("/api/ai/briefings/other", "ai-briefing"),
        ("/api/ai/rankings", "ai-rankings"),
        ("/api/ai/rankings/refresh", "ai-rankings"),
        ("/api/ai/models", "ai-compare"),
        ("/api/ai/image", "ai-briefing"),
    ]
    for path, expect in cases:
        got = mr.route_module(path)
        check(f"{path} -> {expect}", got == expect, f"实际 {got}")
    for core_path in ("/api/stats/summary", "/api/appearance", "/api/modules/state",
                      "/api/modules/install", "/api/hotkeys", "/api/webui/version",
                      "/", "/index.html", "/api/unknown/x"):
        got = mr.route_module(core_path)
        check(f"核心/未知不拦 {core_path}", got is None, f"实际 {got}")
    # /api/ai 未知子路径保守放行（不归任何模块）
    check("/api/ai/unknown 保守放行", mr.route_module("/api/ai/unknown") is None)


def test_gate_and_persistence():
    print("== 安装表与 gate ==")
    tmp = Path(tempfile.mkdtemp(prefix="modrt_test_"))
    try:
        write = _sandbox_appearance(tmp)
        # 缺 modules 字段 = 默认集（translate/cleaner 安装，clipboard 未安装）
        write(None)
        check("缺字段按默认集", mr.module_enabled("translate") and mr.module_enabled("cleaner"))
        check("默认集外视为未安装", not mr.module_enabled("clipboard"))
        check("核心恒放行", mr.module_enabled("dashboard") and mr.module_enabled("status"))
        check("未登记 id 视为未安装", not mr.module_enabled("no-such-mod"))
        # 全量卸载（installed=[]）是有效状态
        write({"installed": []})
        check("空表=全部未安装", not mr.module_enabled("translate") and not mr.module_enabled("clipboard"))
        check("空表核心仍放行", mr.module_enabled("dashboard"))
        # gate 随安装表热切换
        write({"installed": ["clipboard"]})
        check("gate 放行已安装", mr.gate("/api/clipboard/history"))
        check("gate 拦截未安装", not mr.gate("/api/translate"))
        check("gate 核心路径放行", mr.gate("/api/stats/summary") and mr.gate("/api/appearance"))
        # set_installed 落盘读回，且不冲掉其它键
        ret = mr.set_installed("translate", True)
        check("set_installed 落盘 translate", ret is None and mr.module_enabled("translate"))
        raw = json.loads((tmp / "appearance.json").read_text(encoding="utf-8"))
        check("其它键保留", raw.get("theme") == "dark")
        check("modules 结构正确", raw["modules"]["installed"] == ["clipboard", "translate"])
        mr.set_installed("translate", False)
        check("再卸载读回", not mr.module_enabled("translate"))
        mr.set_installed("translate", False)     # 幂等
        check("重复卸载幂等", not mr.module_enabled("translate"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ai_scheduler_linkage():
    print("== AI 组与调度器联动 ==")
    tmp = Path(tempfile.mkdtemp(prefix="modrt_ai_"))
    try:
        write = _sandbox_appearance(tmp)
        import ai_fetcher
        steps = []
        write({"installed": ["ai-briefing"]})       # 仍有 AI 模块安装
        mr._ai_scheduler_sync(steps)
        check("部分安装 -> 调度器跑", ai_fetcher._sched_started is True)
        write({"installed": []})                    # 全部卸载
        mr._ai_scheduler_sync(steps)
        check("全部卸载 -> 调度器停", ai_fetcher._sched_started is False
              and ai_fetcher._sched_stop.is_set())
        write({"installed": ["ai-news"]})
        mr._ai_scheduler_sync(steps)
        check("重新安装 -> 调度器复跑", ai_fetcher._sched_started is True
              and not ai_fetcher._sched_stop.is_set())
        ai_fetcher.stop_scheduler()                 # 测试收尾停掉
        check("收尾停调度器", ai_fetcher._sched_started is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_uninstall_install_flow():
    print("== 卸载/安装主流程 ==")
    tmp = Path(tempfile.mkdtemp(prefix="modrt_flow_"))
    try:
        write = _sandbox_appearance(tmp)
        write({"installed": ["clipboard", "translate"]})
        # 外部服务动作替换为记录器（不杀真实进程/服务）
        # 用 translate 做装卸主流程样本：开源版与主库都有该模块与 /api/translate 路由
        calls = []
        mr._UNINSTALL_ACTIONS["translate"] = lambda steps: calls.append("trans-stop")
        mr._INSTALL_ACTIONS["translate"] = lambda steps: calls.append("trans-start")
        mr._UNINSTALL_ACTIONS["clipboard"] = lambda steps: calls.append("clip-stop")
        # core 拒绝
        ret = mr.uninstall_module("dashboard")
        check("核心模块拒绝卸载", not ret["ok"])
        ret = mr.uninstall_module("no-such")
        check("未登记模块拒绝卸载", not ret["ok"])
        # 正常卸载：先落盘后动作
        ret = mr.uninstall_module("translate")
        check("卸载成功", ret["ok"] and "trans-stop" in calls)
        check("卸载后 gate 拦截", not mr.gate("/api/translate"))
        check("steps 含落盘明细", any("落盘" in s for s in ret["steps"]))
        # 安装回去
        ret = mr.install_module("translate")
        check("安装成功", ret["ok"] and "trans-start" in calls)
        check("安装后 gate 放行", mr.gate("/api/translate"))
        check("安装表读回", "translate" in mr.installed_ids())
        # 未登记模块拒绝安装
        ret = mr.install_module("no-such")
        check("未登记模块拒绝安装", not ret["ok"])
        # module_state 形状
        st = mr.module_state()
        check("state 含全部模块", {m["id"] for m in st["modules"]} == set(mr.ALL_MODULE_IDS))
        qcore = [m for m in st["modules"] if m["id"] == "dashboard"][0]
        check("state 核心标记", qcore["core"] and qcore["installed"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_data_files_sandbox():
    print("== 数据删除白名单 ==")
    tmp = Path(tempfile.mkdtemp(prefix="modrt_data_"))
    old_base = mr.BASE_DIR
    try:
        mr.BASE_DIR = str(tmp)
        # 构造沙盒文件与目录
        (tmp / "clipboard_cache.json").write_text("[]", encoding="utf-8")
        (tmp / "clash_node_history").mkdir()
        (tmp / "clash_node_history" / "snap.json").write_text("{}", encoding="utf-8")
        (tmp / "clash_node_reports").mkdir()
        (tmp / "clash_node_reports" / "r.yaml").write_text("{}", encoding="utf-8")
        (tmp / "clash_node_audit_cache.json").write_text("{}", encoding="utf-8")
        # 清单形状
        df = mr.data_files("clipboard")
        check("clipboard 清单 1 项", len(df) == 1 and df[0][1] == "剪贴板历史记录" and not df[0][2])
        df = mr.data_files("nodes")
        check("nodes 清单含目录", any(is_dir for _, _, is_dir in df))
        check("凭据/配置模块无删除清单",
              mr.data_files("agent") == [] and mr.data_files("agent-qcode") == [])
        check("未登记模块无清单", mr.data_files("no-such") == [])
        # 删除执行
        steps = []
        removed = mr._delete_data("nodes", steps)
        check("nodes 删除生效（3 项全删）", len(removed) == 3)
        check("目录已移除", not (tmp / "clash_node_history").exists()
              and not (tmp / "clash_node_reports").exists())
        check("步骤记录删除明细", any("已删除数据" in s for s in steps))
        # 不存在文件静默跳过
        steps = []
        removed = mr._delete_data("clipboard", steps)   # 第一次删
        check("已存在文件删除", removed == ["剪贴板历史记录"])
        removed = mr._delete_data("clipboard", steps)   # 第二次不存在
        check("不存在时静默", removed == [])
        # LOCALAPPDATA: 前缀解析
        df = mr.data_files("cleaner")
        check("cleaner 指向 LOCALAPPDATA", "diskcleaner" in df[0][0] and df[0][0].startswith(
            (str(Path(mr._localappdata(""))), str(mr._localappdata("")))))
    finally:
        mr.BASE_DIR = old_base
        shutil.rmtree(tmp, ignore_errors=True)


def test_clipboard_thread_lifecycle():
    print("== 剪贴板监听线程启停（真实线程） ==")
    tmp = Path(tempfile.mkdtemp(prefix="modrt_clip_"))
    old_store = None
    try:
        import clipboard_history as ch
        old_store = ch.STORE
        ch.STORE = tmp / "clipboard_cache.json"
        ch._items.clear()
        ch._next_id = 1
        ch._watcher_started = False
        ch._stop_event.clear()
        ch.start_watcher()
        check("watcher 启动", ch._watcher_started)
        time.sleep(0.2)
        alive = any(t.name == "clipboard-watch" and t.is_alive()
                    for t in threading.enumerate())
        check("监听线程存活", alive)
        ch.stop_watcher()
        check("stop 后标志复位", ch._watcher_started is False
              and ch._stop_event.is_set())
        deadline = time.time() + 3
        gone = False
        while time.time() < deadline:
            if not any(t.name == "clipboard-watch" and t.is_alive()
                       for t in threading.enumerate()):
                gone = True
                break
            time.sleep(0.1)
        check("监听线程真实退出", gone)
        ch.start_watcher()          # 重启幂等：清事件再起线程
        time.sleep(0.2)
        alive2 = any(t.name == "clipboard-watch" and t.is_alive()
                     for t in threading.enumerate())
        check("重启后线程再存活", alive2 and ch._watcher_started)
        ch.stop_watcher()
        time.sleep(1.0)             # 收尾
    finally:
        if old_store is not None:
            import clipboard_history as ch
            ch.STORE = old_store
            ch._watcher_started = False
            ch._stop_event.clear()
            ch._items.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_route_map()
    test_gate_and_persistence()
    test_ai_scheduler_linkage()
    test_uninstall_install_flow()
    test_data_files_sandbox()
    test_clipboard_thread_lifecycle()
    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
