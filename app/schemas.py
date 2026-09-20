from typing import List, Optional

from pydantic import BaseModel, Field


class FragmentIn(BaseModel):
    seq: int = Field(..., ge=1, description="片段序号，从 1 开始连续编号")
    revision: int = Field(..., ge=0, description="修订号，同一序号只接受更大的值")
    text: str = Field(..., description="字幕文本")


class FinalizedItem(BaseModel):
    seq: int
    revision: int
    text: str


class SubmitResponse(BaseModel):
    channel: str
    seq: int
    applied: bool = Field(..., description="本次提交是否生效（被接受并写入缓冲区）")
    reason: Optional[str] = Field(
        None,
        description="未生效原因：stale_revision=修订号不够新；already_finalized=序号已定稿",
    )
    watermark: int = Field(..., description="提交处理完成后的最新水位线")
    finalized: List[FinalizedItem] = Field(
        default_factory=list, description="本次提交触发新定稿的片段（缺口补齐时可能一次释放多条）"
    )


class FinalizedResponse(BaseModel):
    channel: str
    watermark: int
    items: List[FinalizedItem]
