"""核心业务：片段接收与水位线推进。

不变式（由单事务保证）：
- finalized 中每个频道的序号恰好是 1..watermark 的连续前缀，无缺口的
  片段只存在于 fragments 缓冲区；
- 同一序号只保留最高修订号，定稿后不再接受任何修改。
"""

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
            # 已定稿的序号不可再修改
            reason = "already_finalized"
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

    遇到第一个缺口即停止，保证不会越过缺口定稿；缺口补齐的提交会
    在一次事务里释放整段连续片段。
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


def get_finalized(channel: str, after: int = 0, limit: int = 1000) -> dict:
    """查询频道定稿结果与当前水位线。after 用于增量拉取。"""
    with read_tx() as conn:
        row = conn.execute(
            "SELECT watermark FROM channels WHERE channel = ?", (channel,)
        ).fetchone()
        watermark = row["watermark"] if row else 0
        items = conn.execute(
            "SELECT seq, revision, text FROM finalized "
            "WHERE channel = ? AND seq > ? ORDER BY seq LIMIT ?",
            (channel, after, limit),
        ).fetchall()
        return {
            "channel": channel,
            "watermark": watermark,
            "items": [dict(item) for item in items],
        }
