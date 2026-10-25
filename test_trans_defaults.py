# -*- coding: utf-8 -*-
"""验证「本地翻译服务默认关闭」改动：门禁与引擎默认值。不真的拉起 llama-server。"""
import os
import sys
import types
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import translate_backend as tb
import selection
import tray_app


class FakeToolboxApp:
    def __init__(self, cfg):
        self.cfg = cfg
        self._backend_state = "stopped"
        self.proc = None
        self.toasts = []

    def _toast(self, m, secs=3):
        self.toasts.append(m)


FakeToolboxApp.ensure_backend = tray_app.ToolboxApp.ensure_backend
FakeToolboxApp._backend_or_start = tray_app.ToolboxApp._backend_or_start


class TestTransDefaults(unittest.TestCase):
    def setUp(self):
        self.calls = {"start": 0, "port_busy": False}

        def fake_start_server(**kw):
            self.calls["start"] += 1
            self.calls["port_busy"] = True
            return types.SimpleNamespace(poll=lambda: None)

        self._orig_tb_start = tb.start_server
        self._orig_tb_wait = tb.wait_ready
        self._orig_tb_busy = tb.port_busy
        self._orig_tb_cfg = tb.CONFIG_FILE
        self._orig_tb_load = getattr(tb, "load_trans_engine_config", None)

        self._orig_tray_start = tray_app.start_server
        self._orig_tray_wait = tray_app.wait_ready
        self._orig_tray_busy = tray_app.port_busy

        tb.start_server = fake_start_server
        tb.wait_ready = lambda timeout=180: True
        tb.port_busy = lambda port=tb.PORT: self.calls["port_busy"]

        tray_app.start_server = fake_start_server
        tray_app.wait_ready = lambda timeout=180: True
        tray_app.port_busy = lambda port=tb.PORT: self.calls["port_busy"]

    def tearDown(self):
        tb.start_server = self._orig_tb_start
        tb.wait_ready = self._orig_tb_wait
        tb.port_busy = self._orig_tb_busy
        tb.CONFIG_FILE = self._orig_tb_cfg
        if self._orig_tb_load:
            tb.load_trans_engine_config = self._orig_tb_load

        tray_app.start_server = self._orig_tray_start
        tray_app.wait_ready = self._orig_tray_wait
        tray_app.port_busy = self._orig_tray_busy

    def test_engine_default_fallback_to_cloud(self):
        tb.CONFIG_FILE = tb.Path(r"F:\__nonexistent_dir__\no_such_config.json")
        cfg = tb.load_trans_engine_config()
        self.assertEqual(cfg["trans_engine"], "cloud")

    def test_selection_default_auto_start_is_false(self):
        self.assertFalse(selection.DEFAULT_CONFIG["auto_start_backend"])

    def test_ensure_backend_auto_start_false_does_not_start(self):
        f = FakeToolboxApp({"auto_start_backend": False, "model": "m", "ngl": 1, "threads": 1, "ctx": 1})
        f.ensure_backend()
        self.assertEqual(self.calls["start"], 0)
        self.assertEqual(f._backend_state, "stopped")

    def test_ensure_backend_manual_force_starts(self):
        f = FakeToolboxApp({"auto_start_backend": False, "model": "m", "ngl": 1, "threads": 1, "ctx": 1})
        f.ensure_backend(force=True)
        self.assertEqual(self.calls["start"], 1)
        self.assertEqual(f._backend_state, "running")

    def test_ensure_backend_auto_start_true_starts(self):
        f = FakeToolboxApp({"auto_start_backend": True, "model": "m", "ngl": 1, "threads": 1, "ctx": 1})
        f.ensure_backend()
        self.assertEqual(self.calls["start"], 1)

    def test_backend_or_start_cloud_engine_skips(self):
        tb.load_trans_engine_config = lambda: {"trans_engine": "cloud"}
        f = FakeToolboxApp({"auto_start_backend": False, "model": "m", "ngl": 1, "threads": 1, "ctx": 1})
        res = f._backend_or_start()
        self.assertEqual(self.calls["start"], 0)
        self.assertTrue(res)

    def test_backend_or_start_local_engine_starts_on_demand(self):
        self.calls["port_busy"] = False
        tb.load_trans_engine_config = lambda: {"trans_engine": "local"}
        f = FakeToolboxApp({"auto_start_backend": False, "model": "m", "ngl": 1, "threads": 1, "ctx": 1})
        res = f._backend_or_start()
        self.assertEqual(self.calls["start"], 1)
        self.assertTrue(res)


if __name__ == "__main__":
    unittest.main()
