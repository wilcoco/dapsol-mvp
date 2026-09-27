import hashlib
import os
import re
import secrets
import time
from authlib.integrations.starlette_client import OAuth
from .db import one, new_user
from .domain import reject

COOKIE = "dapsol_session"
PROVIDERS = {"google": "Google", "naver": "네이버", "kakao": "카카오"}
oauth = OAuth()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(value, salt=None):
    salt = salt or secrets.token_hex(16)
    raw = hashlib.scrypt(value.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ":" + raw.hex()


def valid_password(value, encoded):
    return secrets.compare_digest(password_hash(value, encoded.split(":")[0]), encoded)


def register(db, data):
    handle = str(data.get("handle", "")).strip().lower()
    password = str(data.get("password", ""))
    name = str(data.get("name", "")).strip()
    if not re.fullmatch(r"[a-z0-9_]{4,32}", handle) or not 12 <= len(password) <= 128 or not 1 <= len(name) <= 60:
        reject("아이디는 영문·숫자·밑줄 4~32자, 비밀번호는 12~128자, 표시 이름은 1~60자로 입력해 주세요.")
    if one(db, "SELECT id FROM users WHERE handle=?", (handle,)):
        reject("사용할 수 없는 아이디입니다.")
    return new_user(db, name, handle, password_hash(password))


def login(db, data):
    handle = str(data.get("handle", "")).strip().lower()[:32]
    password = str(data.get("password", ""))[:129]
    user = one(db, "SELECT * FROM users WHERE handle=?", (handle,))
    encoded = user["password"] if user and user["password"] else password_hash("unused-secret", "00" * 16)
    if not valid_password(password, encoded) or not user:
        reject("아이디 또는 비밀번호를 확인해 주세요.", 401)
    return user


def throttle(db, key, limit=30, period=3600):
    bucket = f"{key}:{int(time.time()) // period}"
    db.execute("INSERT INTO limits VALUES (?,1) ON CONFLICT(key) DO UPDATE SET count=count+1", (bucket,))
    count = one(db, "SELECT count FROM limits WHERE key=?", (bucket,))["count"]
    if count > limit:
        reject("요청이 많습니다. 잠시 후 다시 시도해 주세요.", 429)


def session(db, request):
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    return one(db, """SELECT u.id,u.name,u.handle,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.expires>?""", (digest(token), int(time.time())))


def issue(db, request, response, user, production):
    old = request.cookies.get(COOKIE)
    if old:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(old),))
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (digest(token), user["id"], csrf, int(time.time())+604800))
    request.session.clear()
    request.session["csrf"] = csrf
    response.set_cookie(COOKIE, token, max_age=604800, httponly=True, secure=production, samesite="lax")


def configured(provider):
    return provider in PROVIDERS and bool(os.getenv(f"{provider.upper()}_CLIENT_ID") and os.getenv(f"{provider.upper()}_CLIENT_SECRET"))


def client_for(provider):
    if not configured(provider):
        reject("이 로그인은 운영자가 연결 중입니다. 연결이 완료되면 사용할 수 있습니다.", 503)
    common = dict(client_id=os.environ[f"{provider.upper()}_CLIENT_ID"],
                  client_secret=os.environ[f"{provider.upper()}_CLIENT_SECRET"],
                  client_kwargs={"token_endpoint_auth_method": "client_secret_post", "timeout": 15})
    if provider == "google":
        common["server_metadata_url"] = "https://accounts.google.com/.well-known/openid-configuration"
        common["client_kwargs"].update(scope="openid profile", code_challenge_method="S256")
    elif provider == "naver":
        common.update(authorize_url="https://nid.naver.com/oauth2.0/authorize", access_token_url="https://nid.naver.com/oauth2.0/token")
    else:
        common.update(authorize_url="https://kauth.kakao.com/oauth/authorize", access_token_url="https://kauth.kakao.com/oauth/token")
    return oauth.register(provider, overwrite=True, **common)


async def verified_profile(client, provider, request):
    token = await client.authorize_access_token(request)
    if provider == "google":
        profile = token["userinfo"]
        subject, name = profile.get("sub"), profile.get("name")
    elif provider == "naver":
        response = await client.get("https://openapi.naver.com/v1/nid/me", token=token)
        response.raise_for_status()
        payload = response.json()
        if payload.get("resultcode") != "00":
            raise ValueError("Invalid profile")
        profile = payload["response"]
        subject, name = profile.get("id"), profile.get("nickname")
    else:
        response = await client.get("https://kapi.kakao.com/v2/user/me", token=token)
        response.raise_for_status()
        profile = response.json()
        subject, name = profile.get("id"), (profile.get("properties") or {}).get("nickname")
    if subject is None or not str(subject).strip():
        raise ValueError("Missing subject")
    return str(subject), str(name or "길을 만드는 사람")[:60]


def identity_user(db, provider, subject, name):
    identity = one(db, "SELECT user_id FROM identities WHERE provider=? AND subject=?", (provider, subject))
    if identity:
        return one(db, "SELECT * FROM users WHERE id=?", (identity["user_id"],))
    user = new_user(db, name)
    db.execute("INSERT INTO identities VALUES (?,?,?)", (provider,subject,user["id"]))
    return user
