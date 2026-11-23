from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.modules.m1_orders.models import Year

Key = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=70)]
LetterNo = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class Notice(BaseModel):
    """出版方停刊/复刊来函一件，可覆盖同卷连续的一个或多个期位。"""

    model_config = ConfigDict(extra="forbid")
    letter_no: LetterNo
    kind: Literal["suspension", "resumption"]
    effect: Literal["pause", "terminate"] = "pause"
    chain: Key
    year: Year
    volume: Text
    issue_first: Text
    issue_last: Text | None = None
    title: Text | None = None
    issued_on: Text | None = None


class NoticeBatch(BaseModel):
    """分批补送的来函；rules 仅在首批或需要随附时提供，随后沿用首批锁定的订购规则。"""

    model_config = ConfigDict(extra="forbid")
    notices: list
    rules: list[dict] | None = None
    note: Text | None = None
