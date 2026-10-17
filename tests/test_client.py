import asyncio

import pytest
from conftest import make_result

from core.client import (
    DeepSeekToolsClient,
    DeepSeekToolsError,
    RateLimiter,
    validate_fetch_url,
)


def make_client(fake_api, no_sleep, key="sk-test", **kw):
    return DeepSeekToolsClient(
        lambda: key, base_url=fake_api.base_url, qps=0, sleep=no_sleep, **kw
    )


async def test_search_sends_expected_request(fake_api, no_sleep):
    fake_api.search_queue.append((200, {"results": [make_result(1)]}, {}))
    client = make_client(fake_api, no_sleep)
    results = await client.search(
        "DeepSeek V4",
        count=3,
        ori_question="  DeepSeek V4 发布了吗？ ",
        need_content=True,
    )
    await client.close()

    req = fake_api.requests[0]
    assert req["path"] == "/v1/tools/search"
    assert req["auth"] == "Bearer sk-test"
    assert req["json"] == {
        "query": "DeepSeek V4",
        "count": 3,
        "need_content": True,
        "ori_question": "DeepSeek V4 发布了吗？",
    }
    assert results[0].title == "标题1"
    assert results[0].rerank_score == 2.0
    assert results[0].authority == 3


async def test_search_clamps_query_count_and_ori_question(fake_api, no_sleep):
    fake_api.search_queue.append((200, {"results": []}, {}))
    client = make_client(fake_api, no_sleep)
    await client.search("字" * 200, count=999, ori_question="问" * 300)
    await client.close()

    body = fake_api.requests[0]["json"]
    assert len(body["query"]) == 128
    assert body["count"] == 50
    assert len(body["ori_question"]) == 128


async def test_blank_ori_question_is_omitted(fake_api, no_sleep):
    fake_api.search_queue.append((200, {"results": []}, {}))
    client = make_client(fake_api, no_sleep)
    await client.search("q", ori_question="   ")
    await client.close()
    assert "ori_question" not in fake_api.requests[0]["json"]


async def test_tolerates_missing_and_null_fields(fake_api, no_sleep):
    fake_api.search_queue.append(
        (
            200,
            {
                "results": [
                    {"title": "t", "url": "u", "rerank_score": None, "date": None}
                ]
            },
            {},
        )
    )
    client = make_client(fake_api, no_sleep)
    (r,) = await client.search("q")
    await client.close()
    assert r.rerank_score == 0.0 and r.authority == -1 and r.date is None


async def test_retries_on_429_then_succeeds(fake_api, no_sleep):
    fake_api.search_queue += [
        (429, {"error": {"message": "slow down"}}, {"Retry-After": "3"}),
        (503, "bad gateway", {}),
        (200, {"results": [make_result(1)]}, {}),
    ]
    client = make_client(fake_api, no_sleep, max_retries=2)
    results = await client.search("q")
    await client.close()

    assert len(results) == 1
    assert len(fake_api.requests) == 3
    # 第一次退避取 Retry-After（3s）与指数退避（1s）中较大者，第二次 2s
    assert no_sleep.calls == [3.0, 2.0]


async def test_gives_up_after_max_retries(fake_api, no_sleep):
    fake_api.search_queue += [(500, {"error": {"message": "x"}}, {})] * 3
    client = make_client(fake_api, no_sleep, max_retries=2)
    with pytest.raises(DeepSeekToolsError) as info:
        await client.search("q")
    await client.close()
    assert info.value.status == 500
    assert len(fake_api.requests) == 3


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
async def test_does_not_retry_client_errors(fake_api, no_sleep, status):
    fake_api.search_queue.append((status, {"error": {"message": "nope"}}, {}))
    client = make_client(fake_api, no_sleep, max_retries=2)
    with pytest.raises(DeepSeekToolsError) as info:
        await client.search("q")
    await client.close()
    assert info.value.status == status
    assert info.value.detail == "nope"
    assert len(fake_api.requests) == 1
    assert no_sleep.calls == []


async def test_402_message_tells_model_to_stop(fake_api, no_sleep):
    fake_api.search_queue.append(
        (402, {"error": {"message": "Insufficient Balance"}}, {})
    )
    client = make_client(fake_api, no_sleep)
    with pytest.raises(DeepSeekToolsError) as info:
        await client.search("q")
    await client.close()
    assert "余额不足" in info.value.message


async def test_missing_key_fails_without_request(fake_api, no_sleep):
    client = make_client(fake_api, no_sleep, key="")
    with pytest.raises(DeepSeekToolsError) as info:
        await client.search("q")
    await client.close()
    assert info.value.status == 401
    assert fake_api.requests == []


async def test_network_error_is_retried_then_raised(no_sleep):
    # 指向一个没有服务监听的端口
    client = DeepSeekToolsClient(
        lambda: "k",
        base_url="http://127.0.0.1:9",
        qps=0,
        sleep=no_sleep,
        max_retries=1,
        timeout=2,
    )
    with pytest.raises(DeepSeekToolsError) as info:
        await client.search("q")
    await client.close()
    assert info.value.status == 0
    assert no_sleep.calls == [1.0]


async def test_fetch(fake_api, no_sleep):
    fake_api.fetch_queue.append(
        (200, {"title": "文章", "content": "# 文章\n\n正文"}, {})
    )
    client = make_client(fake_api, no_sleep)
    page = await client.fetch(" https://example.com/a ")
    await client.close()
    assert fake_api.requests[0]["json"] == {"url": "https://example.com/a"}
    assert page.title == "文章" and page.content.startswith("# 文章")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://x.com/a",
        "example.com/a",
        "https://user:pw@x.com/",
        "https://x.com/" + "a" * 8200,
    ],
)
def test_validate_fetch_url_rejects(url):
    with pytest.raises(DeepSeekToolsError):
        validate_fetch_url(url)


async def test_rate_limiter_spaces_requests():
    now = [0.0]
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(round(s, 3))

    limiter = RateLimiter(5, clock=lambda: now[0], sleep=fake_sleep)
    await asyncio.gather(*(limiter.acquire() for _ in range(4)))
    assert sorted(sleeps) == [0.2, 0.4, 0.6]

    # 空闲足够久后不再等待
    now[0] = 10.0
    sleeps.clear()
    await limiter.acquire()
    assert sleeps == []
