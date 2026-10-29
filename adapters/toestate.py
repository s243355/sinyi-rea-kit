# -*- coding: utf-8 -*-
"""ToEstate（toestate.tw）實價登錄資料：社區行情、行政區統計。

接法：ToEstate 公開的 MCP 端點（Streamable HTTP，JSON-RPC）。官方說明是免金鑰；有商用授權金鑰的，
在串接精靈填上，會用 Authorization: Bearer 帶過去（原則上以 ToEstate 提供的接法為準）。

只用三個工具：search（找社區／行政區）、get_community（社區行情摘要）、get_area_statistics（行政區統計）。
- 結果快取 6 小時（同一個社區一天被同仁問很多次也只查一次）。
- 每天呼叫上限（預設 300 次）；遇到 429 當天就停，不重試硬打。
- 價格欄位的口徑照 ToEstate：*_ping 是「元/坪、已扣車位」，回給仲介時換成「萬/坪」。

連不上或額度用完時，呼叫端改用內政部開放資料（adapters/lvr_opendata.py）。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from typing import Any, Callable

from adapters.integration_checks import NetError, http
from adapters.storage import TenantStore

DEFAULT_URL = "https://toestate.tw/mcp"
URL_ENV, KEY_ENV, CAP_ENV = "REA_TOESTATE_URL", "REA_TOESTATE_KEY", "REA_TOESTATE_DAILY_CAP"
SPACE = "sys-market"                    # 快取與用量放這裡（保留前綴，不會被當成一位仲介）
CACHE_TTL = datetime.timedelta(hours=6)
PROTOCOL = "2025-06-18"
ATTRIBUTION = "資料：ToEstate（內政部實價登錄整理）"


class ToEstateError(RuntimeError):
    """連不上、被限流、回應看不懂。訊息是白話，可以直接給仲介看。"""


def _wan(v) -> float | None:
    try:
        return round(float(v) / 10000, 1) if v else None
    except (TypeError, ValueError):
        return None


class ToEstateClient:
    def __init__(self, store: TenantStore | None = None, *, url: str | None = None, key: str | None = None,
                 daily_cap: int | None = None, opener=None, now: Callable[[], datetime.datetime] = datetime.datetime.now,
                 env=os.environ):
        self.store = store or TenantStore()
        self.url = (url or env.get(URL_ENV) or DEFAULT_URL).strip()
        self.key = (key if key is not None else env.get(KEY_ENV, "")).strip()
        try:
            self.daily_cap = int(daily_cap if daily_cap is not None else env.get(CAP_ENV) or 300)
        except ValueError:
            self.daily_cap = 300
        self.opener, self.now = opener, now
        self._session: str | None = None
        self._id = 0

    # ── 對外 ──
    @property
    def available(self) -> bool:
        return self.url.startswith("https://") and not self._stopped_today()

    def search(self, query: str, limit: int = 5) -> list[dict]:
        return (self.call("search", {"query": query, "limit": limit}) or {}).get("items") or []

    def community(self, community_id: int) -> dict:
        return self.call("get_community", {"community_id": int(community_id)}) or {}

    def area_stats(self, county: str, town: str | None = None) -> dict:
        args = {"county": county} if not town else {"county": county, "town": town}
        items = (self.call("get_area_statistics", args) or {}).get("items") or []
        return items[0] if town and items else ({"items": items} if not town else {})

    def call(self, tool: str, args: dict) -> Any:
        """呼叫一個工具，回解開後的 JSON。有快取先用快取。"""
        ck = hashlib.sha256(json.dumps([tool, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
        cached = self.store.read_json(SPACE, "toestate", "cache", ck + ".json", default=None)
        if cached and self._fresh(cached.get("at")):
            return cached.get("data")
        if not self.url.startswith("https://"):
            raise ToEstateError("ToEstate 網址要用 https:// 開頭，請到串接精靈重填。")
        if self._stopped_today():
            raise ToEstateError("ToEstate 今天的查詢次數用完了（或被限流），明天會自動恢復。")
        self._count()
        if self._session is None:
            self._handshake()
        res = self._rpc("tools/call", {"name": tool, "arguments": args})
        data = self._unwrap(res)
        self.store.write_json(SPACE, "toestate", "cache", ck + ".json",
                              data={"at": self.now().isoformat(timespec="seconds"), "data": data})
        return data

    # ── MCP（Streamable HTTP）──
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL}
        if self.key:
            h["Authorization"] = "Bearer " + self.key
        if self._session:
            h["Mcp-Session-Id"] = self._session
        return h

    def _handshake(self) -> None:
        self._session = ""                       # 有些伺服器不給 session id：空字串代表「握過手了」
        self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                 "clientInfo": {"name": "rea-kit-sinyi", "version": "1.0"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_body=False)

    def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if "error" in msg:
            err = msg["error"] or {}
            raise ToEstateError(f"ToEstate 回了錯誤：{str(err.get('message') or '不明原因')[:80]}")
        return msg.get("result") or {}

    def _post(self, payload: dict, *, expect_body: bool = True) -> dict:
        try:
            r = http("POST", self.url, headers=self._headers(), body=json.dumps(payload).encode(),
                     opener=self.opener, timeout=20)
        except NetError as e:
            raise ToEstateError("連不上 ToEstate（網路或對方網站暫時有問題）。") from e
        if r.status == 429:
            self._stop_today()
            raise ToEstateError("ToEstate 說查太快了（限流），今天先停，明天自動恢復。")
        if r.status in (401, 403):
            raise ToEstateError("ToEstate 不讓我們查（金鑰錯誤或沒有權限），請到串接精靈檢查金鑰。")
        if r.status == 404 and self._session:
            self._session = None                  # session 過期：下次重新握手
            raise ToEstateError("ToEstate 連線過期了，請再問一次。")
        if r.status >= 400:
            raise ToEstateError(f"ToEstate 暫時有問題（HTTP {r.status}），請稍後再試。")
        if getattr(r, "session_id", ""):
            self._session = r.session_id
        if not expect_body or not r.body.strip():
            return {}
        return self._parse(r.body, r.content_type)

    @staticmethod
    def _parse(body: bytes, ctype: str) -> dict:
        text = body.decode("utf-8", "replace")
        if "text/event-stream" in (ctype or "").lower() or text.lstrip().startswith(("event:", "data:")):
            last = None
            for line in text.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk:
                        try:
                            obj = json.loads(chunk)
                        except ValueError:
                            continue
                        if isinstance(obj, dict) and ("result" in obj or "error" in obj):
                            last = obj
            if last is None:
                raise ToEstateError("ToEstate 的回應看不懂（串流裡沒有結果）。")
            return last
        try:
            obj = json.loads(text)
        except ValueError as e:
            raise ToEstateError("ToEstate 的回應看不懂。") from e
        return obj if isinstance(obj, dict) else {}

    @staticmethod
    def _unwrap(result: dict) -> Any:
        """工具結果：優先 structuredContent，其次第一段文字內容（JSON 字串）。外面包一層 {"result": "<json>"} 也解開。"""
        if result.get("isError"):
            txt = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
            raise ToEstateError("ToEstate 查不到：" + (txt[:80] or "不明原因"))
        data = result.get("structuredContent")
        if data is None:
            for c in result.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    try:
                        data = json.loads(c.get("text") or "")
                    except ValueError:
                        data = {"text": c.get("text")}
                    break
        for _ in range(2):
            if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], str):
                try:
                    data = json.loads(data["result"])
                except ValueError:
                    break
        return data

    # ── 用量與快取 ──
    def _fresh(self, at: str | None) -> bool:
        try:
            return self.now() - datetime.datetime.fromisoformat(at) < CACHE_TTL
        except (TypeError, ValueError):
            return False

    def _usage(self) -> dict:
        u = self.store.read_json(SPACE, "toestate", "usage.json", default={}) or {}
        return u if u.get("day") == self.now().date().isoformat() else {"day": self.now().date().isoformat(), "n": 0}

    def _count(self) -> None:
        with self.store.lock(SPACE, "toestate-usage"):
            u = self._usage()
            if u["n"] >= self.daily_cap:
                raise ToEstateError(f"ToEstate 今天已經查了 {u['n']} 次（上限），明天會自動恢復。")
            u["n"] += 1
            self.store.write_json(SPACE, "toestate", "usage.json", data=u)

    def _stop_today(self) -> None:
        with self.store.lock(SPACE, "toestate-usage"):
            u = self._usage()
            u["stopped"] = True
            self.store.write_json(SPACE, "toestate", "usage.json", data=u)

    def _stopped_today(self) -> bool:
        u = self._usage()
        return bool(u.get("stopped")) or u.get("n", 0) >= self.daily_cap


# ── 給 LINE 回覆用的摘要（純函式，方便測試）──

def area_text(stats: dict, *, county: str, town: str) -> str:
    if not stats or not stats.get("n_1y"):
        return f"{county}{town}：ToEstate 近一年沒有足夠的成交資料。"
    med, prev = _wan(stats.get("median_ping_1y_c") or stats.get("median_ping_1y")), _wan(stats.get("median_ping_prev_1y"))
    yoy = f"（前一年 {prev} 萬，{'+' if med >= prev else ''}{round((med / prev - 1) * 100, 1)}%）" if med and prev else ""
    lines = [f"📊 {county}{town} 行政區行情",
             f"近一年 {stats.get('n_1y')} 筆，單價中位 {med} 萬/坪{yoy}",
             f"一半落在 {_wan(stats.get('p25_ping_1y'))}～{_wan(stats.get('p75_ping_1y'))} 萬/坪"]
    if stats.get("median_total_1y"):
        lines.append(f"總價中位 {round(stats['median_total_1y'] / 10000):,} 萬")
    if stats.get("rent_median_ping_1y"):
        lines.append(f"租金中位 {round(stats['rent_median_ping_1y']):,} 元/坪/月（{stats.get('rent_n_1y')} 筆）")
    lines.append("單價已扣車位；排除親友等特殊交易。實價登錄約晚 1～3 個月申報。")
    lines.append(ATTRIBUTION)
    return "\n".join(lines)


def community_text(c: dict, *, recent: int = 5) -> str:
    name = c.get("name") or c.get("address_norm") or "這個社區"
    head = f"🏢 {name}（{c.get('town') or ''}・{c.get('building_type') or ''}"
    if c.get("completion_date"):
        head += f"・{c['completion_date'][:4]} 年完工"
    head += "）"
    lines = [head]
    if c.get("n_1y"):
        lines.append(f"近一年 {c['n_1y']} 筆，單價中位 {_wan(c.get('median_ping_1y'))} 萬/坪")
    if c.get("n_3y"):
        lines.append(f"近三年 {c['n_3y']} 筆，單價中位 {_wan(c.get('median_ping_3y'))} 萬/坪")
    sales = ((c.get("summary") or {}).get("recent_sales") or c.get("recent_sales") or [])[:recent]
    if sales:
        lines.append("最近成交：")
        for s in sales:
            floor = f"{s.get('floor')}F/{s.get('floor_total')}F" if s.get("floor") else ""
            rooms = f"{s.get('rooms')}房" if s.get("rooms") else ""
            total = f"{round(s['total_price'] / 10000):,}萬" if s.get("total_price") else ""
            park = "含車位" if s.get("has_parking") else ""
            lines.append("・" + " ".join(x for x in (s.get("date", "")[:7], floor, rooms, f"{s.get('area_ping')}坪"
                                                      if s.get("area_ping") else "", total,
                                                      f"{_wan(s.get('unit_price_ping'))}萬/坪", park) if x))
    rs = c.get("remark_summary") or {}
    if rs.get("n_flags"):
        lines.append(f"⚠ 成交備註有 {rs['n_flags']} 筆提到瑕疵（凶宅、漏水等），帶看前請查清楚。")
    if not c.get("n_3y"):
        lines.append("近三年沒有成交紀錄。")
    lines.append("單價已扣車位；最近成交不含預售。")
    lines.append(ATTRIBUTION)
    return "\n".join(lines)
