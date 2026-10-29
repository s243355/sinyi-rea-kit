"""縣市／行政區點選卡（LINE Flex），找新買方與替買方找房共用。

按鈕用 postback、不帶 displayText：點了聊天室不會多出一行使用者訊息，卡片留在畫面上可以繼續點。
每次點選後只回一則簡短的「目前已選」，附快速回覆「下一步」。
postback 格式：ap=<動作>&p=<用途>&v=<值>[&k=<縣市>]
  c＝切換縣市（值＝縣市名）  all＝全台  next＝選完縣市
  d＝切換行政區（值＝在該縣市中的序號）；值為 * ＝全區，直接換下一個縣市（不是切換，點兩次不會取消）
  done＝這個縣市選好了
  k＝這張卡屬於哪個縣市：舊卡片上的按鈕只改它自己的縣市，不會改到現在正在選的那個
"""
from __future__ import annotations

import urllib.parse
from typing import Iterable

from core.tw_districts import COUNTIES, DISTRICTS, WHOLE
from core import brand

INK = brand.INK
BAR = brand.GREEN
PER_BUBBLE = 24          # 每張卡最多 24 個行政區（8 列 × 3），避免單張卡超過 LINE 的大小上限
TITLES = {"prospect": "選服務縣市", "intake": "客人想找哪些縣市", "listing": "物件在哪個縣市", "tax": "土地在哪個縣市"}

# 快速回覆送出的文字＝等同按卡片上的鈕（給點不到卡片、或習慣打字的人）
NEXT_TEXT, ALL_TEXT, RESET_TEXT = "下一步：選區域", "全台", "重選縣市"
DONE_TEXT, WHOLE_TEXT = "這個縣市選好了", "全區"
TEXT_OPS = {NEXT_TEXT: ("next", ""), ALL_TEXT: ("all", ""), DONE_TEXT: ("done", ""),
            WHOLE_TEXT: ("d", WHOLE), RESET_TEXT: ("reset", "")}


def data(op: str, purpose: str, value: str = "", county: str = "") -> str:
    return urllib.parse.urlencode({"ap": op, "p": purpose, "v": value, **({"k": county} if county else {})})


def parse(raw: str) -> dict | None:
    q = {k: v[0] for k, v in urllib.parse.parse_qs(str(raw or "")).items() if v}
    return q if "ap" in q else None


def _button(label: str, payload: str, selected: bool) -> dict:
    return {"type": "button", "height": "sm", "flex": 1,
            "style": "primary" if selected else "secondary",
            **({"color": BAR} if selected else {}),
            "action": {"type": "postback", "label": (("✓" if selected else "") + label)[:20], "data": payload}}


def _grid(buttons: list[dict], cols: int = 3) -> list[dict]:
    rows = []
    for i in range(0, len(buttons), cols):
        row = buttons[i:i + cols]
        while len(row) < cols:
            row.append({"type": "box", "layout": "vertical", "contents": [], "flex": 1})   # 補位，讓最後一列對齊
        rows.append({"type": "box", "layout": "horizontal", "spacing": "xs", "contents": row})
    return rows


def _bubble(title: str, note: str, rows: list[dict], footer: list[dict]) -> dict:
    return {"type": "bubble", "size": "giga",
            "header": {"type": "box", "layout": "vertical", "backgroundColor": BAR, "paddingAll": "12px",
                       "contents": [{"type": "text", "text": title, "color": "#FFFFFF", "weight": "bold",
                                     "size": "md", "wrap": True},
                                    {"type": "text", "text": note, "color": "#FFFFFF", "size": "xs", "wrap": True}]},
            "body": {"type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "10px", "contents": rows},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "10px", "contents": footer}}


def county_card(selected: Iterable[str], purpose: str, *, single: bool = False) -> dict:
    """single＝只能選一個（例：一間物件只會在一個縣市），點了直接到行政區卡。"""
    chosen = set(selected)
    buttons = [_button(c, data("c", purpose, c), c in chosen) for c in COUNTIES]
    if single:
        title = TITLES.get(purpose, "選縣市")
        bubble = _bubble(title, "點一個縣市，接著選行政區。", _grid(buttons), [])
        bubble.pop("footer")
        return {"type": "flex", "altText": title, "contents": bubble}
    footer = [_button("全台（所有縣市）", data("all", purpose), False),
              {"type": "button", "style": "primary", "height": "sm", "color": INK,
               "action": {"type": "postback", "label": "選好了，下一步", "data": data("next", purpose)}}]
    title = TITLES.get(purpose, "選縣市") + "（可複選）"
    bubble = _bubble(title, "點一下選取，再點一次取消；選好按「下一步」。", _grid(buttons), footer)
    return {"type": "flex", "altText": title, "contents": bubble}


def district_card(county: str, selected: Iterable[str], purpose: str, *, position: str = "",
                  single: bool = False) -> dict:
    chosen = set(selected)
    names = DISTRICTS[county]
    whole = _button(f"全區（整個{county}）→ 下一步", data("d", purpose, WHOLE, county), WHOLE in chosen)
    done = {"type": "button", "style": "primary", "height": "sm", "color": INK,
            "action": {"type": "postback", "label": "這個縣市選好了", "data": data("done", purpose, "", county)}}
    back = _button("← 換縣市", data("reset", purpose, "", county), False)
    bubbles = []
    for start in range(0, len(names), PER_BUBBLE):
        part = names[start:start + PER_BUBBLE]
        buttons = [_button(n, data("d", purpose, str(start + i), county), n in chosen) for i, n in enumerate(part)]
        if single:
            title, note, footer = f"{county}：在哪一區？", "點一區就好。", [back]
        else:
            title = f"{county}：選區域（可複選）" + (f" {position}" if position else "")
            note, footer = "要哪幾區就點哪幾區，點錯再點一次取消。整個縣市都要→按「全區」，直接下一步。", [whole, done]
        if len(names) > PER_BUBBLE:
            note += f"（第 {start // PER_BUBBLE + 1} 頁，往左滑看更多）"
        bubbles.append(_bubble(title, note, _grid(buttons), footer))
    contents = bubbles[0] if len(bubbles) == 1 else {"type": "carousel", "contents": bubbles}
    return {"type": "flex", "altText": f"{county}：在哪一區？" if single else f"{county}：選區域", "contents": contents}


def new_pick(*, single: bool = False) -> dict:
    return {"step": "city", "cities": [], "areas": {}, **({"single": True} if single else {})}


def card(pick: dict, purpose: str) -> dict:
    """目前這一步該給的卡：縣市卡，或「第幾個縣市」的行政區卡。"""
    single = bool(pick.get("single"))
    if pick["step"] == "city":
        return county_card(pick.get("cities", []), purpose, single=single)
    county = pick["cities"][pick["cursor"]]
    position = "" if single else f"（{pick['cursor'] + 1}/{len(pick['cities'])}）"
    return district_card(county, pick["areas"].get(county, []), purpose, position=position, single=single)


def quick(pick: dict) -> tuple[str, ...]:
    return (NEXT_TEXT, ALL_TEXT, RESET_TEXT) if pick["step"] == "city" else (DONE_TEXT, WHOLE_TEXT, "取消")


def _advance(pick: dict, finished: str) -> None:
    if pick["cursor"] + 1 < len(pick["cities"]):
        pick["cursor"] += 1
    else:
        pick["step"] = finished


def apply(pick: dict, op: str, value: str, card_county: str = "", *, finished: str) -> tuple[str | None, bool, bool]:
    """點一下卡片（或打快速回覆文字）→ 回 (給使用者的短訊或 None, 是否改動, 是否換到下一張卡／下一題)。

    pick＝{step: city|area|<finished>, cities, areas, cursor}；選完時 step 變成 finished。
    「全區」是設定不是切換：點幾次都是全區，並直接換下一個縣市（負責人 2026-10-01 實測：切換式會被
    卡片鈕＋快速回覆鈕各點一次而互相抵銷）。card_county＝按鈕所屬卡片的縣市；舊卡片只改它自己的縣市。
    """
    step = pick["step"]
    if op == "reset" and step in ("city", "area"):
        pick.update(step="city", cities=[], areas={})
        pick.pop("cursor", None)
        return None, True, True
    if pick.get("single"):                        # 單選：點縣市→行政區卡；點行政區→選完
        if step == "city" and op == "c" and value in COUNTIES:
            pick.update(step="area", cities=[value], areas={value: []}, cursor=0)
            return None, True, True
        if step == "area" and op == "d" and value.isdigit():
            county = pick["cities"][0]
            if card_county and card_county != county or int(value) >= len(DISTRICTS[county]):
                return None, False, False
            pick["areas"] = {county: [DISTRICTS[county][int(value)]]}
            pick["step"] = finished
            return "✓ " + county + DISTRICTS[county][int(value)], True, True
        return None, False, False
    if step == "city":
        cities = pick.setdefault("cities", [])
        if op == "c" and value in COUNTIES:
            if value in cities:
                cities.remove(value)
            else:
                cities.append(value)
            cities.sort(key=COUNTIES.index)
            chosen = ("已選：" + "、".join(cities)) if cities else "還沒選縣市"
            return chosen + "\n選好按「選好了，下一步」。", True, False
        if op == "all":
            pick["cities"] = list(COUNTIES)
            pick["areas"] = {c: [WHOLE] for c in COUNTIES}
            pick["step"] = finished
            return "✓ 已選：全台所有縣市（全區）", True, True
        if op == "next":
            if not cities:
                return "請至少選一個縣市，或按「全台」。", False, False
            pick["areas"] = {c: pick.get("areas", {}).get(c, []) for c in cities}
            pick.update(step="area", cursor=0)
            return None, True, True
        return None, False, False
    if step == "area":
        current = pick["cities"][pick["cursor"]]
        if card_county and card_county not in pick["cities"]:
            return None, False, False
        county = card_county or current
        picked = pick["areas"].setdefault(county, [])
        if op == "d":
            if value == WHOLE:
                pick["areas"][county] = [WHOLE]
                if county != current:
                    return f"✓ 已改成 {county}：全區\n現在在選 {current}。", True, False
                _advance(pick, finished)
                return f"✓ {county}：全區", True, True
            if not (value.isdigit() and int(value) < len(DISTRICTS[county])):
                return None, False, False
            name = DISTRICTS[county][int(value)]
            if WHOLE in picked:
                picked.remove(WHOLE)
            if name in picked:
                picked.remove(name)
            else:
                picked.append(name)
            note = summary({county: pick["areas"][county]})
            if county != current:
                return f"✓ 已改：{note}\n現在在選 {current}。", True, False
            return f"{note}\n還要選就繼續點；選好按「這個縣市選好了」。", True, False
        if op == "done":
            if county != current:
                return f"{county} 已經選好了，現在在選 {current}。", False, False
            if not picked:
                return f"{county} 還沒選區域：請點選行政區，或按「全區」。", False, False
            _advance(pick, finished)
            return "✓ " + summary({county: picked}), True, True
    return None, False, False


def summary(selection: dict[str, list[str]]) -> str:
    """{縣市: [區…] 或 ["*"]} → 「台中市：西屯區、南屯區；彰化縣：全區」。"""
    parts = []
    for county in COUNTIES:
        picked = selection.get(county)
        if picked is None:
            continue
        if WHOLE in picked:
            parts.append(f"{county}：全區")
        elif picked:
            parts.append(f"{county}：" + "、".join(d for d in DISTRICTS[county] if d in picked))
        else:
            parts.append(f"{county}：（尚未選區）")
    return "；".join(parts) if parts else "（尚未選擇）"
