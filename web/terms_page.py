# -*- coding: utf-8 -*-
"""對外的服務條款頁 /terms（加好友按「同意並開始」＝同意這一頁）。

內容來自 docs/法遵/使用條款.md＋委託處理條款.md（開頭 `>` 的內部說明不顯示）；{提供者姓名} 用執行資料夾設定
（REA_PROVIDER_NAME）帶入，沒設定就寫「本服務提供者」。只收 GET、不放外部資源、不讓搜尋引擎收錄。
"""
from __future__ import annotations

import html
import os
import pathlib
import re
from core import brand

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = (ROOT / "docs" / "法遵" / "使用條款.md", ROOT / "docs" / "法遵" / "委託處理條款.md")


def _inline(text: str) -> str:
    out = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)


def markdown(md: str) -> str:
    """只認條款會用到的寫法：標題、清單、表格、粗體、段落。"""
    out, in_list, table = [], None, []

    def close():
        nonlocal in_list, table
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|[\s\-|:]+\|", r)]
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            out.append("<table>" + "".join(
                "<tr>" + "".join(f"<{'th' if i == 0 else 'td'}>{_inline(c)}</{'th' if i == 0 else 'td'}>" for c in row)
                + "</tr>" for i, row in enumerate(cells)) + "</table>")
            table = []

    for line in md.splitlines():
        s = line.strip()
        if s.startswith(">"):
            continue                                       # 內部說明不對外
        if s.startswith("|"):
            if in_list:
                close()
            table.append(s)
            continue
        if table:
            close()
        m = re.match(r"^(#{1,3})\s+(.*)$", s)
        if m:
            close()
            n = len(m.group(1)) + 1
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
        elif re.match(r"^[-*]\s+", s) or re.match(r"^\d+\.\s+", s):
            kind = "ol" if s[0].isdigit() else "ul"
            if in_list != kind:
                close()
                out.append(f"<{kind}>")
                in_list = kind
            out.append("<li>" + _inline(re.sub(r"^([-*]|\d+\.)\s+", "", s)) + "</li>")
        elif s:
            close()
            out.append("<p>" + _inline(s) + "</p>")
        else:
            close()
    close()
    return "\n".join(out)


def render(provider: str | None = None) -> bytes:
    name = (provider or os.environ.get("REA_PROVIDER_NAME", "")).strip() or "本服務提供者"
    parts = []
    for path in DOCS:
        md = path.read_text(encoding="utf-8")
        md = md.replace("（簽署時填寫；改由公司提供時填 {公司名稱}）", "").replace("{提供者姓名}", name)
        parts.append(markdown(md))
    style = ("body{font-family:system-ui,'Microsoft JhengHei',sans-serif;margin:0 auto;max-width:760px;padding:24px 18px;"
             "background:#fff;color:#1d2420;line-height:1.7}h1{font-size:24px;color:" + brand.GREEN + "}h2{font-size:20px;"
             "color:" + brand.GREEN + ";border-top:1px solid #dfe7e3;padding-top:18px}h3{font-size:17px;margin-top:22px}"
             "li,p,td,th{font-size:16px}table{border-collapse:collapse;width:100%;margin:8px 0}"
             "td,th{border:1px solid #dfe7e3;padding:6px 8px;text-align:left;vertical-align:top}th{background:" + brand.TINT + "}")
    doc = ("<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'>"
           "<title>" + brand.NAME + " 服務條款</title><style>" + style + "</style></head><body>"
           + "\n<hr>\n".join(parts) + "</body></html>")
    return doc.encode("utf-8")


def app(environ, start_response):
    if environ.get("REQUEST_METHOD") not in ("GET", "HEAD"):
        start_response("405 Method Not Allowed", [("Allow", "GET"), ("Content-Length", "0")])
        return [b""]
    body = render()
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-cache"),
                              ("Content-Length", str(len(body)))])
    return [body] if environ.get("REQUEST_METHOD") == "GET" else [b""]
