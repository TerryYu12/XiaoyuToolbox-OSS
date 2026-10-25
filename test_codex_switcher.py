# -*- coding: utf-8 -*-
"""小宇工具箱 · codex_switcher 模块单元测试。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_switcher


class TestCodexSwitcher(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.test_dir.name)
        self.mock_accounts_file = self.tmp_path / "codex_accounts.json"
        self.mock_settings_file = self.tmp_path / "settings.json"
        self.mock_auth_file = self.tmp_path / "auth.json"
        self.mock_config_file = self.tmp_path / "config.toml"

        self.patchers = [
            patch.object(codex_switcher, "CODEX_ACCOUNTS_FILE", self.mock_accounts_file),
            patch.object(codex_switcher, "CODEX_PLUS_SETTINGS", self.mock_settings_file),
            patch.object(codex_switcher, "AUTH_FILE", self.mock_auth_file),
            patch.object(codex_switcher, "CONFIG_FILE", self.mock_config_file),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        self.test_dir.cleanup()

    def test_parse_auth_info_valid_jwt(self):
        # 模拟一个标准 JWT payload: {"email": "test@example.com", "exp": 2000000000}
        header_b64 = "eyJhbGciOiJSUzI1NiJ9"
        payload_b64 = "eyJlbWFpbCI6InRlc3RAZXhhbXBsZS5jb20iLCJleHAiOjIwMDAwMDAwMDB9"
        jwt_mock = f"{header_b64}.{payload_b64}.sig"
        auth_data = {
            "tokens": {
                "id_token": jwt_mock,
                "account_id": "acc-12345",
                "refresh_token": "rt.mock"
            }
        }
        info = codex_switcher.parse_auth_info(auth_data)
        self.assertEqual(info["email"], "test@example.com")
        self.assertEqual(info["account_id"], "acc-12345")
        self.assertTrue(info["has_refresh"])
        self.assertFalse(info["is_expired"])

    def test_load_and_save_profiles_data(self):
        data = {
            "active_profile_id": "acc-1",
            "profiles": [
                {
                    "id": "acc-1",
                    "name": "测试号1",
                    "authContents": json.dumps({"tokens": {"account_id": "acc-1", "account_email": "test1@abc.com"}}),
                    "configContents": ""
                }
            ]
        }
        ok = codex_switcher.save_profiles_data(data)
        self.assertTrue(ok)
        self.assertTrue(self.mock_accounts_file.exists())

        loaded = codex_switcher.load_profiles_data()
        self.assertEqual(loaded["active_profile_id"], "acc-1")
        self.assertEqual(len(loaded["profiles"]), 1)
        self.assertEqual(loaded["profiles"][0]["name"], "测试号1")

    def test_add_or_update_and_delete_profile(self):
        valid_auth = json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {
                "account_id": "acc-999",
                "account_email": "hello@xyz.com",
                "refresh_token": "rt.xxx"
            }
        })
        # 1. 添加新账号
        res = codex_switcher.add_or_update_profile("新朋友号", valid_auth)
        self.assertTrue(res["ok"])
        pid = res["id"]

        status = codex_switcher.get_codex_status()
        self.assertEqual(len(status["profiles"]), 1)
        self.assertEqual(status["profiles"][0]["name"], "新朋友号")

        # 2. 修改该账号
        res2 = codex_switcher.add_or_update_profile("改名朋友号", valid_auth, profile_id=pid)
        self.assertTrue(res2["ok"])
        status2 = codex_switcher.get_codex_status()
        self.assertEqual(status2["profiles"][0]["name"], "改名朋友号")

        # 3. 删除该账号
        res3 = codex_switcher.delete_codex_profile(pid)
        self.assertTrue(res3["ok"])
        status3 = codex_switcher.get_codex_status()
        self.assertEqual(len(status3["profiles"]), 0)

    def test_save_current_as_profile(self):
        # 写入模拟当前 auth.json
        curr_auth = {
            "tokens": {
                "account_id": "acc-current",
                "account_email": "current@user.com",
                "refresh_token": "rt.active"
            }
        }
        with open(self.mock_auth_file, "w", encoding="utf-8") as f:
            json.dump(curr_auth, f)

        res = codex_switcher.save_current_as_profile("当前大号")
        self.assertTrue(res["ok"])
        self.assertEqual(res["name"], "当前大号")

        status = codex_switcher.get_codex_status()
        self.assertEqual(len(status["profiles"]), 1)
        self.assertEqual(status["profiles"][0]["info"]["email"], "current@user.com")

    @patch("codex_switcher.kill_codex_clients")
    @patch("codex_switcher.launch_codex_desktop")
    def test_switch_codex_account(self, mock_launch, mock_kill):
        target_auth = json.dumps({
            "tokens": {
                "account_id": "acc-target",
                "account_email": "target@user.com",
                "refresh_token": "rt.target"
            }
        })
        res = codex_switcher.add_or_update_profile("目标账号", target_auth, profile_id="target-id")
        self.assertTrue(res["ok"])

        # 切换
        switch_res = codex_switcher.switch_codex_account("target-id")
        self.assertTrue(switch_res["ok"])
        mock_kill.assert_called_once()
        mock_launch.assert_called_once()

        # 验证写入的目标 auth.json 内容
        with open(self.mock_auth_file, "r", encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written["tokens"]["account_id"], "acc-target")

    def test_reorder_and_move_profiles(self):
        auth1 = json.dumps({"tokens": {"account_id": "id-1", "account_email": "1@a.com"}})
        auth2 = json.dumps({"tokens": {"account_id": "id-2", "account_email": "2@a.com"}})
        auth3 = json.dumps({"tokens": {"account_id": "id-3", "account_email": "3@a.com"}})

        codex_switcher.add_or_update_profile("账号1", auth1, profile_id="p1")
        codex_switcher.add_or_update_profile("账号2", auth2, profile_id="p2")
        codex_switcher.add_or_update_profile("账号3", auth3, profile_id="p3")

        # 测试自定义重新排序
        res = codex_switcher.reorder_codex_profiles(["p3", "p1", "p2"])
        self.assertTrue(res["ok"])
        st = codex_switcher.get_codex_status()
        self.assertEqual([p["id"] for p in st["profiles"]], ["p3", "p1", "p2"])

        # 测试上移下移与置顶
        # 将 p2 上移（目前顺序: p3, p1, p2 -> 上移后: p3, p2, p1）
        res_up = codex_switcher.move_codex_profile("p2", "up")
        self.assertTrue(res_up["ok"])
        st2 = codex_switcher.get_codex_status()
        self.assertEqual([p["id"] for p in st2["profiles"]], ["p3", "p2", "p1"])

        # 将 p1 置顶（目前顺序: p3, p2, p1 -> 置顶后: p1, p3, p2）
        res_top = codex_switcher.move_codex_profile("p1", "top")
        self.assertTrue(res_top["ok"])
        st3 = codex_switcher.get_codex_status()
        self.assertEqual([p["id"] for p in st3["profiles"]], ["p1", "p3", "p2"])

        # 将 p1 下移（目前顺序: p1, p3, p2 -> 下移后: p3, p1, p2）
        res_down = codex_switcher.move_codex_profile("p1", "down")
        self.assertTrue(res_down["ok"])
        st4 = codex_switcher.get_codex_status()
        self.assertEqual([p["id"] for p in st4["profiles"]], ["p3", "p1", "p2"])


if __name__ == "__main__":
    unittest.main()
