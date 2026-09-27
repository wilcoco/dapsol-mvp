import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode
from contextlib import closing
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from . import auth, domain as d, ai
from .db import initialize, connect, transaction, one, all_rows, uid, now

ROOT = Path(__file__).parent


def create_app(database=None, testing=False):
    production = not testing and os.getenv("APP_ENV", "production") == "production"
    base = os.getenv("BASE_URL", "http://127.0.0.1:8010").rstrip("/")
    secret = os.getenv("SESSION_SECRET")
    if production and (not secret or len(secret) < 32 or not base.startswith("https://")):
        raise RuntimeError("Production requires SESSION_SECRET (32+ chars) and HTTPS BASE_URL")
    path = database or os.getenv("DATABASE_PATH", "./data/dapsol.db")
    if production and os.getenv("RAILWAY_ENVIRONMENT_ID"):
        mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
        if not mount or not Path(path).resolve().is_relative_to(Path(mount).resolve()):
            raise RuntimeError("Railway DATABASE_PATH must be inside an attached persistent volume")
    initialize(path)
    app = FastAPI(title="답설", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database = path
    app.state.production = production
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")
    templates.env.filters["date"] = lambda value: str(value)[:10]
    templates.env.filters["query"] = lambda value: urlencode(value)

    def read():
        return closing(connect(path))

    def render(request, name, **context):
        with read() as db:
            user = request.state.user
            groups = all_rows(db, "SELECT s.* FROM spaces s JOIN memberships m ON m.space_id=s.id WHERE m.user_id=?", (user["id"],)) if user else []
        return templates.TemplateResponse(request=request, name=name, context={
            "user": user, "csrf": request.session["csrf"], "groups": groups,
            "scope": "public", "ai_available": ai.available(), **context})

    def require(request):
        if not request.state.user:
            d.reject("로그인하면 질문하고 경험을 나눌 수 있습니다.", 401)
        return request.state.user

    def redirect(url):
        return RedirectResponse(url, status_code=303)

    def question_url(qid, anchor=""):
        return f"/questions/{qid}{anchor}"

    @app.middleware("http")
    async def security(request, call_next):
        with read() as db:
            request.state.user = auth.session(db, request)
        if "csrf" not in request.session:
            request.session["csrf"] = request.state.user["csrf"] if request.state.user else secrets.token_urlsafe(32)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            # Stream with a hard cap rather than trusting a user-supplied Content-Length.
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > 65536:
                    return JSONResponse({"detail": "입력 내용이 너무 큽니다."}, status_code=413)
            request._body = body
            origin = request.headers.get("origin")
            if origin and origin != base:
                return JSONResponse({"detail": "요청 출처를 확인할 수 없습니다."}, status_code=403)
            form = await request.form()
            token = str(form.get("csrf", "")) or request.headers.get("x-csrf-token", "")
            expected = request.state.user["csrf"] if request.state.user else request.session["csrf"]
            if not token or not secrets.compare_digest(token, expected):
                return JSONResponse({"detail": "페이지를 새로고침한 뒤 다시 시도해 주세요."}, status_code=403)
            request.state.form = dict(form)
            # Commit rate accounting independently, including rejected writes and login failures.
            try:
                with transaction(path) as db:
                    who = request.state.user["id"] if request.state.user else auth.digest(request.client.host if request.client else "unknown")
                    auth.throttle(db, "write:" + who, 120, 3600)
                if request.url.path in {"/login", "/register"}:
                    with transaction(path) as db:
                        auth.throttle(db, "login:" + who, 20, 3600)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    # Session middleware must wrap security so the signed OAuth/CSRF session is available.
    app.add_middleware(SessionMiddleware, secret_key=secret or secrets.token_urlsafe(48),
                       session_cookie="dapsol_flow", https_only=production, same_site="lax", max_age=3600)
    if production:
        from urllib.parse import urlparse
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlparse(base).hostname, "healthcheck.railway.app"])

    @app.exception_handler(StarletteHTTPException)
    async def error(request, exc):
        response = render(request, "error.html", message=str(exc.detail), status=exc.status_code)
        response.status_code = exc.status_code
        return response

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request, exc):
        response = render(request, "error.html", message="이미 기록된 요청이거나 동시에 수정되었습니다. 새로고침해 주세요.", status=409)
        response.status_code = 409
        return response

    @app.get("/health")
    async def health():
        with read() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok", "service": "dapsol-mvp", "version": "0.1.0"}

    @app.get("/")
    async def home(request: Request, q: str = "", scope: str = "public", waiting: bool = False):
        with read() as db:
            results = d.search(db, request.state.user, q[:300], scope, waiting)
        return render(request, "home.html", query=q[:300], scope=scope, results=results, waiting=waiting)

    @app.get("/about")
    async def about(request: Request):
        return render(request, "about.html")

    @app.get("/login")
    async def login_page(request: Request):
        return render(request, "login.html", providers=[(p,label,auth.configured(p)) for p,label in auth.PROVIDERS.items()],
                      local_auth=os.getenv("LOCAL_AUTH_ENABLED", "true") == "true")

    @app.post("/register")
    @app.post("/login")
    async def password_login(request: Request):
        if os.getenv("LOCAL_AUTH_ENABLED", "true") != "true":
            d.reject("간편 로그인을 이용해 주세요.", 403)
        with transaction(path) as db:
            user = auth.register(db, request.state.form) if request.url.path == "/register" else auth.login(db, request.state.form)
            response = redirect("/")
            auth.issue(db, request, response, user, production)
        return response

    @app.get("/auth/{provider}")
    async def oauth_start(request: Request, provider: str):
        client = auth.client_for(provider)
        return await client.authorize_redirect(request, base + f"/auth/{provider}/callback")

    @app.get("/auth/{provider}/callback")
    async def oauth_callback(request: Request, provider: str):
        client = auth.client_for(provider)
        try:
            subject, name = await auth.verified_profile(client, provider, request)
        except Exception:
            d.reject("로그인을 완료하지 못했습니다. 로그인 화면에서 다시 시작해 주세요.", 400)
        with transaction(path) as db:
            user = auth.identity_user(db, provider, subject, name)
            response = redirect("/")
            auth.issue(db, request, response, user, production)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        with transaction(path) as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (auth.digest(request.cookies.get(auth.COOKIE, "")),))
        request.session.clear()
        response = redirect("/")
        response.delete_cookie(auth.COOKIE)
        return response

    @app.get("/new")
    async def new_page(request: Request, kind: str = "question", scope: str = "public", source: str = "", conversation: str = "", q: str = ""):
        user = require(request)
        draft, source_rows = {"title":q[:180]}, []
        with read() as db:
            d.allowed_scope(db, user, scope)
            if source:
                r = d.revision(db,user,source)
                scope = r["scope"]
                source_rows = [r]
            if conversation:
                c = conversation_for(db, user, conversation)
                scope = c["scope"]
                turns = all_rows(db,"SELECT * FROM turns WHERE conversation_id=? ORDER BY rowid",(conversation,))
                prompts = [t["body"] for t in turns if t["role"] == "user"]
                draft = {"title": prompts[0][:180] if prompts else "", "body": "\n\n".join(prompts)[:12000],
                         "attempts": "AI와 검토했지만 추가 도움이 필요합니다. 공유할 내용을 직접 정리해 주세요."}
        return render(request,"new.html",kind="knowledge" if kind=="knowledge" else "question", scope=scope,
                      draft=draft,source_rows=source_rows,source=source)

    @app.post("/questions")
    async def post_question(request: Request):
        user = require(request)
        data = request.state.form
        kind = "knowledge" if data.get("kind") == "knowledge" else "question"
        with transaction(path) as db:
            qid = d.create_question(db,user,data,kind)
            if kind == "knowledge":
                d.create_answer(db,user,qid,{**data,"body":data.get("answer", "")})
        return redirect(question_url(qid))

    @app.get("/questions/{qid}")
    async def show_question(request: Request, qid: str):
        user = request.state.user
        with read() as db:
            q = d.question(db,user,qid)
            answers = []
            for a in all_rows(db,"SELECT * FROM answers WHERE question_id=? ORDER BY created_at",(qid,)):
                latest = one(db,"SELECT id FROM revisions WHERE answer_id=? ORDER BY number DESC LIMIT 1",(a["id"],))
                r = d.revision(db,user,latest["id"])
                r["investment_request"] = uid()
                r["sources"] = d.sources_for(db,user,"target_revision",r["id"])
                r["history"] = all_rows(db,"SELECT id,number,reason,created_at FROM revisions WHERE answer_id=? ORDER BY number DESC",(a["id"],))
                r["contributions"] = all_rows(db,"""SELECT c.*,u.name author,r.number version FROM contributions c
                    JOIN users u ON u.id=c.author_id JOIN revisions r ON r.id=c.revision_id
                    WHERE r.answer_id=? ORDER BY c.created_at""",(a["id"],))
                r["acknowledged"] = all_rows(db,"""SELECT c.*,u.name author FROM acknowledgments ak
                    JOIN contributions c ON c.id=ak.contribution_id JOIN users u ON u.id=c.author_id WHERE ak.revision_id=?""",(r["id"],))
                r["outcomes"] = all_rows(db,"""SELECT o.*,u.name author,r.number version FROM outcomes o
                    JOIN users u ON u.id=o.user_id JOIN revisions r ON r.id=o.revision_id
                    WHERE r.answer_id=? ORDER BY o.created_at""",(a["id"],))
                r["reuses"] = all_rows(db,"""SELECT re.*,u.name author,r.number version FROM reuses re
                    JOIN users u ON u.id=re.user_id JOIN revisions r ON r.id=re.revision_id
                    WHERE r.answer_id=? ORDER BY re.created_at""",(a["id"],))
                r["stakes"] = all_rows(db,"""SELECT s.*,u.name author FROM stakes s JOIN users u ON u.id=s.user_id
                    WHERE revision_id=? AND withdrawn_at IS NULL""",(r["id"],))
                answers.append(r)
            selected = one(db,"""SELECT s.*,r.number,r.answer_id,u.name author FROM selections s
                JOIN revisions r ON r.id=s.revision_id JOIN users u ON u.id=s.user_id
                WHERE s.question_id=? ORDER BY s.rowid DESC LIMIT 1""",(qid,))
            links = all_rows(db,"""SELECT DISTINCT q.id,q.title,q.scope FROM sources s
                JOIN questions q ON q.id=s.target_question JOIN revisions r ON r.id=s.source_revision
                JOIN answers a ON a.id=r.answer_id WHERE a.question_id=?""",(qid,))
            visible = d.scopes(db,user)
            links = [l for l in links if l["scope"] in visible]
            source_rows = d.sources_for(db,user,"target_question",qid)
        return render(request,"question.html",q=q,answers=answers,selected=selected,source_rows=source_rows,
                      links=links,scope=q["scope"])

    @app.post("/questions/{qid}/answers")
    async def post_answer(request: Request, qid: str):
        user = require(request)
        with transaction(path) as db:
            rid = d.create_answer(db,user,qid,request.state.form)
        return redirect(question_url(qid,"#r-"+rid))

    @app.get("/revisions/{rid}")
    async def show_revision(request: Request, rid: str):
        with read() as db:
            r = d.revision(db,request.state.user,rid)
            sources = d.sources_for(db,request.state.user,"target_revision",rid)
        return render(request,"revision.html",r=r,source_rows=sources,scope=r["scope"])

    @app.get("/answers/{aid}/edit")
    async def edit_page(request: Request, aid: str):
        user = require(request)
        with read() as db:
            row = one(db,"SELECT id FROM revisions WHERE answer_id=? ORDER BY number DESC LIMIT 1",(aid,))
            r = d.revision(db,user,row["id"] if row else "")
            if r["author_id"] != user["id"]:
                d.reject("작성자만 수정할 수 있습니다.",403)
            cs = all_rows(db,"""SELECT c.*,u.name author FROM contributions c JOIN users u ON u.id=c.author_id
               JOIN revisions r ON r.id=c.revision_id WHERE r.answer_id=?""",(aid,))
        return render(request,"edit.html",r=r,contributions=cs,scope=r["scope"])

    @app.post("/answers/{aid}/edit")
    async def edit_answer(request: Request, aid: str):
        user = require(request)
        with transaction(path) as db:
            a = one(db,"SELECT question_id FROM answers WHERE id=?",(aid,))
            if not a:
                d.reject("답을 찾을 수 없습니다.",404)
            rid = d.create_answer(db,user,a["question_id"],request.state.form,aid)
        return redirect(question_url(a["question_id"],"#r-"+rid))

    @app.post("/revisions/{rid}/{action}")
    async def interact(request: Request, rid: str, action: str):
        user = require(request)
        data = request.state.form
        with transaction(path) as db:
            r = d.revision(db,user,rid)
            q = d.question(db,user,r["question_id"])
            if action == "select":
                if q["author_id"] != user["id"]:
                    d.reject("질문자만 도움된 답을 선택할 수 있습니다.",403)
                last = one(db,"SELECT revision_id FROM selections WHERE question_id=? ORDER BY rowid DESC LIMIT 1",(q["id"],))
                if not last or last["revision_id"] != rid:
                    db.execute("INSERT INTO selections VALUES (?,?,?,?,?)",(uid(),q["id"],rid,user["id"],now()))
            elif action == "bookmark":
                db.execute("INSERT OR IGNORE INTO bookmarks VALUES (?,?)",(user["id"],rid))
            elif action == "reuse":
                note = d.text_field(data,"note","사용 목적",2000,True)
                db.execute("INSERT OR IGNORE INTO reuses VALUES (?,?,?,?,?)",(uid(),rid,user["id"],note,now()))
            elif action == "outcome":
                result = data.get("result")
                if result not in {"solved","partial","failed"}:
                    d.reject("적용 결과를 선택해 주세요.")
                conditions = d.text_field(data,"conditions","적용한 조건",3000,True)
                body = d.text_field(data,"body","실제 결과",6000,True)
                observed = d.observed(data)
                if not observed:
                    d.reject("직접 적용한 날짜를 남겨 주세요.")
                db.execute("INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?)",(uid(),rid,user["id"],result,conditions,body,observed,now()))
            elif action == "contribute":
                kind = data.get("kind")
                if kind not in {"addition","correction","clarification"}:
                    d.reject("추가 정보의 종류를 선택해 주세요.")
                body = d.text_field(data,"body","추가 정보",6000,True)
                db.execute("INSERT INTO contributions VALUES (?,?,?,?,?,?)",(uid(),rid,user["id"],kind,body,now()))
            elif action == "invest":
                try:
                    amount = int(str(data.get("amount","")))
                except ValueError:
                    d.reject("포인트를 정수로 입력해 주세요.")
                d.invest(db,user,rid,amount,data.get("request_id"))
            else:
                d.reject("찾을 수 없는 작업입니다.",404)
        return redirect(question_url(q["id"],"#r-"+rid))

    @app.post("/stakes/{sid}/withdraw")
    async def withdraw(request: Request, sid: str):
        with transaction(path) as db:
            d.withdraw(db,require(request),sid)
        return redirect("/me")

    @app.get("/groups")
    async def group_page(request: Request):
        require(request)
        return render(request,"groups.html")

    @app.post("/groups")
    async def create_group(request: Request):
        user = require(request)
        name = d.text_field(request.state.form,"name","그룹 이름",80,True)
        sid = uid()
        with transaction(path) as db:
            db.execute("INSERT INTO spaces VALUES (?,?,?,?)",(sid,name,user["id"],now()))
            db.execute("INSERT INTO memberships VALUES (?,?)",(sid,user["id"]))
        return redirect("/groups/"+sid)

    def group_owner(db,user,sid):
        d.allowed_scope(db,user,"group:"+sid)
        s = one(db,"SELECT * FROM spaces WHERE id=?",(sid,))
        if s["owner_id"] != user["id"]:
            d.reject("그룹 관리자만 할 수 있습니다.",403)
        return s

    @app.get("/groups/{sid}")
    async def show_group(request: Request, sid: str):
        user = require(request)
        with read() as db:
            d.allowed_scope(db,user,"group:"+sid)
            s = one(db,"SELECT * FROM spaces WHERE id=?",(sid,))
            members = all_rows(db,"SELECT u.id,u.name FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.space_id=?",(sid,))
        return render(request,"group.html",group=s,members=members,invite="",scope="group:"+sid)

    @app.post("/groups/{sid}/invite")
    async def invite(request: Request, sid: str):
        user = require(request)
        token = secrets.token_urlsafe(32)
        with transaction(path) as db:
            s = group_owner(db,user,sid)
            db.execute("INSERT INTO invites VALUES (?,?,?,NULL)",(auth.digest(token),sid,int(time.time())+86400))
            members = all_rows(db,"SELECT u.id,u.name FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.space_id=?",(sid,))
        return render(request,"group.html",group=s,members=members,invite=base+"/join/"+token,scope="group:"+sid)

    @app.get("/join/{token}")
    async def join_page(request: Request, token: str):
        require(request)
        with read() as db:
            invite = one(db,"SELECT i.*,s.name FROM invites i JOIN spaces s ON s.id=i.space_id WHERE token_hash=? AND expires>? AND used_by IS NULL",(auth.digest(token),int(time.time())))
            if not invite:
                d.reject("초대가 만료되었거나 이미 사용되었습니다.",404)
        return render(request,"join.html",invite=invite,token=token)

    @app.post("/join/{token}")
    async def join(request: Request, token: str):
        user = require(request)
        with transaction(path) as db:
            invite = one(db,"SELECT * FROM invites WHERE token_hash=? AND expires>? AND used_by IS NULL",(auth.digest(token),int(time.time())))
            if not invite:
                d.reject("초대가 만료되었거나 이미 사용되었습니다.",404)
            db.execute("INSERT OR IGNORE INTO memberships VALUES (?,?)",(invite["space_id"],user["id"]))
            db.execute("UPDATE invites SET used_by=? WHERE token_hash=?",(user["id"],auth.digest(token)))
        return redirect("/groups/"+invite["space_id"])

    @app.post("/groups/{sid}/remove/{member}")
    async def remove_member(request: Request, sid: str, member: str):
        with transaction(path) as db:
            s = group_owner(db,require(request),sid)
            if member == s["owner_id"]:
                d.reject("그룹 관리자는 내보낼 수 없습니다.")
            db.execute("DELETE FROM memberships WHERE space_id=? AND user_id=?",(sid,member))
        return redirect("/groups/"+sid)

    @app.get("/me")
    async def me(request: Request):
        user = require(request)
        with read() as db:
            visible = d.scopes(db,user)
            marks = ",".join("?" for _ in visible)
            qs = all_rows(db,f"SELECT * FROM questions WHERE author_id=? AND scope IN ({marks}) ORDER BY created_at DESC",(user["id"],*visible))
            answers = all_rows(db,f"""SELECT a.id,q.id question_id,q.title FROM answers a JOIN questions q ON q.id=a.question_id
                WHERE a.author_id=? AND q.scope IN ({marks})""",(user["id"],*visible))
            saved = all_rows(db,f"""SELECT r.id,q.title FROM bookmarks b JOIN revisions r ON r.id=b.revision_id
                JOIN answers a ON a.id=r.answer_id JOIN questions q ON q.id=a.question_id
                WHERE b.user_id=? AND q.scope IN ({marks})""",(user["id"],*visible))
            stakes = all_rows(db,"""SELECT s.*,q.title,q.scope FROM stakes s JOIN revisions r ON r.id=s.revision_id
                JOIN answers a ON a.id=r.answer_id JOIN questions q ON q.id=a.question_id
                WHERE s.user_id=? AND s.withdrawn_at IS NULL""",(user["id"],))
            for s in stakes:
                if s["scope"] not in visible:
                    s["title"] = "접근이 종료된 그룹의 투자 · 원금 회수 가능"
                    s["revision_id"] = None
            ledger = all_rows(db,"SELECT * FROM ledger WHERE user_id=? ORDER BY rowid DESC LIMIT 100",(user["id"],))
            recognition = all_rows(db,f"""SELECT q.id,q.title,
                COUNT(DISTINCT CASE WHEN o.user_id!=a.author_id THEN o.user_id END) applied,
                COUNT(DISTINCT CASE WHEN re.user_id!=a.author_id THEN re.user_id END) reused
                FROM answers a JOIN questions q ON q.id=a.question_id JOIN revisions r ON r.answer_id=a.id
                LEFT JOIN outcomes o ON o.revision_id=r.id LEFT JOIN reuses re ON re.revision_id=r.id
                WHERE a.author_id=? AND q.scope IN ({marks}) GROUP BY q.id""",(user["id"],*visible))
            funds = d.balance(db,user["id"])
            conversations = all_rows(db,"SELECT * FROM conversations WHERE user_id=? ORDER BY created_at DESC",(user["id"],))
            conversations = [c for c in conversations if c["scope"] in visible]
        return render(request,"me.html",questions=qs,answers=answers,saved=saved,stakes=stakes,ledger=ledger,
                      recognition=recognition,funds=funds,conversations=conversations)

    def conversation_for(db,user,cid):
        c = one(db,"SELECT * FROM conversations WHERE id=? AND user_id=?",(cid,user["id"]))
        if not c:
            d.reject("대화를 찾을 수 없습니다.",404)
        d.allowed_scope(db,user,c["scope"])
        return c

    @app.get("/ai")
    async def ai_page(request: Request, scope: str="public", q: str=""):
        user = require(request)
        with read() as db:
            d.allowed_scope(db,user,scope)
        return render(request,"ai.html",conversation=None,turns=[],scope=scope,prompt=q[:300])

    @app.post("/ai")
    async def ai_start(request: Request):
        user = require(request)
        if not ai.available():
            d.reject("AI 연결을 준비 중입니다. 지금은 사람에게 바로 질문할 수 있습니다.",503)
        cid = uid()
        with transaction(path) as db:
            scope = d.allowed_scope(db,user,request.state.form.get("scope","public"))
            db.execute("INSERT INTO conversations VALUES (?,?,?,?)",(cid,user["id"],scope,now()))
        return await ai_turn(request,cid)

    @app.get("/ai/{cid}")
    async def show_ai(request: Request, cid: str):
        user = require(request)
        with read() as db:
            c = conversation_for(db,user,cid)
            turns = all_rows(db,"SELECT * FROM turns WHERE conversation_id=? ORDER BY rowid",(cid,))
        return render(request,"ai.html",conversation=c,turns=turns,scope=c["scope"],prompt="")

    @app.post("/ai/{cid}")
    async def ai_turn(request: Request, cid: str):
        user = require(request)
        if not ai.available():
            d.reject("AI 연결을 준비 중입니다.",503)
        prompt = d.text_field(request.state.form,"prompt","질문",4000,True)
        if request.state.form.get("consent") != "on":
            d.reject("이 대화를 AI 제공자에게 전송하는 데 동의해 주세요.")
        with transaction(path) as db:
            c = conversation_for(db,user,cid)
            turns = all_rows(db,"SELECT * FROM turns WHERE conversation_id=? ORDER BY rowid",(cid,))
            if len(turns) >= 20:
                d.reject("한 대화는 10회까지입니다. 남은 질문을 정리해 사람에게 물어보세요.")
            auth.throttle(db,"ai:"+user["id"],20,86400)
            expected = len(turns)
        try:
            reply = await ai.respond(turns + [{"role":"user","body":prompt}])
        except Exception:
            d.reject("AI 응답을 받지 못했습니다. 잠시 후 다시 시도하거나 사람에게 질문해 주세요.",503)
        with transaction(path) as db:
            conversation_for(db,user,cid)
            count = one(db,"SELECT COUNT(*) n FROM turns WHERE conversation_id=?",(cid,))["n"]
            if count != expected:
                d.reject("다른 창에서 대화가 진행되었습니다. 새로고침해 주세요.",409)
            for role,body in (("user",prompt),("assistant",reply)):
                db.execute("INSERT INTO turns VALUES (?,?,?,?,?)",(uid(),cid,role,body,now()))
        return redirect("/ai/"+cid)

    return app
