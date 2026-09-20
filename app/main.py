from contextlib import asynccontextmanager

from fastapi import FastAPI, Query

from . import service
from .db import init_db
from .schemas import FinalizedResponse, FragmentIn, GapRuleIn, GapRuleResponse, SubmitResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时自动初始化数据库（建表幂等）
    init_db()
    yield


app = FastAPI(
    title="Live Subtitle Finalizer",
    summary="直播字幕定稿服务：乱序片段缓冲、按连续前缀推进水位线并定稿",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/channels/{channel}/fragments", response_model=SubmitResponse)
def submit_fragment(channel: str, body: FragmentIn) -> dict:
    """提交字幕片段。片段可乱序到达；同一序号只接受更高修订号。"""
    return service.submit_fragment(channel, body.seq, body.revision, body.text)


@app.post(
    "/channels/{channel}/gaps",
    response_model=GapRuleResponse,
    responses={
        409: {
            "description": "裁决校验失败：起点不匹配 / 区间内有缓冲片段 / 与既有区间部分重叠",
        }
    },
)
def rule_gap(channel: str, body: GapRuleIn) -> dict:
    """裁决永久缺口：跳过缺失序号区间，并在同一事务释放紧随其后的连续缓冲片段。

    重复提交已完整裁决的同一区间幂等返回当前结果；部分重叠返回 409。
    """
    return service.rule_gap(channel, body.start_seq, body.end_seq, body.reason)


@app.get("/channels/{channel}/finalized", response_model=FinalizedResponse)
def get_finalized(
    channel: str,
    after: int = Query(0, ge=0, description="只返回序号（片段序号 / 缺口起点）大于该值的条目"),
    limit: int = Query(1000, ge=1, le=10000),
) -> dict:
    """查询频道已定稿字幕、缺口标记与当前水位线（按序号顺序混合返回）。"""
    return service.get_finalized(channel, after=after, limit=limit)
