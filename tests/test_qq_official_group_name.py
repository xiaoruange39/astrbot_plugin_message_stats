import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace


UTILS_DIR = Path(__file__).parents[1] / "utils"
GROUP_OPENID = "8E5B1C3A9D7F4021AC63BE95"
CHANNEL_ID = "1234567890"


def _stub_astrbot():
    """astrbot 未安装/加载缓慢时用桩替代，本模块只用到 logger 与类型标注。"""
    if "astrbot" in sys.modules:
        return
    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    api.logger = SimpleNamespace(
        debug=lambda *a, **kw: None,
        info=lambda *a, **kw: None,
        warning=lambda *a, **kw: None,
        error=lambda *a, **kw: None,
    )
    event = ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    api.event = event
    astrbot.api = api
    sys.modules.update(
        {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event}
    )


def _load_helper():
    """按包内模块加载 qq_official_helper，绕开 utils/__init__ 的重依赖。"""
    _stub_astrbot()
    package = ModuleType("ms_utils")
    package.__path__ = [str(UTILS_DIR)]
    sys.modules["ms_utils"] = package
    spec = importlib.util.spec_from_file_location(
        "ms_utils.qq_official_helper", UTILS_DIR / "qq_official_helper.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["ms_utils.qq_official_helper"] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


class _FakeHttp:
    """假的 botpy BotHttp：记录请求次数，按脚本返回结果或抛错。"""

    def __init__(self, results):
        self.results = list(results)
        self.routes = []

    async def request(self, route, **kwargs):
        self.routes.append(route)
        result = self.results.pop(0) if self.results else None
        if isinstance(result, Exception):
            raise result
        return result


def _fake_event(http, platform_name="qq_official"):
    client = SimpleNamespace(api=SimpleNamespace(_http=http))
    return SimpleNamespace(
        get_platform_name=lambda: platform_name,
        client=client,
        platform_meta=SimpleNamespace(name=platform_name),
    )


class OfficialGroupNameTests(unittest.TestCase):
    def setUp(self):
        helper._OFFICIAL_GROUP_NAME_CACHE.clear()
        helper._OFFICIAL_GROUP_NAME_LOCKS.clear()

    def test_fetches_group_name_and_caches_it(self):
        http = _FakeHttp([{"group_name": "测试群", "group_member_num": 12}])
        event = _fake_event(http)

        name = asyncio.run(helper.fetch_official_group_name(GROUP_OPENID, event=event))
        self.assertEqual(name, "测试群")
        self.assertEqual(
            http.routes[0].url,
            f"https://api.sgroup.qq.com/v2/groups/{GROUP_OPENID}/info",
        )

        # 第二次直接命中缓存，不再发请求
        again = asyncio.run(helper.fetch_official_group_name(GROUP_OPENID, event=event))
        self.assertEqual(again, "测试群")
        self.assertEqual(len(http.routes), 1)

    def test_skips_channel_id(self):
        http = _FakeHttp([{"group_name": "不该被查到"}])
        event = _fake_event(http)

        name = asyncio.run(helper.fetch_official_group_name(CHANNEL_ID, event=event))
        self.assertEqual(name, "")
        self.assertEqual(http.routes, [])

    def test_skips_non_official_platform(self):
        http = _FakeHttp([{"group_name": "不该被查到"}])
        event = _fake_event(http, platform_name="aiocqhttp")

        name = asyncio.run(helper.fetch_official_group_name(GROUP_OPENID, event=event))
        self.assertEqual(name, "")
        self.assertEqual(http.routes, [])

    def test_failure_is_cached_to_avoid_per_message_requests(self):
        http = _FakeHttp([RuntimeError("boom"), {"group_name": "测试群"}])
        event = _fake_event(http)

        self.assertEqual(
            asyncio.run(helper.fetch_official_group_name(GROUP_OPENID, event=event)), ""
        )
        self.assertEqual(
            asyncio.run(helper.fetch_official_group_name(GROUP_OPENID, event=event)), ""
        )
        self.assertEqual(len(http.routes), 1)

    def test_denied_error_backs_off_longer_than_transient_error(self):
        from botpy.errors import ForbiddenError

        self.assertIn(ForbiddenError, helper._group_api_denied_errors())

        http = _FakeHttp([ForbiddenError("no permission")])
        event = _fake_event(http)
        asyncio.run(helper.fetch_official_group_name(GROUP_OPENID, event=event))
        _, denied_expire_at = helper._OFFICIAL_GROUP_NAME_CACHE[GROUP_OPENID]

        helper._OFFICIAL_GROUP_NAME_CACHE.clear()
        http = _FakeHttp([RuntimeError("boom")])
        event = _fake_event(http)
        asyncio.run(helper.fetch_official_group_name(GROUP_OPENID, event=event))
        _, retry_expire_at = helper._OFFICIAL_GROUP_NAME_CACHE[GROUP_OPENID]

        self.assertGreater(denied_expire_at, retry_expire_at)

    def test_uses_context_clients_without_event(self):
        http = _FakeHttp([{"group_name": "定时推送群"}])
        client = SimpleNamespace(api=SimpleNamespace(_http=http))
        platform = SimpleNamespace(
            meta=lambda: SimpleNamespace(name="qq_official_webhook"),
            get_client=lambda: client,
        )
        other = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))
        context = SimpleNamespace(
            platform_manager=SimpleNamespace(get_insts=lambda: [other, platform])
        )

        name = asyncio.run(
            helper.fetch_official_group_name(GROUP_OPENID, context=context)
        )
        self.assertEqual(name, "定时推送群")
        self.assertEqual(len(http.routes), 1)

    def test_concurrent_lookups_share_one_request(self):
        http = _FakeHttp([{"group_name": "测试群"}])
        event = _fake_event(http)

        async def race():
            return await asyncio.gather(
                *(
                    helper.fetch_official_group_name(GROUP_OPENID, event=event)
                    for _ in range(5)
                )
            )

        self.assertEqual(asyncio.run(race()), ["测试群"] * 5)
        self.assertEqual(len(http.routes), 1)


if __name__ == "__main__":
    unittest.main()
