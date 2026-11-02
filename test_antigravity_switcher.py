# -*- coding: utf-8 -*-
"""小宇工具箱 · antigravity_switcher 模块单元测试。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import antigravity_switcher


class TestAntigravitySwitcher(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.test_dir.name)
        self.mock_accounts_file = self.tmp_path / "antigravity_accounts.json"

        self.patchers = [
            patch.object(antigravity_switcher, "ANTIGRAVITY_ACCOUNTS_FILE", self.mock_accounts_file),
            patch.object(antigravity_switcher, "LEGACY_PROFILES_DIR", self.tmp_path / "legacy_profiles"),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        self.test_dir.cleanup()

    def test_parse_cred_info_valid(self):
        # 构造有效凭据对象
        cred = {
            "token": {
                "access_token": "mock_access_token",
                "token_type": "Bearer",
                "refresh_token": "1//mock_refresh_token",
                "expiry": "2030-01-01T00:00:00Z"
            },
            "auth_method": "consumer",
            "id_token_email": "test@gmail.com"
        }
        info = antigravity_switcher.parse_cred_info(cred)
        self.assertEqual(info["email"], "test@gmail.com")
        self.assertEqual(info["auth_method"], "consumer")
        self.assertTrue(info["has_refresh"])
        self.assertFalse(info["is_expired"])
        self.assertIsNotNone(info["exp_minutes"])

    def test_load_and_save_profiles_data(self):
        data = {
            "active_profile_id": "acc-1",
            "profiles": [
                {
                    "id": "acc-1",
                    "name": "测试号1",
                    "email": "test1@gmail.com",
                    "credential": {"token": {"refresh_token": "1//abc"}}
                }
            ]
        }
        ok = antigravity_switcher.save_profiles_data(data)
        self.assertTrue(ok)
        self.assertTrue(self.mock_accounts_file.exists())

        loaded = antigravity_switcher.load_profiles_data()
        self.assertEqual(loaded["active_profile_id"], "acc-1")
        self.assertEqual(len(loaded["profiles"]), 1)
        self.assertEqual(loaded["profiles"][0]["name"], "测试号1")

    def test_add_or_update_and_delete_profile(self):
        valid_cred = {
            "token": {
                "access_token": "acc_tok",
                "refresh_token": "1//test_rt",
                "expiry": "2030-01-01T00:00:00Z"
            },
            "id_token_email": "user@gmail.com"
        }
        # 1. 添加新账号
        res = antigravity_switcher.add_or_update_profile("我的测试号", valid_cred)
        self.assertTrue(res["ok"])
        pid = res["id"]

        status = antigravity_switcher.get_antigravity_status()
        self.assertEqual(len(status["profiles"]), 1)
        self.assertEqual(status["profiles"][0]["name"], "我的测试号")
        self.assertEqual(status["profiles"][0]["email"], "user@gmail.com")

        # 2. 修改该账号
        res2 = antigravity_switcher.add_or_update_profile("改名测试号", valid_cred, profile_id=pid)
        self.assertTrue(res2["ok"])
        status2 = antigravity_switcher.get_antigravity_status()
        self.assertEqual(status2["profiles"][0]["name"], "改名测试号")

        # 3. 删除该账号
        del_res = antigravity_switcher.delete_antigravity_profile(pid)
        self.assertTrue(del_res["ok"])
        status3 = antigravity_switcher.get_antigravity_status()
        self.assertEqual(len(status3["profiles"]), 0)

    @patch("antigravity_switcher.read_raw_credential")
    def test_save_current_as_profile(self, mock_read):
        mock_read.return_value = {
            "token": {
                "access_token": "tok_curr",
                "refresh_token": "1//curr_rt"
            },
            "id_token_email": "current_active@gmail.com"
        }
        res = antigravity_switcher.save_current_as_profile("当前大号")
        self.assertTrue(res["ok"])
        self.assertEqual(res["email"], "current_active@gmail.com")

        status = antigravity_switcher.get_antigravity_status()
        self.assertEqual(len(status["profiles"]), 1)
        self.assertEqual(status["profiles"][0]["name"], "当前大号")

    @patch("antigravity_switcher.kill_antigravity_clients")
    @patch("antigravity_switcher.launch_antigravity_desktop")
    @patch("antigravity_switcher.write_raw_credential")
    @patch("antigravity_switcher.read_raw_credential")
    def test_switch_antigravity_account(self, mock_read, mock_write, mock_launch, mock_kill):
        mock_read.return_value = None
        mock_write.return_value = True
        mock_launch.return_value = True
        mock_kill.return_value = True

        target_cred = {
            "token": {"refresh_token": "1//target"},
            "id_token_email": "target@gmail.com"
        }
        res = antigravity_switcher.add_or_update_profile("目标账号", target_cred, profile_id="target-pid")
        self.assertTrue(res["ok"])

        switch_res = antigravity_switcher.switch_antigravity_account("target-pid", restart=True)
        self.assertTrue(switch_res["ok"])
        self.assertEqual(switch_res["switched_to"], "目标账号")
        mock_kill.assert_called_once()
        mock_write.assert_called_once_with(target_cred)
        mock_launch.assert_called_once()

    def test_reorder_and_move_profiles(self):
        c1 = {"id_token_email": "1@gmail.com"}
        c2 = {"id_token_email": "2@gmail.com"}
        c3 = {"id_token_email": "3@gmail.com"}

        antigravity_switcher.add_or_update_profile("账号1", c1, profile_id="p1")
        antigravity_switcher.add_or_update_profile("账号2", c2, profile_id="p2")
        antigravity_switcher.add_or_update_profile("账号3", c3, profile_id="p3")

        # 批量排序重排为 p3, p1, p2
        res = antigravity_switcher.reorder_antigravity_profiles(["p3", "p1", "p2"])
        self.assertTrue(res["ok"])
        st = antigravity_switcher.get_antigravity_status()
        self.assertEqual([p["id"] for p in st["profiles"]], ["p3", "p1", "p2"])

        # 将 p2 上移
        res_up = antigravity_switcher.move_antigravity_profile("p2", "up")
        self.assertTrue(res_up["ok"])
        st2 = antigravity_switcher.get_antigravity_status()
        self.assertEqual([p["id"] for p in st2["profiles"]], ["p3", "p2", "p1"])

        # 将 p1 置顶
        res_top = antigravity_switcher.move_antigravity_profile("p1", "top")
        self.assertTrue(res_top["ok"])
        st3 = antigravity_switcher.get_antigravity_status()
        self.assertEqual([p["id"] for p in st3["profiles"]], ["p1", "p3", "p2"])

    @patch("antigravity_switcher.exchange_rt_for_tokens")
    def test_refresh_credential_if_needed(self, mock_exchange):
        mock_exchange.return_value = {
            "ok": True,
            "email": "test@gmail.com",
            "credential": {
                "token": {
                    "access_token": "new_access_token",
                    "refresh_token": "1//valid_rt",
                    "expiry": "2030-01-01T01:00:00Z",
                    "expires_in": 3600,
                },
                "id_token_email": "test@gmail.com",
            },
        }
        expired_cred = {
            "token": {
                "access_token": "old_access_token",
                "refresh_token": "1//valid_rt",
                "expiry": "2020-01-01T00:00:00Z",
            },
            "id_token_email": "test@gmail.com",
        }
        new_cred, did_refresh = antigravity_switcher.refresh_credential_if_needed(expired_cred, min_minutes=5)
        self.assertTrue(did_refresh)
        self.assertEqual(new_cred["token"]["access_token"], "new_access_token")
        mock_exchange.assert_called_once_with("1//valid_rt")


if __name__ == "__main__":
    unittest.main()
