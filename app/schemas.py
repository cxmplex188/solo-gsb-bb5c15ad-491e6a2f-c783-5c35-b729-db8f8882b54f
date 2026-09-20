from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


class FragmentIn(BaseModel):
    seq: int = Field(..., ge=1, description="片段序号，从 1 开始连续编号")
    revision: int = Field(..., ge=0, description="修订号，同一序号只接受更大的值")
    text: str = Field(..., description="字幕文本")


class FinalizedItem(BaseModel):
    """已定稿字幕片段。"""

    type: Literal["fragment"] = Field(
        "fragment", description="条目类型：fragment=字幕；gap=永久缺口标记"
    )
    seq: int
    revision: int
    text: str


class GapItem(BaseModel):
    """永久缺口标记：[start_seq, end_seq] 闭区间内的字幕已被裁决为永久丢失。"""

    type: Literal["gap"] = Field("gap", description="条目类型：fragment=字幕；gap=永久缺口标记")
    start_seq: int
    end_seq: int
    reason: Optional[str] = None


FinalizedEntry = Annotated[
    Union[FinalizedItem, GapItem],
    Field(discriminator="type", description="按序号顺序排列的字幕或缺口标记"),
]


class SubmitResponse(BaseModel):
    channel: str
    seq: int
    applied: bool = Field(..., description="本次提交是否生效（被接受并写入缓冲区）")
    reason: Optional[str] = Field(
        None,
        description=(
            "未生效原因：stale_revision=修订号不够新；already_finalized=序号已定稿；"
            "already_skipped=序号已被裁决为永久缺口"
        ),
    )
    watermark: int = Field(..., description="提交处理完成后的最新水位线")
    finalized: List[FinalizedItem] = Field(
        default_factory=list, description="本次提交触发新定稿的片段（缺口补齐时可能一次释放多条）"
    )


class GapRuleIn(BaseModel):
    start_seq: int = Field(..., ge=1, description="缺口起点（含），必须等于当前水位线下一位")
    end_seq: int = Field(..., ge=1, description="缺口终点（含），不得小于起点")
    reason: Optional[str] = Field(None, description="裁决备注，例如丢失原因")

    @model_validator(mode="after")
    def _check_range(self) -> "GapRuleIn":
        if self.end_seq < self.start_seq:
            raise ValueError("end_seq 不得小于 start_seq")
        return self


class GapRange(BaseModel):
    start_seq: int
    end_seq: int
    reason: Optional[str] = None


class GapRuleResponse(BaseModel):
    channel: str
    gap: GapRange = Field(..., description="本次裁决（或幂等命中）的缺口区间")
    idempotent: bool = Field(
        ..., description="false=本次完成裁决；true=该区间此前已完整裁决，本次幂等返回"
    )
    watermark: int = Field(..., description="处理完成后的最新水位线（幂等返回当前值）")
    skipped: List[GapRange] = Field(
        default_factory=list, description="本次跳过的序号区间（幂等重复时为当前已裁决结果）"
    )
    finalized: List[FinalizedItem] = Field(
        default_factory=list,
        description="本次裁决后在同一事务中紧随释放的连续缓冲片段（幂等重复时为当次释放的片段）",
    )


class FinalizedResponse(BaseModel):
    channel: str
    watermark: int
    items: List[FinalizedEntry] = Field(
        default_factory=list,
        description="按序号顺序排列的定稿字幕（type=fragment）与缺口标记（type=gap）",
    )
