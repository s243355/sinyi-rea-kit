# -*- coding: utf-8 -*-
"""「行情 南屯區」「行情 語山簷」這類查詢：判斷是在問行政區，還是在問社區／建案名稱。純邏輯，不碰網路。"""
from __future__ import annotations

import re

from core.tw_districts import DISTRICTS

COMMANDS = ("行情", "實價", "實登", "成交")
COUNTIES = tuple(DISTRICTS)
_SHORT = {c[:-1]: c for c in COUNTIES if c.endswith(("市", "縣"))}      # 台中→台中市、彰化→彰化縣


def split_command(text: str) -> str | None:
    """「行情 南屯區」→「南屯區」；不是查行情的指令回 None；只打「行情」回空字串。"""
    t = (text or "").strip()
    for cmd in COMMANDS:
        if t == cmd:
            return ""
        if t.startswith(cmd) and (len(t) > len(cmd)) and t[len(cmd)] in " 　:：":
            return t[len(cmd) + 1:].strip()
    return None


def parse(query: str, default_county: str | None = None) -> tuple[str, str, str | None]:
    """回 ("area", 縣市, 行政區或 None) 或 ("name", 查詢字, None)。

    - 「台中市南屯區」「臺中南屯區」「台中 南屯」→ 行政區
    - 「南屯區」「南屯」→ 用預設縣市補（預設縣市沒有這區就當名稱查）
    - 「台中市」→ 整個縣市
    - 其他（社區名、建案名、路名）→ 名稱
    """
    q = re.sub(r"\s+", "", (query or "").replace("臺", "台"))
    if not q:
        return "name", "", None
    county, rest = None, q
    for c in COUNTIES:
        if q.startswith(c):
            county, rest = c, q[len(c):]
            break
    else:
        for short, full in _SHORT.items():
            if q.startswith(short) and (q == short or _district(full, q[len(short):])):
                county, rest = full, q[len(short):]
                break
    if county:
        if not rest:
            return "area", county, None
        town = _district(county, rest)
        return ("area", county, town) if town else ("name", query.strip(), None)
    if default_county in DISTRICTS:
        town = _district(default_county, q)
        if town:
            return "area", default_county, town
    return "name", query.strip(), None


def _district(county: str, text: str) -> str | None:
    if not text:
        return None
    names = DISTRICTS.get(county, ())
    if text in names:
        return text
    hits = [d for d in names if d[:-1] == text]          # 「南屯」→「南屯區」、「員林」→「員林市」
    return hits[0] if len(hits) == 1 else None
