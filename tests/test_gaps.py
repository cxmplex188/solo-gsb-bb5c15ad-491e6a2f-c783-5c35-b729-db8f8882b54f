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


def rule_gap(client, channel, start_seq, end_seq, reason=None):
    return client.post(
        f"/channels/{channel}/gaps",
        json={"start_seq": start_seq, "end_seq": end_seq, "reason": reason},
    )


def test_rule_gap_skips_missing_prefix_and_releases_buffered_run(client):
    # 1,2 已正常定稿；4,5 已缓冲；3 永久丢失
    submit(client, "c1", 1, 1, "one")
    submit(client, "c1", 2, 1, "two")
    submit(client, "c1", 4, 1, "four")
    submit(client, "c1", 5, 1, "five")

    resp = rule_gap(client, "c1", 3, 3, "uplink-loss")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["idempotent"] is False
    assert body["watermark"] == 5
    assert body["skipped"] == [
        {"start_seq": 3, "end_seq": 3, "reason": "uplink-loss"}
    ]
    # 紧随缺口的连续缓冲片段在同一事务中被释放
    assert [i["seq"] for i in body["finalized"]] == [4, 5]
    assert body["finalized"][0]["text"] == "four"

    resp = client.get("/channels/c1/finalized").json()
    assert resp["watermark"] == 5
    assert [(i["type"], i.get("seq", i.get("start_seq"))) for i in resp["items"]] == [
        ("fragment", 1),
        ("fragment", 2),
        ("gap", 3),
        ("fragment", 4),
        ("fragment", 5),
    ]
    gap = next(i for i in resp["items"] if i["type"] == "gap")
    assert gap == {"type": "gap", "start_seq": 3, "end_seq": 3, "reason": "uplink-loss"}


def test_rule_gap_from_zero_releases_run_after_range(client):
    # 频道一开始就裁决 1..3 丢失，4,5 已缓冲
    submit(client, "c1", 4, 1)
    submit(client, "c1", 5, 1)
    body = rule_gap(client, "c1", 1, 3).json()
    assert body["watermark"] == 5
    assert body["skipped"] == [{"start_seq": 1, "end_seq": 3, "reason": None}]
    assert [i["seq"] for i in body["finalized"]] == [4, 5]


def test_rule_gap_without_trailing_buffered_fragments(client):
    submit(client, "c1", 1, 1)
    body = rule_gap(client, "c1", 2, 4, reason="gone").json()
    assert body["watermark"] == 4
    assert [i["seq"] for i in body["finalized"]] == []
    assert body["skipped"][0] == {"start_seq": 2, "end_seq": 4, "reason": "gone"}

    # 之后正常片段到达可继续定稿
    body = submit(client, "c1", 5, 1, "five")
    assert body["watermark"] == 5
    assert [i["seq"] for i in body["finalized"]] == [5]


def test_gap_start_must_equal_next_after_watermark(client):
    submit(client, "c1", 1, 1)
    # 水位线 1，下一位必须是 2；裁决 3..3 拒绝
    resp = rule_gap(client, "c1", 3, 3)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "gap_start_mismatch"
    assert detail["watermark"] == 1
    assert detail["expected_start_seq"] == 2

    # 校验失败无任何写入：水位线不变、无缺口记录
    assert service.get_finalized("c1")["watermark"] == 1
    assert client.get("/channels/c1/finalized").json()["items"] == [
        {"type": "fragment", "seq": 1, "revision": 1, "text": "t"}
    ]


def test_gap_range_with_buffered_fragments_is_rejected_without_writes(client, conn):
    # 水位线停在 1：2 缺失（阻塞缺口），3 已提前缓冲 —— 裁决区间不得覆盖已缓冲的 3
    submit(client, "c1", 3, 1, "buffered-three")  # 先到
    submit(client, "c1", 1, 1)                    # 释放 1 后停住，3 留在缓冲区
    submit(client, "c1", 5, 1)

    resp = rule_gap(client, "c1", 2, 3)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "gap_has_buffered"
    assert detail["buffered_seqs"] == [3]

    # 零写入：水位线仍为 1，缓冲片段 3、5 仍在，gaps 表为空
    assert conn.execute(
        "SELECT watermark FROM channels WHERE channel = 'c1'"
    ).fetchone()["watermark"] == 1
    assert [
        r["seq"]
        for r in conn.execute(
            "SELECT seq FROM fragments WHERE channel = 'c1' ORDER BY seq"
        ).fetchall()
    ] == [3, 5]
    assert conn.execute("SELECT COUNT(*) AS c FROM gaps").fetchone()["c"] == 0


def test_exact_same_gap_is_idempotent(client):
    submit(client, "c1", 1, 1)
    submit(client, "c1", 3, 1, "three")
    first = rule_gap(client, "c1", 2, 2, reason="first-note").json()
    assert first["idempotent"] is False
    assert first["watermark"] == 3
    assert [i["seq"] for i in first["finalized"]] == [3]

    # 水位线继续推进后，重复提交同一区间
    submit(client, "c1", 4, 1, "four")

    again = rule_gap(client, "c1", 2, 2, reason="different-note").json()
    assert again["idempotent"] is True
    assert again["gap"] == {"start_seq": 2, "end_seq": 2, "reason": "first-note"}
    assert again["skipped"] == [
        {"start_seq": 2, "end_seq": 2, "reason": "first-note"}
    ]
    # 返回当前水位线，并还原该裁决当次释放的片段（仅 3，不含之后的 4）
    assert again["watermark"] == 4
    assert [i["seq"] for i in again["finalized"]] == [3]


def test_partial_overlap_gap_is_rejected(client):
    submit(client, "c1", 1, 1)
    assert rule_gap(client, "c1", 2, 3).status_code == 200

    # 与 [2,3] 部分重叠的各种区间均拒绝（同起点更宽、越界延伸、内部子区间）
    for start, end in ((2, 4), (1, 2), (3, 5), (2, 2)):
        resp = rule_gap(client, "c1", start, end)
        assert resp.status_code == 409, (start, end)
        assert resp.json()["detail"]["reason"] == "gap_overlap"

    # 紧邻但不重叠的区间允许裁决
    resp = rule_gap(client, "c1", 4, 4)
    assert resp.status_code == 200
    assert resp.json()["skipped"][0] == {"start_seq": 4, "end_seq": 4, "reason": None}


def test_validation_rejects_inverted_range_and_bad_seqs(client):
    submit(client, "c1", 1, 1)
    resp = rule_gap(client, "c1", 5, 2)
    assert resp.status_code == 422
    resp = rule_gap(client, "c1", 0, 2)
    assert resp.status_code == 422


def test_late_fragment_for_skipped_seq_is_rejected(client):
    submit(client, "c1", 1, 1)
    rule_gap(client, "c1", 2, 2)

    body = submit(client, "c1", 2, 1, "late")
    assert body["applied"] is False
    assert body["reason"] == "already_skipped"
    assert body["watermark"] == 2  # 裁决水位未受影响

    # 缓冲区内也不应残留该片段
    resp = client.get("/channels/c1/finalized").json()
    assert [i for i in resp["items"] if i["type"] == "fragment"] == [
        {"type": "fragment", "seq": 1, "revision": 1, "text": "t"}
    ]


def test_finalized_query_interleaves_fragments_and_gaps_in_order(client):
    submit(client, "c1", 1, 1, "a")
    rule_gap(client, "c1", 2, 2, "lost")
    submit(client, "c1", 3, 1, "c")
    submit(client, "c1", 4, 1, "d")
    rule_gap(client, "c1", 5, 6, "lost-2")
    submit(client, "c1", 7, 1, "g")

    resp = client.get("/channels/c1/finalized").json()
    assert resp["watermark"] == 7

    def order_key(i):
        return i["seq"] if i["type"] == "fragment" else (i["start_seq"], i["end_seq"])

    assert [(i["type"], order_key(i)) for i in resp["items"]] == [
        ("fragment", 1),
        ("gap", (2, 2)),
        ("fragment", 3),
        ("fragment", 4),
        ("gap", (5, 6)),
        ("fragment", 7),
    ]

    # after 增量拉取对两类条目都生效（缺口以起点序号参与比较）
    resp = client.get("/channels/c1/finalized", params={"after": 4}).json()
    assert [i["type"] for i in resp["items"]] == ["gap", "fragment"]


def test_concurrent_gap_rules_and_fragments_keep_invariants(conn):
    """并发混合：同一缺口被多线程并发裁决 + 乱序片段提交。

    裁决只成功一次，其余要么幂等命中、要么在水位线越过后被拒绝；
    最终状态必须满足“片段与缺口共同覆盖 1..watermark”的不变式。
    """
    channel = "stress"

    def fragment_job(seq):
        return service.submit_fragment(channel, seq, 1, f"seq{seq}")

    def rule_job():
        return service.rule_gap(channel, 3, 5, "lost")

    def outcome(fn):
        try:
            return ("ok", fn())
        except Exception as exc:  # noqa: BLE001 - 并发裁决失败是预期分支
            return ("rejected", getattr(exc, "status_code", None))

    initial = [1, 2, 6, 7, 8]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fragment_job, initial))
        jobs = [rule_job] * 12 + [lambda: fragment_job(9)]
        outcomes = list(pool.map(lambda f: outcome(f), jobs))

    rulings = [r for kind, r in outcomes if kind == "ok" and r.get("skipped")]
    rejects = [code for kind, code in outcomes if kind == "rejected"]
    assert sum(1 for r in rulings if not r["idempotent"]) == 1
    assert any(r["idempotent"] for r in rulings)  # 有并发重复请求被幂等消化
    assert all(code == 409 for code in rejects)  # 其余被原子拒绝

    state = service.get_finalized(channel, limit=10000)
    assert state["watermark"] == 9

    def order_key(i):
        return i["seq"] if i["type"] == "fragment" else i["start_seq"]

    assert [(i["type"], order_key(i)) for i in state["items"]] == [
        ("fragment", 1),
        ("fragment", 2),
        ("gap", 3),
        ("fragment", 6),
        ("fragment", 7),
        ("fragment", 8),
        ("fragment", 9),
    ]
    # 缓冲区内不得残留 ≤ 水位线的片段
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM fragments WHERE channel = ? AND seq <= 9",
        (channel,),
    ).fetchone()["c"] == 0
