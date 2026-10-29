"""組裝 LINE 官方帳號服務：各功能的處理順序、今日報告、管理員面板登入連結。"""
import datetime
import os

from adapters import team
from adapters.team import TeamCustomerSource, TeamListingSource
from adapters.line.agent_profile import make_profile_handler
from adapters.line.customer_intake import make_intake_handler
from adapters.line.listing_intake import make_listing_handler
from adapters.line.market_lookup import make_market_handler
from adapters.line.my_data import make_my_data_handler
from adapters.line.prospect_setup import make_prospect_handler
from adapters.line.onboarding import make_onboarding_handler
from adapters.line.shared import SharedChannelConfig, SharedLineApp
from adapters.storage import TenantStore
from adapters.toestate import ToEstateClient
from adapters.web_search import BudgetedSearch, provider_from_env
from web.jobs import daily_brief
from web.admin_app import AdminAuth, admins
from web.setup_claim import make_admin_claim_handler


BRIEF_WORDS = {"今日報告", "立即海巡", "自動海巡"}


def message_text(text: str) -> dict:
    return {"type": "text", "text": text[:4800]}


def create_app(*, store=None, channel=None, client_factory=None, search_provider=None,
               now=datetime.datetime.now, public_base=None):
    store = store or TenantStore()
    holder = {}

    def is_admin(tenant_id: str) -> bool:
        """店長／管理員可以改全店的客戶與物件；一般同仁只能改自己建的。"""
        app_ = holder.get("app")
        user = app_.registry.user_of(tenant_id) if app_ else None
        return bool(user) and user in admins()

    # 全店共用（REA_TEAM_MODE=shared，預設）：LINE 上看得到、配得到全店；排程與隱私功能只動自己的（own_view）
    customers = TeamCustomerSource(store, now=now, is_admin=is_admin)
    listings = TeamListingSource(store, now=now, is_admin=is_admin)
    own_customers, own_listings = customers.own_view(), listings.own_view()
    # 順序有意義：有草稿的流程會吃掉所有文字，先到先處理；新案／配對由新增物件那支回（有沒有物件都講實話）
    # 自動海巡＋各平台新物件：環境變數有搜尋服務金鑰才會動（沒有就只回「還沒接上」）
    # 同樣的查詢 2 小時內共用（同區仲介查的字一樣）；加報間隔也是 2 小時，不會把舊結果當新的
    search = BudgetedSearch(search_provider or provider_from_env(), store, now=now,
                            cache_ttl=datetime.timedelta(hours=2))

    def brief(ctx):
        if not ctx.is_owner_dm or ctx.text not in BRIEF_WORDS:
            return
        if not search.available:
            ctx.reply_text("自動搜尋還沒接上搜尋服務（管理員設定金鑰後就會每天自動跑）。\n現在可以先用「今日海巡」的搜尋連結。")
            return
        # 指令是同步跑（回覆權杖有時效），所以客人數上限比排程小，避免卡住其他訊息太久
        msgs, result, commit = daily_brief.run_for_tenant(tenant_id=ctx.tenant_id, store=store, search=search,
                                                          customers_src=own_customers, now=now,
                                                          max_customers=daily_brief.MAX_CUSTOMERS_COMMAND,
                                                          scan_market=False)   # 全市場盤點由排程跑，指令只用已盤點的
        if msgs:
            if ctx.reply(msgs):
                commit()
        else:
            why = "；".join(result["errors"]) or "最近一天沒有新的貼文或物件（看過的不重複給）"
            ctx.reply_text(f"今日報告：沒有新的結果。{why}。")

    def patrol_now(tenant_id):
        """「今日海巡」：直接找出那幾篇貼文（不是給搜尋連結）。沒接搜尋服務回 None，改用搜尋連結備案。"""
        if not search.available:
            return None
        msgs, _, commit = daily_brief.run_for_tenant(tenant_id=tenant_id, store=store, search=search,
                                                     customers_src=own_customers, now=now, patrol_only=True)
        return msgs, commit

    def toestate_client():
        """ToEstate 預設開（官方說明免金鑰）；串接精靈把網址設成 off 就只用內政部資料。"""
        if (os.environ.get("REA_TOESTATE_URL") or "").strip().lower() == "off":
            return None
        return ToEstateClient(store, now=now)

    def notify_colleague(tenant_id: str, text: str):
        """全店共用：新物件配到同事的買方 → 推一則給那位同事（走推播閘，吃推播額度、每日上限）。"""
        return holder["app"].notify(tenant_id, [message_text(text)], kind="colleague_match", keep_payload=False)

    # 隱私說明／匯出／刪除排第一：有草稿的流程也擋不住（刪除要解除綁定，綁定表等 App 建好才拿得到）
    my_data = make_my_data_handler(store, own_customers, own_listings, registry=lambda: holder["app"].registry, now=now)
    app = SharedLineApp(store, channel or SharedChannelConfig.from_env(), client_factory=client_factory,
                        handlers=[my_data, make_admin_claim_handler(store), make_onboarding_handler(store, now=now, is_admin=lambda u: u in admins()),
                                  make_prospect_handler(store, now=now, patrol_now=patrol_now),
                                  make_profile_handler(store, now=now),
                                  make_listing_handler(listings, customers, now=now, auto_search=lambda: search.available,
                                                       notify_colleague=notify_colleague,
                                                       adder_name=lambda t: team.name_of(store, t)),
                                  brief,
                                  make_intake_handler(customers, listings=listings, now=now,
                                                      auto_search=lambda: search.available),
                                  # 行情查詢排最後：正在填草稿時打的字歸草稿（跟其他指令一樣）
                                  make_market_handler(client_factory=toestate_client)], now=now)
    holder["app"] = app
    app.search, app.customers, app.payments, app.plans = search, own_customers, None, None
    app.team_customers, app.team_listings = customers, listings
    base = (public_base if public_base is not None else os.environ.get("REA_PUBLIC_BASE", "")).rstrip("/")
    app.terms_url = f"{base}/terms" if str(base).startswith("https://") else None      # 歡迎卡「看服務條款」
    app.listings = own_listings
    app.admin_auth = AdminAuth(store, now=now)                                         # 管理員面板（/admin）
    app.admin_link = (lambda user: f"{base}/admin/login?t={app.admin_auth.issue_link(user)}") \
        if str(base).startswith("https://") else (lambda user: None)
    # Unlike the legacy tenant-owned bot, the shared product does NOT retain
    # raw events/messages. Only confirmed needs and expiring form state remain.
    app.inner.persist_messages = False
    return app


class LazyApplication:
    def __init__(self):
        self.app = None

    def __call__(self, environ, start_response):
        if environ.get("PATH_INFO") == "/healthz" and environ.get("REQUEST_METHOD") == "GET":
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
            return [b"ok"]
        if self.app is None:
            self.app = create_app()
        return self.app.wsgi(environ, start_response)


application = LazyApplication()
