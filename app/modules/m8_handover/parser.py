import re
from unicodedata import normalize

from app.errors import LedgerError

RANGE = re.compile(r"([0-9]{1,4})(?:\s*[-–—~至]\s*([0-9]{1,4}))?")


def parse_cover(volume, issue_first, issue_last):
    """把来函卷期字面值（允许全角与前导零）规范化为整数卷号与期位范围。"""
    volume_text = normalize("NFKC", str(volume)).strip().removeprefix("第").removesuffix("卷").strip()
    if not re.fullmatch(r"[0-9]{1,4}", volume_text) or int(volume_text) == 0:
        raise LedgerError("BAD_VOLUME", "卷号须为 1 至 9999")
    first_text = normalize("NFKC", str(issue_first)).strip().removeprefix("第").removesuffix("期").strip()
    match = RANGE.fullmatch(first_text)
    if not match:
        raise LedgerError("BAD_ISSUE", "期号须为正整数或同卷连续范围")
    first = int(match[1])
    last_text = None
    if issue_last is not None:
        last_text = normalize("NFKC", str(issue_last)).strip().removeprefix("第").removesuffix("期").strip()
    if last_text:
        end_match = RANGE.fullmatch(last_text)
        if not end_match:
            raise LedgerError("BAD_ISSUE", "期号须为正整数或同卷连续范围")
        last = int(end_match[1])
    else:
        last = int(match[2]) if match[2] is not None else first
    if first < 1 or last < first:
        raise LedgerError("ISSUE_ORDER", "期号须为正数且范围递增")
    return {"volume": int(volume_text), "first": first, "last": last}
