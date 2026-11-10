from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import re
import unicodedata
from typing import Any

_TEXT_VARIANTS = {
    "Over head Crane": "Over Head Crane",
    "over head crane": "Over Head Crane",
    "경남 HC": "경남HC",
    "경남 H C": "경남HC",
    "일변번호": "일련번호",
}


def normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = re.sub(r"\s+", " ", text)
    text = _TEXT_VARIANTS.get(text, text)
    return text or None


def canonical_text(value: Any) -> str | None:
    text = normalize_text(value)
    if text is None:
        return None
    return re.sub(r"[\s\-_./,()\[\]:]+", "", text).casefold()


def normalize_number(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    text = normalize_text(value)
    if not text:
        return None
    neg = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = re.sub(r"[,\s원㎡m²평]", "", text, flags=re.IGNORECASE)
    if text in {"", "-"}:
        return None
    number = Decimal(text)
    if neg:
        number = -number
    return int(number) if number == number.to_integral_value() else float(number)


def normalize_money(value: Any) -> float | int | None:
    return normalize_number(value)


def normalize_area(value: Any) -> float | None:
    num = normalize_number(value)
    return None if num is None else float(num)


def normalize_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = normalize_text(value)
    if not text:
        return None
    text = text.replace("년", ".").replace("월", ".").replace("일", "")
    text = re.sub(r"\s+00:00:00$", "", text)
    text = text.strip().rstrip(".")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y. %m. %d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    m = re.match(r"^(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return text


def normalize_membership(value: Any, *, blank_policy: str = "unknown") -> str:
    text = canonical_text(value)
    if not text:
        return blank_policy
    if text in {"o", "포함", "y", "yes", "true", "1"}:
        return "included"
    if text in {"x", "미포함", "n", "no", "false", "0"}:
        return "not_included"
    return "unknown"


def almost_equal(a: Any, b: Any, tolerance: float = 0.0) -> bool:
    na = normalize_number(a)
    nb = normalize_number(b)
    if na is None or nb is None:
        return na is nb
    return abs(float(na) - float(nb)) <= tolerance
