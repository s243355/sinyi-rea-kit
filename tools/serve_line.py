"""Single-instance closed-beta WSGI server. HTTPS terminates at reviewed hosting."""
import os
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

STOP_FILE = "stop.request"        # 執行資料夾裡出現這個檔 → 服務自己收工（換版用；開機排程啟動的服務一般權限砍不掉）


def stop_file() -> pathlib.Path:
    from adapters.storage import TenantStore
    return TenantStore().root.parent / STOP_FILE


def watch_for_stop(server, path: pathlib.Path, interval: float = 2.0) -> threading.Thread:
    """看到停止紙條就刪掉它、關掉伺服器（主程式正常結束，啟動器看到 0 就不重試）。"""
    def loop():
        stop = threading.Event()
        while not stop.wait(interval):
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
                server.close()
                return
    t = threading.Thread(target=loop, name="stop-watch", daemon=True)
    t.start()
    return t


def build_wsgi(holder=None, settings=None, *, setup_token=None, opener=None):
    """組出整個服務：LINE webhook、串接精靈（/setup）、服務條款、管理員面板。"""
    from adapters.settings_store import SettingsStore
    from web.app_holder import AppHolder
    from web.beta_ingress import BetaIngress
    from web.rate_limit import RateLimited
    from web import terms_page
    from web.admin_app import make_admin_app
    from web.setup_wizard import make_setup_app

    settings = settings or SettingsStore()
    settings.apply_to_environ()                                  # 精靈存過的設定帶進來（環境變數優先）
    holder = holder or AppHolder()
    holder.reload()                                              # 沒串 LINE 也能開，只是 webhook 回 503
    ingress = BetaIngress(holder.wsgi, burst=100, per_second=10)
    terms = RateLimited(terms_page.app)                          # 服務條款頁（歡迎卡「看服務條款」）
    setup = make_setup_app(holder, settings, token=setup_token, opener=opener)  # 一鍵串接精靈：只收本機＋一次性網址

    def admin(environ, start_response):                          # 管理員面板（LINE 拿登入連結，不用密碼）
        if holder.app is None:
            return holder.wsgi(environ, start_response)
        return RateLimited(make_admin_app(holder.app, holder.app.admin_auth))(environ, start_response)

    def wsgi(environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == "/setup" or path.startswith("/setup/"):
            return setup(environ, start_response)
        if path in ("/terms", "/terms/"):
            return terms(environ, start_response)
        if path == "/admin" or path.startswith("/admin/"):
            return admin(environ, start_response)
        if path == "/healthz":
            body = b"ok" if holder.app else b"setup-needed"
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))])
            return [body]
        return ingress(environ, start_response)

    wsgi.holder, wsgi.settings = holder, settings
    return wsgi


def main():
    from waitress import create_server
    wsgi = build_wsgi()

    server = create_server(wsgi, host=os.environ.get("REA_BIND_HOST", "127.0.0.1"),
                           port=int(os.environ.get("PORT", "8080")),
                           threads=4, max_request_body_size=1_000_000, channel_timeout=30)
    path = stop_file()
    if path:
        if path.exists():                                         # 上次留下的紙條不算
            path.unlink()
        watch_for_stop(server, path)
    try:   # 服務輸出寫在 server.log；背景執行沒有主控台時 print 不會出錯
        print("本工具改自開源專案 rea-kit（作者：林其衞／留白事務所，Apache-2.0 授權），署名詳見 NOTICE。", flush=True)
    except Exception:
        pass
    try:
        server.run()
    except (OSError, ValueError):                                 # close() 之後主迴圈收尾
        pass


if __name__ == "__main__":
    main()
