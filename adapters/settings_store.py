# -*- coding: utf-8 -*-
"""串接設定（一鍵串接精靈寫的那一份）：只存在這台電腦的資料夾，不進 git、不回顯在畫面上。

檔案：<資料根>/settings/integrations.json（資料根＝REA_DATA_DIR，沒設就是 repo 下的 data/，已在 .gitignore）
  - 金鑰類（is_secret）在 Windows 用 DPAPI 加密（只有同一個 Windows 帳號解得開）；
    其他系統以明文存，但檔案權限設成只有自己能讀（600）。
  - 啟動服務時 apply_to_environ() 把設定帶進環境變數；**已經設好的環境變數優先**（雲端平台的私密變數蓋過檔案）。
  - 畫面只會看到「已設定／未設定」，看不到值，連最後幾碼也不給。
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import threading
from dataclasses import dataclass

from adapters.storage import DATA_ENV, REPO


@dataclass(frozen=True)
class Field:
    key: str            # 環境變數名
    label: str          # 給人看的名字
    secret: bool = True


# 每個串接用到的設定欄位。精靈、狀態頁、文件都從這裡取，不各寫一份。
FIELDS: dict[str, Field] = {f.key: f for f in (
    Field("REA_SHARED_LINE_SECRET", "LINE Channel secret"),
    Field("REA_SHARED_LINE_TOKEN", "LINE Channel access token"),
    Field("REA_PUBLIC_BASE", "對外 HTTPS 網址", secret=False),
    Field("REA_ADMIN_USERS", "管理員 LINE 帳號", secret=False),
    Field("REA_SIGNUP_MODE", "誰可以自己加入（invite／open）", secret=False),
    Field("REA_TEAM_MODE", "全店共用買方與物件（shared／solo）", secret=False),
    Field("REA_GSHEET_CUSTOMERS_URL", "Google 試算表（客戶需求）網址"),
    Field("REA_GSHEET_LISTINGS_URL", "Google 試算表（物件）網址"),
    Field("REA_LVR_COUNTIES", "實價登錄縣市", secret=False),
    Field("REA_DEFAULT_CITY", "預設縣市（行情查詢只打行政區時用）", secret=False),
    Field("REA_TOESTATE_URL", "ToEstate 網址（off＝不用）", secret=False),
    Field("REA_TOESTATE_KEY", "ToEstate 金鑰（選填）"),
    Field("REA_TOESTATE_DAILY_CAP", "ToEstate 每日查詢上限", secret=False),
    Field("REA_MOI_CLIENT_ID", "地政平台 Client ID"),
    Field("REA_MOI_SECRET", "地政平台安全碼"),
    Field("REA_MOI_DAILY_LIMIT", "地政平台每日上限", secret=False),
    Field("REA_SEARCH_PROVIDER", "搜尋服務", secret=False),
    Field("REA_SEARCH_KEY", "搜尋服務金鑰"),
)}

_LOCK = threading.Lock()


def settings_path(root: str | os.PathLike | None = None) -> pathlib.Path:
    base = pathlib.Path(root or os.environ.get(DATA_ENV) or (REPO / "data")).resolve()
    return base / "settings" / "integrations.json"


# ── Windows DPAPI（只用標準庫 ctypes，不多裝套件）──
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):   # 0x1＝不跳任何視窗
        raise OSError("DPAPI 失敗（可能換了 Windows 帳號或電腦，請重新跑一次串接精靈）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _seal(value: str) -> str:
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi(value.encode("utf-8"), True)).decode("ascii")
    return "plain:" + value


def _open(raw: str) -> str:
    if raw.startswith("dpapi:"):
        return _dpapi(base64.b64decode(raw[6:]), False).decode("utf-8")
    if raw.startswith("plain:"):
        return raw[6:]
    return raw


class SettingsStore:
    def __init__(self, root: str | os.PathLike | None = None):
        self.path = settings_path(root)

    def _read_raw(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            raise ValueError(f"{self.path} 壞掉了：請刪掉它再重跑串接精靈") from None
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> str:
        raw = self._read_raw().get(key)
        if not raw:
            return ""
        try:
            return _open(str(raw))
        except OSError:
            return ""

    def values(self) -> dict[str, str]:
        out = {}
        for k in self._read_raw():
            v = self.get(k)
            if v:
                out[k] = v
        return out

    def save(self, updates: dict[str, str], *, apply: bool = True) -> None:
        """寫入（空字串＝刪除該欄）。apply＝同時更新這個行程的環境變數，不用重開就生效。"""
        unknown = [k for k in updates if k not in FIELDS]
        if unknown:
            raise ValueError("不認得的設定：" + "、".join(unknown))
        with _LOCK:
            data = self._read_raw()
            for k, v in updates.items():
                v = str(v or "").strip()
                if v:
                    data[k] = _seal(v) if FIELDS[k].secret else v
                else:
                    data.pop(k, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)
        if apply:
            for k, v in updates.items():
                v = str(v or "").strip()
                if v:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def apply_to_environ(self, environ=os.environ) -> list[str]:
        """啟動時呼叫：檔案裡有、環境變數還沒設的才帶進去。回帶進去的欄位名（不含值）。"""
        applied = []
        for k, v in self.values().items():
            if k in FIELDS and not environ.get(k):
                environ[k] = v
                applied.append(k)
        return applied

    def status(self, environ=os.environ) -> dict[str, bool]:
        """每個欄位有沒有設（檔案或環境變數任一）。只回 True／False，不回值。"""
        saved = self._read_raw()
        return {k: bool(environ.get(k) or saved.get(k)) for k in FIELDS}
