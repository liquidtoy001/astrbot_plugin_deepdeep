import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


class FakeDeepSeek:
    """模拟 DeepSeek Search & Fetch 接口。按顺序返回 queue 里的 (status, body, headers)。"""

    def __init__(self) -> None:
        self.search_queue: list[tuple[int, object, dict]] = []
        self.fetch_queue: list[tuple[int, object, dict]] = []
        self.tavily_queue: list[tuple[int, object, dict]] = []
        self.requests: list[dict] = []

    async def _handle(self, request: web.Request, queue) -> web.Response:
        self.requests.append(
            {
                "path": request.path,
                "auth": request.headers.get("Authorization"),
                "json": await request.json(),
            }
        )
        status, body, headers = (
            queue.pop(0) if queue else (500, {"error": {"message": "queue empty"}}, {})
        )
        if isinstance(body, str):
            return web.Response(status=status, text=body, headers=headers)
        return web.json_response(body, status=status, headers=headers)

    async def search(self, request):
        return await self._handle(request, self.search_queue)

    async def fetch(self, request):
        return await self._handle(request, self.fetch_queue)

    async def tavily(self, request):
        return await self._handle(request, self.tavily_queue)


@pytest.fixture
async def fake_api():
    fake = FakeDeepSeek()
    app = web.Application()
    app.router.add_post("/v1/tools/search", fake.search)
    app.router.add_post("/v1/tools/fetch", fake.fetch)
    # 同一个假服务也模拟 Tavily 的 /search
    app.router.add_post("/search", fake.tavily)
    server = TestServer(app)
    await server.start_server()
    fake.base_url = str(server.make_url("")).rstrip("/")
    yield fake
    await server.close()


@pytest.fixture
def no_sleep():
    calls: list[float] = []

    async def _sleep(seconds: float) -> None:
        calls.append(seconds)

    _sleep.calls = calls
    return _sleep


def make_result(i: int, score: float = 2.0, **extra) -> dict:
    item = {
        "rerank_score": score,
        "title": f"标题{i}",
        "url": f"https://example.com/{i}",
        "snippet": f"短摘要{i}",
        "summary": f"长摘要{i}",
        "content": f"正文{i}" * 50,
        "site_name": "Example",
        "site_icon": None,
        "date": "2026-10-01T00:00:00+00:00",
        "authority": 3,
    }
    item.update(extra)
    return item
