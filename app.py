import hashlib
import hmac
import math
import os
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone, timedelta

from flask import (
    Flask, abort, flash, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from storage import StorageConflict, StorageUnavailable, make_store


SUBJECTS = {"math": "数学", "english": "英語"}
PAGE_SIZE = 20
USERNAME = re.compile(r"^[a-zA-Z0-9_]{3,32}$")
JST = timezone(timedelta(hours=9))


class InputError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


class AttemptLimiter:
    """Bounded per-process auth throttle; post throttle is persisted in Blob."""

    def __init__(self):
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def allow(self, key, limit=10, window=600):
        now = time.monotonic()
        with self.lock:
            hits = [stamp for stamp in self.entries.pop(key, []) if stamp > now - window]
            allowed = len(hits) < limit
            if allowed:
                hits.append(now)
            self.entries[key] = hits
            while len(self.entries) > 2048:
                self.entries.popitem(last=False)
            return allowed


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        STORAGE_BACKEND=os.environ.get("STORAGE_BACKEND", "azure"),
        AZURE_STORAGE_CONNECTION_STRING=os.environ.get("AZURE_STORAGE_CONNECTION_STRING", ""),
        AZURE_STORAGE_ACCOUNT_URL=os.environ.get("AZURE_STORAGE_ACCOUNT_URL", ""),
        AZURE_STORAGE_CONTAINER=os.environ.get("AZURE_STORAGE_CONTAINER", "studyboard"),
        AZURE_STORAGE_BLOB=os.environ.get("AZURE_STORAGE_BLOB", "board.json"),
        LOCAL_STORAGE_PATH=os.path.join(app.instance_path, "board.json"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=128 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if os.environ.get("WEBSITE_SITE_NAME"):
        # Azure's front end appends the client address. Trust exactly one hop;
        # don't trust forwarded host/prefix or arbitrary headers on local runs.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    if not app.config["SECRET_KEY"] or len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("SECRET_KEYに32文字以上のランダムな値を設定してください。")
    store = make_store(app.config)
    app.extensions["board_store"] = store
    limiter = AttemptLimiter()

    def mac(value):
        return hmac.new(app.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()

    def client_address():
        address = request.remote_addr or "unknown"
        # App Service may append a source port; it must not bypass the throttle.
        if address.startswith("[") and "]" in address:
            return address[1:address.index("]")]
        if address.count(":") == 1 and address.rsplit(":", 1)[1].isdigit():
            return address.rsplit(":", 1)[0]
        return address

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    @app.context_processor
    def globals_for_templates():
        return {"subjects": SUBJECTS, "csrf_token": csrf_token, "current_user": session.get("user_id")}

    @app.template_filter("jst")
    def format_date(timestamp):
        return datetime.fromtimestamp(timestamp, JST).strftime("%Y/%m/%d %H:%M")

    @app.before_request
    def protect_forms():
        if request.method == "POST":
            expected = session.get("csrf", "")
            submitted = request.form.get("csrf_token", "")
            if not expected or not secrets.compare_digest(expected.encode(), submitted.encode()):
                raise InputError("ページの有効期限が切れました。再読み込みしてから送信してください。", 400)

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "font-src 'self' https://cdn.jsdelivr.net; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # HTML contains CSRF and account state; never cache it publicly.
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store"
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(InputError)
    def input_error(exc):
        return render_template("error.html", code=exc.status, message=exc.message), exc.status

    @app.errorhandler(StorageUnavailable)
    def storage_error(exc):
        # Never log connection strings or Azure exception bodies.
        app.logger.warning("Board storage unavailable (%s)", type(exc).__name__)
        message = "投稿が混み合っています。少し待ってからもう一度送信してください。" if isinstance(exc, StorageConflict) else "保存先に接続できません。しばらく待ってからお試しください。"
        return render_template("error.html", code=503, message=message), 503, {"Retry-After": "10"}

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", code=404, message="ページが見つかりませんでした。"), 404

    @app.errorhandler(413)
    def too_large(_):
        return render_template("error.html", code=413, message="入力が長すぎます。本文は1万文字以内にしてください。"), 413

    @app.errorhandler(500)
    def internal_error(_):
        return render_template("error.html", code=500, message="処理を完了できませんでした。しばらく待ってからお試しください。"), 500

    def require_user():
        if not session.get("user_id"):
            raise InputError("投稿するにはログインしてください。", 401)
        return session["user_id"]

    def valid_user(state, user_id):
        if not any(user["id"] == user_id for user in state["users"].values()):
            raise InputError("ログインし直してください。", 401)

    def validate_body():
        body = request.form.get("body", "").replace("\r\n", "\n").strip()
        if not body or len(body) > 10000:
            raise InputError("本文は1〜10,000文字で入力してください。")
        return body

    def post_limit(state, user_id, now):
        if now - state["last_posts"].get(user_id, 0) < 10:
            raise InputError("連続投稿は10秒あけてください。", 429)
        state["last_posts"][user_id] = now

    def list_threads(subject=None, query=""):
        state = store.read()
        rows = [thread for thread in state["threads"].values() if not subject or thread["subject"] == subject]
        if query:
            needle = query.casefold()
            rows = [thread for thread in rows if needle in thread["title"].casefold() or any(
                needle in post["body"].casefold() for post in thread["posts"]
            )]
        rows.sort(key=lambda thread: (thread["updated_at"], thread["id"]), reverse=True)
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
        page = min(page, pages)
        return {
            "threads": rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE],
            "total": len(rows), "page": page, "pages": pages,
        }

    @app.get("/")
    def index():
        return render_template("index.html", **list_threads())

    @app.get("/board/<subject>")
    def board(subject):
        if subject not in SUBJECTS:
            abort(404)
        return render_template("board.html", subject=subject, **list_threads(subject))

    @app.get("/search")
    def search():
        query = request.args.get("q", "").strip()
        if len(query) > 100:
            raise InputError("検索語は100文字以内にしてください。")
        subject = request.args.get("subject", "")
        if subject and subject not in SUBJECTS:
            raise InputError("教科を選び直してください。")
        return render_template("search.html", query=query, subject=subject, **list_threads(subject, query))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "GET":
            return render_template("auth.html", registering=True)
        # Limits apply before slow password hashing.
        if not limiter.allow("register:" + mac(client_address()), limit=5):
            raise InputError("登録が続いています。10分ほど待ってからお試しください。", 429)
        name = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if not USERNAME.fullmatch(name):
            raise InputError("ログインIDは半角英数字・_ の3〜32文字にしてください。")
        if not 10 <= len(password) <= 128:
            raise InputError("パスワードは10〜128文字にしてください。")
        if password != request.form.get("password_confirm", ""):
            raise InputError("確認用パスワードが一致しません。")
        hashed = generate_password_hash(password)
        user_id = uuid.uuid4().hex

        def operation(state):
            if name in state["users"]:
                raise InputError("このログインIDは使われています。別のIDを入力してください。", 409)
            state["users"][name] = {"id": user_id, "password_hash": hashed, "created_at": time.time()}

        store.mutate(operation)
        session.clear()
        session["user_id"] = user_id
        session.permanent = True
        flash("登録できました。さっそく学びを共有しましょう。", "success")
        return redirect(url_for("index"), code=303)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return render_template("auth.html", registering=False)
        name = request.form.get("username", "").strip().lower()
        address = mac(client_address())
        if not limiter.allow("login-ip:" + address, limit=20) or not limiter.allow("login-user:" + name):
            raise InputError("ログイン試行が続いています。10分ほど待ってからお試しください。", 429)
        password = request.form.get("password", "")
        if len(name) > 32 or not 10 <= len(password) <= 128:
            raise InputError("ログインIDまたはパスワードが違います。", 401)
        user = store.read()["users"].get(name)
        if not user or not check_password_hash(user["password_hash"], password):
            raise InputError("ログインIDまたはパスワードが違います。", 401)
        session.clear()
        session["user_id"] = user["id"]
        session.permanent = True
        flash("ログインしました。", "success")
        return redirect(url_for("index"), code=303)

    @app.post("/logout")
    def logout():
        session.clear()
        flash("ログアウトしました。", "success")
        return redirect(url_for("index"), code=303)

    @app.route("/new", methods=["GET", "POST"])
    def new_thread():
        if not session.get("user_id") and request.method == "GET":
            return redirect(url_for("login"))
        user_id = require_user()
        if request.method == "GET":
            subject = request.args.get("subject", "math")
            return render_template("new.html", subject=subject if subject in SUBJECTS else "math")
        subject = request.form.get("subject", "")
        title = request.form.get("title", "").strip()
        body = validate_body()
        if subject not in SUBJECTS:
            raise InputError("英語か数学を選択してください。")
        if not 1 <= len(title) <= 120:
            raise InputError("タイトルは1〜120文字にしてください。")
        thread_id = uuid.uuid4().hex
        now = time.time()

        def operation(state):
            valid_user(state, user_id)
            post_limit(state, user_id, now)
            state["threads"][thread_id] = {
                "id": thread_id, "subject": subject, "title": title,
                "created_at": now, "updated_at": now,
                "posts": [{"number": 1, "user_id": user_id, "body": body, "created_at": now}],
            }

        store.mutate(operation)
        return redirect(url_for("thread", thread_id=thread_id), code=303)

    @app.get("/thread/<thread_id>")
    def thread(thread_id):
        item = store.read()["threads"].get(thread_id)
        if item is None:
            abort(404)
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        pages = max(1, math.ceil(len(item["posts"]) / 50))
        page = min(page, pages)
        posts = []
        for post in item["posts"][(page - 1) * 50:page * 50]:
            public = {key: post[key] for key in ("number", "body", "created_at")}
            public["anonymous_id"] = mac(thread_id + ":" + post["user_id"])[:8]
            public["parts"] = []
            for part in re.split(r"(>>[0-9]{1,7})", post["body"]):
                if re.fullmatch(r">>[0-9]{1,7}", part) and 1 <= int(part[2:]) <= len(item["posts"]):
                    number = int(part[2:])
                    public["parts"].append({"text": part, "number": number, "page": (number - 1) // 50 + 1})
                else:
                    public["parts"].append({"text": part})
            posts.append(public)
        return render_template("thread.html", thread=item, posts=posts, page=page, pages=pages)

    @app.post("/thread/<thread_id>/reply")
    def reply(thread_id):
        user_id = require_user()
        body = validate_body()
        now = time.time()

        def operation(state):
            valid_user(state, user_id)
            item = state["threads"].get(thread_id)
            if item is None:
                abort(404)
            post_limit(state, user_id, now)
            number = len(item["posts"]) + 1
            item["posts"].append({"number": number, "user_id": user_id, "body": body, "created_at": now})
            item["updated_at"] = max(item["updated_at"], now)
            return number

        number = store.mutate(operation)
        return redirect(url_for("thread", thread_id=thread_id, page=(number - 1) // 50 + 1, _anchor=f"post-{number}"), code=303)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    return app


if __name__ == "__main__":
    from waitress import serve

    serve(create_app(), host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), threads=4)

