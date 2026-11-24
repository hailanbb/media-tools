"""DoubanGateway 主备自动切换、Cookie-Editor 解析与系统接口回归测试。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from app.clients import douban_gateway
from app.core.douban_cookie import mask_cookie_summary, parse_douban_cookie
from app.core.douban_lookup import normalize_detail


class TestDoubanCookieParser(unittest.TestCase):
    def test_cookie_editor_json_parses_filters_expired_and_other_domains(self):
        now = 1_800_000_000.0
        payload = json.dumps(
            [
                {"name": "dbcl2", "value": '"12345678:secretToken"', "domain": ".douban.com", "expirationDate": now + 3600},
                {"name": "bid", "value": "iDf0tyAI54I", "domain": ".douban.com", "expirationDate": now + 3600},
                {"name": "expired_key", "value": "old", "domain": ".douban.com", "expirationDate": now - 60},
                {"name": "SESSIONID", "value": "evil", "domain": ".google.com", "expirationDate": now + 3600},
            ]
        )
        res = parse_douban_cookie(payload, now=now)
        self.assertEqual(res["format_detected"], "cookie_editor_json")
        self.assertTrue(res["has_dbcl2"])
        self.assertTrue(res["has_bid"])
        self.assertFalse(res["auto_generated_bid"])
        self.assertEqual(res["uid_masked"], "1234****")
        self.assertEqual(res["bid_masked"], "iDf0****")
        self.assertIn("dbcl2=", res["header"])
        self.assertIn("bid=iDf0tyAI54I", res["header"])
        self.assertNotIn("expired_key", res["header"])
        self.assertNotIn("SESSIONID", res["header"])

    def test_netscape_and_header_string_and_auto_bid_and_mask(self):
        now = 1_800_000_000.0
        netscape = (
            "# Netscape HTTP Cookie File\n"
            f".douban.com\tTRUE\t/\tTRUE\t{int(now + 1000)}\tdbcl2\t\"987654321:abc\"\n"
            f".other.com\tTRUE\t/\tFALSE\t{int(now + 1000)}\tfoo\tbar\n"
        )
        ns_res = parse_douban_cookie(netscape, now=now)
        self.assertEqual(ns_res["format_detected"], "netscape")
        self.assertTrue(ns_res["has_dbcl2"])
        self.assertTrue(ns_res["auto_generated_bid"])
        self.assertRegex(ns_res["header"], r"bid=[A-Za-z0-9]{11}")

        hdr_res = parse_douban_cookie("Cookie: ck=xyz1; ll=\"108288\"", now=now)
        self.assertEqual(hdr_res["format_detected"], "header_string")
        self.assertFalse(hdr_res["has_dbcl2"])
        self.assertTrue(any("dbcl2" in w for w in hdr_res["warnings"]))

        summary = mask_cookie_summary(ns_res["header"], format_detected=ns_res["format_detected"])
        self.assertTrue(summary["configured"])
        self.assertEqual(summary["uid_masked"], "9876****")
        self.assertNotIn("abc", json.dumps(summary))


class TestDoubanNormalizeDetail(unittest.TestCase):
    def test_rexxar_and_frodo_fields_normalized(self):
        raw = {
            "id": "25853071",
            "title": "庆余年 第一季",
            "type": "tv",
            "episodes_count": 46,
            "pubdate": ["2019-11-26(中国大陆)"],
        }
        norm = normalize_detail(raw)
        self.assertEqual(norm["douban_id"], "25853071")
        self.assertEqual(norm["number_of_episodes"], 46)
        self.assertEqual(norm["type"], "电视剧")
        self.assertEqual(norm["year"], "2019")


class TestDoubanGatewayFailoverAndApi(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        douban_gateway.reset_state_for_tests()
        self.settings_patch = patch.object(
            douban_gateway,
            "get_settings",
            return_value=SimpleNamespace(
                data_dir=self.tmpdir.name,
                douban_proxy_base="",
                douban_proxy_token="",
                douban_cookie="",
                douban_failover_cooldown_seconds=300,
                mp_url="",
                mp_token="",
                cd2_url="",
                cd2_token="",
                tmdb_api_key="",
            ),
        )
        self.settings_patch.start()

    def tearDown(self):
        self.settings_patch.stop()
        douban_gateway.reset_state_for_tests()
        self.tmpdir.cleanup()

    async def test_failover_to_cf_proxy_and_auto_recover_back_to_mp(self):
        douban_gateway.save_runtime_config(
            {
                "proxy_base": "https://douban.ubaby.top",
                "proxy_token": "secret-tok",
            }
        )

        # 1. MP 正常 -> 主通道 online
        mp_ok = SimpleNamespace(search_media=AsyncMock(return_value=[{"title": "庆余年"}]))
        h1 = await douban_gateway.check_health(mp_ok, mp_online=True, force=True)
        self.assertEqual(h1["status"], "online")
        self.assertEqual(h1["active_channel"], "moviepilot")
        self.assertFalse(douban_gateway.should_skip_mp())

        # 2. MP 返回空且 CF /probe 成功 -> 触发冷却并自动切换 fallback:cf_proxy
        mp_empty = SimpleNamespace(search_media=AsyncMock(return_value=[]))
        mock_probe_resp = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "ok": True,
                "results": {"suggest": {"ok": True, "sample_title": "庆余年 第一季"}},
            },
        )
        with patch("app.clients.douban_gateway.httpx.AsyncClient") as mock_cls:
            inst = mock_cls.return_value.__aenter__.return_value
            inst.get = AsyncMock(return_value=mock_probe_resp)
            h2 = await douban_gateway.check_health(mp_empty, mp_online=True, force=True)

        self.assertEqual(h2["status"], "fallback:cf_proxy")
        self.assertEqual(h2["active_channel"], "cf_proxy")
        self.assertTrue(douban_gateway.should_skip_mp())
        self.assertGreater(h2["mp_retry_in_seconds"], 0)

        # 3. 冷却期未满（非 force）：自动跳过 MP，直接用 CF 备选承接
        mp_should_not_call = SimpleNamespace(search_media=AsyncMock(side_effect=AssertionError("cooldown should skip MP")))
        with patch("app.clients.douban_gateway.httpx.AsyncClient") as mock_cls:
            inst = mock_cls.return_value.__aenter__.return_value
            inst.get = AsyncMock(return_value=mock_probe_resp)
            h3 = await douban_gateway.check_health(mp_should_not_call, mp_online=True, force=False)
        self.assertEqual(h3["status"], "fallback:cf_proxy")

        # 4. 冷却到期后 MP 恢复 -> 自动探测并回切 MP 主通道
        douban_gateway._STATE["mp_cooldown_until"] = time.time() - 5
        h4 = await douban_gateway.check_health(mp_ok, mp_online=True, force=False)
        self.assertEqual(h4["status"], "online")
        self.assertEqual(h4["active_channel"], "moviepilot")
        self.assertFalse(douban_gateway.should_skip_mp())

    async def test_system_douban_api_routes(self):
        from fastapi import FastAPI
        from app.api import system as system_api

        test_app = FastAPI()
        test_app.include_router(system_api.router, prefix="/api/system")

        system_api._HEALTH_CACHE.clear()
        system_api._DOUBAN_DETAIL_CACHE = None

        fake_settings = SimpleNamespace(
            data_dir=self.tmpdir.name,
            douban_proxy_base="",
            douban_proxy_token="",
            douban_cookie="",
            douban_failover_cooldown_seconds=300,
            mp_url="",
            mp_token="",
            cd2_url="",
            cd2_token="",
            tmdb_api_key="",
        )
        with patch.object(system_api, "get_settings", return_value=fake_settings):
            async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
                r_save = await client.post(
                    "/api/system/douban/config",
                    json={
                        "proxy_base": "https://douban.ubaby.top",
                        "proxy_token": "tok123",
                        "raw_cookie": json.dumps([{"name": "dbcl2", "value": '"11223344:xyz"', "domain": ".douban.com"}]),
                    },
                )
                self.assertEqual(r_save.status_code, 200)
                body = r_save.json()
                self.assertTrue(body["config"]["has_proxy_token"])
                self.assertEqual(body["config"]["cookie_summary"]["uid_masked"], "1122****")
                self.assertNotIn("tok123", json.dumps(body))
                self.assertNotIn("xyz", json.dumps(body))

                r_get = await client.get("/api/system/douban/config")
                self.assertEqual(r_get.status_code, 200)
                self.assertEqual(r_get.json()["proxy_base"], "https://douban.ubaby.top")

                mock_probe_resp = SimpleNamespace(
                    status_code=200,
                    json=lambda: {"ok": True, "results": {"suggest": {"ok": True, "sample_title": "庆余年"}}},
                )
                with patch("app.clients.douban_gateway.httpx.AsyncClient") as mock_cls:
                    inst = mock_cls.return_value.__aenter__.return_value
                    inst.get = AsyncMock(return_value=mock_probe_resp)
                    r_health = await client.get("/api/system/health?force_refresh=true")
                self.assertEqual(r_health.status_code, 200)
                self.assertEqual(r_health.json()["services"]["douban"], "fallback:cf_proxy")


if __name__ == "__main__":
    unittest.main()
