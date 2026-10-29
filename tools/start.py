"""啟動器：在背景開服務（不跳黑色視窗），需要時順便打開串接精靈。

    python tools/start.py              背景啟動服務（已經在跑就什麼都不做）
    python tools/start.py --setup      背景啟動＋用瀏覽器打開一鍵串接精靈
    python tools/start.py --stop       請服務自己收工
    python tools/start.py --status     看服務有沒有在跑
    python tools/start.py --autostart on|off   開機自動啟動（放進「啟動」資料夾，不用系統管理員）

Windows 上建議直接雙擊根目錄的「開始設定.vbs」「啟動.vbs」「停止.vbs」。
服務輸出寫在 <資料根>/logs/server.log。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.settings_store import SettingsStore  # noqa: E402
from adapters.storage import TenantStore  # noqa: E402

PORT = int(os.environ.get("PORT", "8080"))
BASE = f"http://127.0.0.1:{PORT}"
STARTUP_NAME = "rea-kit-autostart.vbs"
# 啟動時印一行署名：本版改自開源 rea-kit（完整署名見 NOTICE）
CUSTOM_AD = "本工具改自開源專案 rea-kit（作者：林其衞／留白事務所，Apache-2.0 授權），署名詳見 NOTICE。"


def running(timeout=1.5) -> bool:
    try:
        with urllib.request.urlopen(BASE + "/healthz", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def pythonw() -> str:
    exe = pathlib.Path(sys.executable)
    if sys.platform == "win32":
        w = exe.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(exe)


def start_server() -> bool:
    if running():
        return True
    logs = TenantStore().root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / "server.log", "ab")
    flags = 0
    if sys.platform == "win32":
        flags = 0x08000000 | 0x00000200          # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP：不跳視窗
    subprocess.Popen([pythonw(), str(ROOT / "tools" / "serve_line.py")], cwd=str(ROOT), stdout=out, stderr=out,
                     stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True,
                     start_new_session=(sys.platform != "win32"))
    for _ in range(40):
        time.sleep(0.5)
        if running():
            return True
    return False


def stop_server() -> None:
    from tools.serve_line import stop_file
    p = stop_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("stop", encoding="utf-8")
    for _ in range(20):
        time.sleep(0.5)
        if not running():
            return


def open_setup() -> None:
    from web.setup_wizard import ensure_token
    webbrowser.open(f"{BASE}/setup?k={ensure_token(SettingsStore())}")


def autostart(on: bool) -> pathlib.Path:
    folder = pathlib.Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    target = folder / STARTUP_NAME
    if on:
        folder.mkdir(parents=True, exist_ok=True)
        cmd = f'"{pythonw()}" "{ROOT / "tools" / "start.py"}"'.replace('"', '""')
        target.write_text(f'CreateObject("WScript.Shell").Run "{cmd}", 0, False\r\n', encoding="utf-8")
    elif target.exists():
        target.unlink()
    return target


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="信義房仲小幫手 啟動器")
    ap.add_argument("--setup", action="store_true", help="啟動後打開串接精靈")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--autostart", choices=("on", "off"))
    a = ap.parse_args(argv)
    if a.autostart:
        if sys.platform != "win32":
            print("開機自動啟動只支援 Windows；Linux／Mac 請用 systemd 或 launchd。")
            return 1
        print("已設定" if a.autostart == "on" else "已取消", "開機自動啟動：", autostart(a.autostart == "on"))
        return 0
    if a.status:
        print("服務在跑" if running() else "服務沒在跑")
        return 0
    if a.stop:
        stop_server()
        print("服務已停止" if not running() else "服務還沒停，稍等再看一次")
        return 0
    if not start_server():
        print(f"服務沒有啟動成功，請看 {TenantStore().root / 'logs' / 'server.log'}")
        return 1
    if a.setup:
        open_setup()
    print(f"服務在跑：{BASE}")
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"   # 繁中 Windows cp950 沒有「衞」，印不出的字換成 ?
    print(CUSTOM_AD.encode(enc, "replace").decode(enc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
