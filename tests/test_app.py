import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app import create_app
from storage import AzureBlobStore, StorageUnavailable, empty_state


@pytest.fixture
def app():
    return create_app({
        "TESTING": True, "SECRET_KEY": "testing-secret-key-with-at-least-32-characters",
        "STORAGE_BACKEND": "memory", "SESSION_COOKIE_SECURE": False,
    })


def token(client, path="/"):
    client.get(path)
    with client.session_transaction() as session:
        # Anonymous home doesn't have a form; auth pages always initialize CSRF.
        if "csrf" not in session:
            session["csrf"] = "test-csrf-token"
        return session["csrf"]


def register(client, name="student_one"):
    return client.post("/register", data={
        "csrf_token": token(client, "/register"), "username": name,
        "password": "correct-password-123", "password_confirm": "correct-password-123",
    })


def create_thread(client, title="数学I p.42 問3", body="平方完成を教えてください。$x^2$", subject="math"):
    return client.post("/new", data={
        "csrf_token": token(client, "/new"), "subject": subject, "title": title, "body": body,
    })


def clear_post_limit(app):
    app.extensions["board_store"].mutate(lambda state: state["last_posts"].clear())


def test_end_to_end_register_post_search_reply_logout_and_login(app):
    client = app.test_client()
    assert register(client).status_code == 303
    created = create_thread(client)
    assert created.status_code == 303
    path = created.headers["Location"]
    page = client.get(path).get_data(as_text=True)
    assert "数学I p.42 問3" in page
    assert "名無しさん" in page and "student_one" not in page
    assert "$x^2$" in page and "katex@0.16.22" in page
    assert client.get("/search?q=平方完成").status_code == 200
    assert "数学I p.42 問3" in client.get("/search?q=平方完成").get_data(as_text=True)
    assert "数学I p.42 問3" not in client.get("/search?q=平方完成&subject=english").get_data(as_text=True)
    second = app.test_client()
    register(second, "student_two")
    response = second.post(path + "/reply", data={"csrf_token": token(second, path), "body": ">>1 両辺を整理します。"})
    assert response.status_code == 303
    assert "#post-2" in response.headers["Location"]
    result = client.get(path).get_data(as_text=True)
    assert 'id="post-2"' in result and "両辺を整理します。" in result
    assert "page=1#post-1" in result
    assert "数学I p.42 問3" in client.get("/search?q=両辺を整理").get_data(as_text=True)
    assert client.post("/logout", data={"csrf_token": token(client)}).status_code == 303
    assert client.post("/login", data={"csrf_token": token(client, "/login"), "username": "STUDENT_ONE", "password": "correct-password-123"}).status_code == 303


def test_csrf_auth_xss_and_no_edit_routes(app):
    client = app.test_client()
    assert client.post("/register", data={}).status_code == 400
    csrf = token(client, "/register")
    assert client.post("/register", data={"csrf_token": "日本語"}).status_code == 400
    assert client.post("/new", data={"csrf_token": csrf}).status_code == 401
    register(client)
    created = create_thread(client, title='<script>alert("title")</script>', body='<img src=x onerror=alert(1)>')
    page = client.get(created.headers["Location"])
    text = page.get_data(as_text=True)
    assert "<img src=x" not in text and "&lt;img" in text
    assert '<script>alert("title")</script>' not in text
    assert "object-src 'none'" in page.headers["Content-Security-Policy"]
    assert page.headers["Cache-Control"] == "no-store"
    assert client.post(created.headers["Location"] + "/edit", data={"csrf_token": token(client)}).status_code == 404
    assert client.delete(created.headers["Location"]).status_code == 405


def test_validation_duplicate_user_and_post_rate_limit(app):
    client = app.test_client()
    register(client)
    assert register(client).status_code == 409
    assert create_thread(client, body="x" * 10001).status_code == 400
    assert create_thread(client, subject="science").status_code == 400
    created = create_thread(client)
    assert created.status_code == 303
    assert create_thread(client).status_code == 429
    path = created.headers["Location"]
    assert client.post(path + "/reply", data={"csrf_token": token(client, path), "body": "連続投稿"}).status_code == 429
    assert len(app.extensions["board_store"].read()["threads"]) == 1
    assert client.get("/board/science").status_code == 404
    assert client.get("/search?subject=science").status_code == 400
    assert client.get("/search?q=" + "x" * 101).status_code == 400


def test_anonymous_id_stable_only_within_thread(app):
    client = app.test_client()
    register(client)
    first = create_thread(client)
    clear_post_limit(app)
    second = create_thread(client, title="別の質問")
    first_page = client.get(first.headers["Location"]).get_data(as_text=True)
    second_page = client.get(second.headers["Location"]).get_data(as_text=True)
    first_id = re.search(r"ID: ([0-9a-f]{8})", first_page).group(1)
    second_id = re.search(r"ID: ([0-9a-f]{8})", second_page).group(1)
    assert first_id != second_id
    clear_post_limit(app)
    client.post(first.headers["Location"] + "/reply", data={"csrf_token": token(client), "body": "補足です"})
    result = client.get(first.headers["Location"]).get_data(as_text=True)
    assert result.count("ID: " + first_id) == 2


def test_local_persistence_survives_app_restart(tmp_path):
    config = {"SECRET_KEY": "test-secret-for-persistence-over-32-chars", "STORAGE_BACKEND": "local", "LOCAL_STORAGE_PATH": str(tmp_path / "board.json"), "SESSION_COOKIE_SECURE": False, "TESTING": True}
    first = create_app(config)
    client = first.test_client()
    register(client)
    path = create_thread(client).headers["Location"]
    second = create_app(config)
    assert "数学I p.42 問3" in second.test_client().get(path).get_data(as_text=True)
    raw = json.loads((tmp_path / "board.json").read_text(encoding="utf-8"))
    assert raw["users"]["student_one"]["password_hash"].startswith("scrypt:")
    assert "correct-password-123" not in json.dumps(raw)


def test_pagination_and_cross_page_reply_links(app):
    store = app.extensions["board_store"]

    def seed(state):
        for i in range(25):
            state["threads"][str(i)] = {"id": str(i), "subject": "math", "title": f"問題{i}", "created_at": i, "updated_at": i, "posts": [{"number": 1, "body": "最初", "user_id": "u", "created_at": i}]}
        for number in range(2, 53):
            state["threads"]["24"]["posts"].append({"number": number, "body": ">>51 >>1", "user_id": "u", "created_at": number})

    store.mutate(seed)
    client = app.test_client()
    assert "2 / 2" in client.get("/?page=2").get_data(as_text=True)
    assert client.get("/?page=bad").status_code == 200
    first = client.get("/thread/24").get_data(as_text=True)
    assert "page=2#post-51" in first and 'id="post-51"' not in first
    assert 'id="post-51"' in client.get("/thread/24?page=2").get_data(as_text=True)


class FakeBlob:
    """ETag backend that deliberately races downloads and uploads."""

    def __init__(self):
        self.state = empty_state()
        self.version = 1
        self.lock = threading.Lock()
        self.conflicts = 0
        self.barrier = threading.Barrier(10)
        self.download_count = 0

    def download_blob(self):
        with self.lock:
            data = json.dumps(self.state).encode()
            etag = str(self.version)
            self.download_count += 1
            count = self.download_count
        if count <= 10:
            self.barrier.wait(timeout=10)
        return SimpleNamespace(size=len(data), readall=lambda: data, properties=SimpleNamespace(etag=etag))

    def upload_blob(self, raw, overwrite, etag=None, **options):
        from azure.core.exceptions import ResourceModifiedError

        with self.lock:
            if etag != str(self.version):
                self.conflicts += 1
                raise ResourceModifiedError("simulated collision")
            self.state = json.loads(raw)
            self.version += 1


def test_blob_ten_concurrent_updates_do_not_lose_posts():
    store = AzureBlobStore.__new__(AzureBlobStore)
    store.blob = FakeBlob()
    store.content_settings = None

    def submit(i):
        def operation(state):
            state["threads"][str(i)] = {"body": f"解説{i}"}
        store.mutate(operation)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(submit, range(10)))
    assert len(store.read()["threads"]) == 10
    assert store.blob.conflicts >= 9


def test_blob_missing_blob_is_empty_but_missing_container_fails():
    from azure.core.exceptions import ResourceNotFoundError

    store = AzureBlobStore.__new__(AzureBlobStore)

    class Missing:
        def __init__(self, code):
            self.code = code

        def download_blob(self):
            exc = ResourceNotFoundError("missing")
            exc.error_code = self.code
            raise exc

    store.blob = Missing("BlobNotFound")
    assert store.read() == empty_state()
    store.blob = Missing("ContainerNotFound")
    with pytest.raises(StorageUnavailable):
        store.read()


def test_storage_failure_shows_retry_without_exception_details(app):
    def broken():
        raise StorageUnavailable("secret-connection-string")

    app.extensions["board_store"].read = broken
    response = app.test_client().get("/")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "10"
    assert "secret-connection-string" not in response.get_data(as_text=True)


def test_secure_session_and_invalid_login(app):
    client = app.test_client()
    register(client)
    client.post("/logout", data={"csrf_token": token(client)})
    response = client.post("/login", data={"csrf_token": token(client, "/login"), "username": "student_one", "password": "wrong-password-123"})
    assert response.status_code == 401
    assert "HttpOnly" in app.test_client().get("/login").headers["Set-Cookie"]
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_existing_public_blob_container_is_rejected():
    store = AzureBlobStore.__new__(AzureBlobStore)
    store.container = SimpleNamespace(create_container=lambda: None, get_container_properties=lambda: {"public_access": "blob"})
    store._container_lock = threading.Lock()
    store._container_ready = False
    with pytest.raises(StorageUnavailable, match="must be private"):
        store._ensure_container()


def test_azure_proxy_port_cannot_bypass_registration_throttle(monkeypatch):
    monkeypatch.setenv("WEBSITE_SITE_NAME", "test-site")
    app = create_app({"TESTING": True, "SECRET_KEY": "test-secret-key-with-at-least-32-characters", "STORAGE_BACKEND": "memory", "SESSION_COOKIE_SECURE": False})
    client = app.test_client()
    csrf = token(client, "/register")
    for i in range(5):
        response = client.post("/register", data={"csrf_token": csrf, "username": "bad!"}, headers={"X-Forwarded-For": f"203.0.113.9:{10000+i}"})
        assert response.status_code == 400
    blocked = client.post("/register", data={"csrf_token": csrf}, headers={"X-Forwarded-For": "203.0.113.9:20000"})
    assert blocked.status_code == 429

