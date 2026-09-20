import random
from concurrent.futures import ThreadPoolExecutor

from app import service


def submit(client, channel, seq, revision, text="t"):
    resp = client.post(
        f"/channels/{channel}/fragments",
        json={"seq": seq, "revision": revision, "text": text},
    )
    assert resp.status_code == 200
    return resp.json()


def test_in_order_submission_finalizes_immediately(client):
    body = submit(client, "c1", 1, 1, "hello")
    assert body["applied"] is True
    assert body["watermark"] == 1
    assert [i["seq"] for i in body["finalized"]] == [1]
    assert body["finalized"][0]["text"] == "hello"


def test_out_of_order_waits_for_gap_then_releases_batch(client):
    body = submit(client, "c1", 2, 1, "second")
    assert body["applied"] is True
    assert body["watermark"] == 0
    assert body["finalized"] == []

    body = submit(client, "c1", 1, 1, "first")
    assert body["watermark"] == 2
    assert [i["seq"] for i in body["finalized"]] == [1, 2]
    assert [i["text"] for i in body["finalized"]] == ["first", "second"]


def test_gap_blocks_prefix_and_fill_releases_all(client):
    assert submit(client, "c1", 1, 1)["watermark"] == 1
    assert submit(client, "c1", 2, 1)["watermark"] == 2
    body = submit(client, "c1", 4, 1)
    assert body["watermark"] == 2  # 缺口 3 未补齐，不能越过
    assert body["finalized"] == []

    body = submit(client, "c1", 3, 1)
    assert body["watermark"] == 4
    assert [i["seq"] for i in body["finalized"]] == [3, 4]


def test_higher_revision_replaces_pending_fragment(client):
    submit(client, "c1", 2, 1, "draft")
    body = submit(client, "c1", 2, 5, "revised")
    assert body["applied"] is True

    body = submit(client, "c1", 1, 1, "first")
    assert body["watermark"] == 2
    assert body["finalized"][1]["revision"] == 5
    assert body["finalized"][1]["text"] == "revised"


def test_stale_or_equal_revision_rejected(client):
    submit(client, "c1", 2, 5, "v5")
    for rev in (5, 3, 0):
        body = submit(client, "c1", 2, rev, "stale")
        assert body["applied"] is False
        assert body["reason"] == "stale_revision"
        assert body["watermark"] == 0


def test_finalized_seq_is_immutable(client):
    submit(client, "c1", 1, 1, "final")
    body = submit(client, "c1", 1, 99, "too late")
    assert body["applied"] is False
    assert body["reason"] == "already_finalized"
    assert body["watermark"] == 1

    resp = client.get("/channels/c1/finalized").json()
    assert resp["items"][0]["text"] == "final"


def test_get_finalized_with_incremental_after(client):
    for seq in (1, 2, 3):
        submit(client, "c1", seq, 1, f"line{seq}")

    resp = client.get("/channels/c1/finalized").json()
    assert resp["watermark"] == 3
    assert [i["seq"] for i in resp["items"]] == [1, 2, 3]

    resp = client.get("/channels/c1/finalized", params={"after": 1}).json()
    assert [i["seq"] for i in resp["items"]] == [2, 3]

    resp = client.get("/channels/unknown/finalized").json()
    assert resp["watermark"] == 0
    assert resp["items"] == []


def test_channels_are_isolated(client):
    submit(client, "a", 2, 1)
    assert submit(client, "b", 1, 1)["watermark"] == 1
    assert submit(client, "a", 2, 1)["watermark"] == 0  # a 的缺口仍在


def test_validation_rejects_bad_input(client):
    resp = client.post("/channels/c1/fragments", json={"seq": 0, "revision": 1, "text": "x"})
    assert resp.status_code == 422
    resp = client.post("/channels/c1/fragments", json={"seq": 1, "revision": -1, "text": "x"})
    assert resp.status_code == 422


def test_concurrent_out_of_order_submissions(conn):
    """多线程乱序提交 + 重复修订竞争：最终定稿必须是 1..N 的连续前缀。"""
    n = 300
    channel = "stress"
    jobs = []
    for seq in range(1, n + 1):
        jobs.append((seq, 1, f"seq{seq}-v1"))
        jobs.append((seq, 2, f"seq{seq}-v2"))  # 更高修订号竞争同一片段
    random.Random(42).shuffle(jobs)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda j: service.submit_fragment(channel, *j), jobs))

    assert all(r["watermark"] <= n for r in results)
    assert service.get_finalized(channel)["watermark"] == n

    rows = conn.execute(
        "SELECT seq, revision, text FROM finalized WHERE channel = ? ORDER BY seq",
        (channel,),
    ).fetchall()
    assert len(rows) == n  # 无重复定稿
    assert [r["seq"] for r in rows] == list(range(1, n + 1))  # 连续前缀，无缺口
    # 每个序号定稿的是定稿时刻已收到的最高修订号（rev2 若晚于定稿到达则被拒绝）
    assert all(r["revision"] in (1, 2) for r in rows)
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM fragments WHERE channel = ?", (channel,)
    ).fetchone()["c"] == 0  # 缓冲区已清空
