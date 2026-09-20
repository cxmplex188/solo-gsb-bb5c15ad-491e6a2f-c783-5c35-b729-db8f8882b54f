"""核心业务：片段接收、永久缺口裁决与水位线推进。

不变式（由单事务保证）：
- 已“处理”的序号 1..watermark 由定稿字幕（finalized）与永久缺口
  （gaps，闭区间互不重叠）共同覆盖；无缺口的待处理片段只存在于
  fragments 缓冲区；
- 缺口区间只能从 watermark+1 开始裁决，区间内不得有已缓冲片段；
  裁决后水位线跳过该区间，并在同一事务内继续释放紧随其后的连续片段；
- 同一序号只保留最高修订号，定稿或被裁决跳过后不再接受任何修改。
"""

from fastapi import HTTPException

from .db import read_tx, write_tx


def submit_fragment(channel: str, seq: int, revision: int, text: str) -> dict:
    """提交一个字幕片段，返回是否生效、最新水位线及本次新定稿的片段。"""
    with write_tx() as conn:
        conn.execute(
            "INSERT INTO channels (channel, watermark) VALUES (?, 0) "
            "ON CONFLICT(channel) DO NOTHING",
            (channel,),
        )
        watermark = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()["watermark"]

        applied = False
        reason = None
        if seq <= watermark:
            # 已处理的序号不可再修改：可能是已定稿，也可能已被裁决为永久缺口
            in_gap = conn.execute(
                "SELECT 1 FROM gaps WHERE channel = ? AND start_seq <= ? AND end_seq >= ?",
                (channel, seq, seq),
            ).fetchone()
            reason = "already_skipped" if in_gap else "already_finalized"
        else:
            row = conn.execute(
                "SELECT revision FROM fragments WHERE channel = ? AND seq = ?",
                (channel, seq),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO fragments (channel, seq, revision, text) "
                    "VALUES (?, ?, ?, ?)",
                    (channel, seq, revision, text),
                )
                applied = True
            elif revision > row["revision"]:
                conn.execute(
                    "UPDATE fragments SET revision = ?, text = ? "
                    "WHERE channel = ? AND seq = ?",
                    (revision, text, channel, seq),
                )
                applied = True
            else:
                reason = "stale_revision"

        newly = _advance_watermark(conn, channel, watermark)
        new_watermark = newly[-1]["seq"] if newly else watermark

        return {
            "channel": channel,
            "seq": seq,
            "applied": applied,
            "reason": reason,
            "watermark": new_watermark,
            "finalized": newly,
        }


def _advance_watermark(conn, channel: str, watermark: int) -> list:
    """从 watermark+1 起消费连续前缀：移入定稿表并更新水位线。

    遇到第一个缺口即停止，保证不会越过缺口定稿；缺口补齐（或被裁决
    跳过）后的提交会在一次事务里释放整段连续片段。
    """
    newly = []
    seq = watermark + 1
    while True:
        row = conn.execute(
            "SELECT revision, text FROM fragments WHERE channel = ? AND seq = ?",
            (channel, seq),
        ).fetchone()
        if row is None:
            break
        conn.execute(
            "INSERT INTO finalized (channel, seq, revision, text) VALUES (?, ?, ?, ?)",
            (channel, seq, row["revision"], row["text"]),
        )
        conn.execute(
            "DELETE FROM fragments WHERE channel = ? AND seq = ?", (channel, seq)
        )
        newly.append({"seq": seq, "revision": row["revision"], "text": row["text"]})
        seq += 1
    if newly:
        conn.execute(
            "UPDATE channels SET watermark = ? WHERE channel = ?",
            (newly[-1]["seq"], channel),
        )
    return newly


def rule_gap(channel: str, start_seq: int, end_seq: int, reason: str | None) -> dict:
    """裁决永久缺口 [start_seq, end_seq]（闭区间）。

    校验（全部在同一个 BEGIN IMMEDIATE 事务内完成，失败即 ROLLBACK，
    不产生任何写入，失败返回 409）：
    - start_seq 必须等于当前水位线下一位（gap_start_mismatch）；
    - 区间内不得存在已缓冲片段（gap_has_buffered）；
    - 与既有裁决区间不得部分重叠（gap_overlap）；完全相同的区间
      幂等返回当前结果。

    成功后在同一事务内记录跳过区间、把水位线推进到 end_seq，
    并继续释放紧随其后的连续缓冲片段。
    """
    with write_tx() as conn:
        # 进入 IMMEDIATE 事务即持有写锁，校验与写入原子完成，杜绝 TOCTOU
        conn.execute(
            "INSERT INTO channels (channel, watermark) VALUES (?, 0) "
            "ON CONFLICT(channel) DO NOTHING",
            (channel,),
        )
        watermark = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()["watermark"]

        overlap = conn.execute(
            "SELECT start_seq, end_seq, reason, release_until FROM gaps "
            "WHERE channel = ? AND start_seq <= ? AND end_seq >= ? "
            "ORDER BY start_seq",
            (channel, end_seq, start_seq),
        ).fetchall()

        exact = next(
            (
                g
                for g in overlap
                if g["start_seq"] == start_seq and g["end_seq"] == end_seq
            ),
            None,
        )
        if exact is not None:
            # 完整裁决过的同一区间：幂等还原当次结果，空事务提交即无写入
            return _idempotent_gap_result(conn, channel, exact)

        if overlap:
            g = overlap[0]
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "gap_overlap",
                    "message": "缺口区间与已裁决区间部分重叠",
                    "conflicting_gap": {
                        "start_seq": g["start_seq"],
                        "end_seq": g["end_seq"],
                    },
                },
            )

        if start_seq != watermark + 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "gap_start_mismatch",
                    "message": "缺口起点必须等于当前水位线下一位",
                    "watermark": watermark,
                    "expected_start_seq": watermark + 1,
                },
            )

        buffered = [
            r["seq"]
            for r in conn.execute(
                "SELECT seq FROM fragments WHERE channel = ? AND seq BETWEEN ? AND ? "
                "ORDER BY seq",
                (channel, start_seq, end_seq),
            ).fetchall()
        ]
        if buffered:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "gap_has_buffered",
                    "message": "缺口区间内存在已缓冲片段，不得裁决为永久缺口",
                    "buffered_seqs": buffered,
                },
            )

        # 先把水位线跳过整个缺口区间
        conn.execute(
            "UPDATE channels SET watermark = ? WHERE channel = ?",
            (end_seq, channel),
        )
        # 再在同一事务内释放紧随其后的连续缓冲片段
        newly = _advance_watermark(conn, channel, end_seq)
        new_watermark = newly[-1]["seq"] if newly else end_seq

        conn.execute(
            "INSERT INTO gaps (channel, start_seq, end_seq, reason, release_until) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, start_seq, end_seq, reason, new_watermark),
        )

        gap = {"start_seq": start_seq, "end_seq": end_seq, "reason": reason}
        return {
            "channel": channel,
            "gap": gap,
            "idempotent": False,
            "watermark": new_watermark,
            "skipped": [gap],
            "finalized": newly,
        }


def _idempotent_gap_result(conn, channel: str, gap_row) -> dict:
    """完整重复裁决：返回当前水位线及该裁决当次释放的定稿片段，无写入。"""
    start_seq, end_seq, release_until = (
        gap_row["start_seq"],
        gap_row["end_seq"],
        gap_row["release_until"],
    )
    watermark = conn.execute(
        "SELECT watermark FROM channels WHERE channel = ?", (channel,)
    ).fetchone()["watermark"]
    released = conn.execute(
        "SELECT seq, revision, text FROM finalized "
        "WHERE channel = ? AND seq > ? AND seq <= ? ORDER BY seq",
        (channel, end_seq, release_until),
    ).fetchall()

    # 以已持久化的裁决备注为准（重复提交不修改既有记录）
    gap = {"start_seq": start_seq, "end_seq": end_seq, "reason": gap_row["reason"]}
    return {
        "channel": channel,
        "gap": gap,
        "idempotent": True,
        "watermark": watermark,
        "skipped": [gap],
        "finalized": [dict(r) for r in released],
    }


def get_finalized(channel: str, after: int = 0, limit: int = 1000) -> dict:
    """查询频道定稿结果与当前水位线，按序号顺序混合返回字幕与缺口标记。

    after 用于增量拉取：只返回序号（片段序号 / 缺口起点）大于该值的条目。
    """
    with read_tx() as conn:
        row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = row["watermark"] if row else 0

        fragments = conn.execute(
            "SELECT seq, revision, text FROM finalized "
            "WHERE channel = ? AND seq > ? ORDER BY seq",
            (channel, after),
        ).fetchall()
        gaps = conn.execute(
            "SELECT start_seq, end_seq, reason FROM gaps "
            "WHERE channel = ? AND start_seq > ? ORDER BY start_seq",
            (channel, after),
        ).fetchall()

        # 双路归并：两类序号空间互不相交（缺口区间内不会有定稿片段）
        items = []
        fi = gi = 0
        while len(items) < limit and (fi < len(fragments) or gi < len(gaps)):
            if gi >= len(gaps) or (fi < len(fragments) and fragments[fi]["seq"] < gaps[gi]["start_seq"]):
                r = fragments[fi]
                items.append(
                    {"type": "fragment", "seq": r["seq"], "revision": r["revision"], "text": r["text"]}
                )
                fi += 1
            else:
                g = gaps[gi]
                items.append(
                    {
                        "type": "gap",
                        "start_seq": g["start_seq"],
                        "end_seq": g["end_seq"],
                        "reason": g["reason"],
                    }
                )
                gi += 1

        return {
            "channel": channel,
            "watermark": watermark,
            "items": items,
        }
