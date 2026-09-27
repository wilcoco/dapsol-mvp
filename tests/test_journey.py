import re
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from app.main import create_app
from app.db import connect, transaction, one, all_rows
from app import domain as d, auth


@pytest.fixture
def app(tmp_path):
    return create_app(str(tmp_path / "test.db"), testing=True)


def client(app, handle):
    c = TestClient(app)
    html = c.get("/login").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    r = c.post("/register", data={"csrf":csrf,"handle":handle,"name":handle,"password":"correct horse battery"})
    assert r.status_code == 200, r.text
    return c


def post(c, url, **data):
    html = c.get("/me").text
    csrf = re.search(r'name="csrf" value="([^"]+)"',html).group(1)
    return c.post(url,data={"csrf":csrf,**data})


def make_q(c, **values):
    r = post(c,"/questions",title="테스트 질문",body="검색과 AI로 해결되지 않았습니다.",scope="public",**values)
    assert r.status_code == 200, r.text
    return str(r.url).split("/questions/")[1]


def latest(app, qid):
    with connect(app.state.database) as db:
        return one(db,"SELECT r.* FROM revisions r JOIN answers a ON a.id=r.answer_id WHERE a.question_id=? ORDER BY r.rowid DESC LIMIT 1",(qid,))


def get_user(app, name):
    with connect(app.state.database) as db:
        return one(db,"SELECT * FROM users WHERE handle=?",(name,))


def test_complete_human_journey_preserves_versions_and_credit(app):
    a,b,c = (client(app,n) for n in ("asker","expert","reader"))
    qid=make_q(a)
    assert "답변을 기다리고" in a.get("/questions/"+qid).text
    assert post(b,f"/questions/{qid}/answers",body="직접 해 본 답변",conditions="작은 팀",evidence="현장 경험",observed_at="2026-01-01").status_code==200
    first=latest(app,qid)
    assert post(a,f"/revisions/{first['id']}/select").status_code==200
    assert post(a,f"/revisions/{first['id']}/outcome",result="solved",conditions="팀원 3명",body="업무 누락이 줄었습니다",observed_at="2026-01-02").status_code==200
    assert "테스트 질문" in c.get("/?q=직접").text
    assert post(c,f"/revisions/{first['id']}/reuse",note="우리 모임에 적용 준비").status_code==200
    follow=make_q(c,sources=first["id"])
    assert "expert" in c.get("/questions/"+follow).text
    assert post(c,f"/revisions/{first['id']}/contribute",kind="correction",body="다섯 명일 때 조건을 더해 주세요").status_code==200
    with connect(app.state.database) as db:
        contribution=one(db,"SELECT id FROM contributions")["id"]
    assert post(b,f"/answers/{first['answer_id']}/edit",body="조건을 보강한 답변",reason="reader의 제안을 반영",expected_revision=first["id"],contribution=contribution).status_code==200
    second=latest(app,qid)
    assert second["number"]==2
    page=c.get("/questions/"+qid).text
    assert "함께 다듬은 사람" in page and "reader" in page
    assert "원문이 수정되었습니다" in c.get("/questions/"+follow).text
    assert "직접 해 본 답변" in c.get("/revisions/"+first["id"]).text
    assert "조건을 보강한 답변" not in c.get("/revisions/"+first["id"]).text
    with connect(app.state.database) as db:
        assert one(db,"SELECT revision_id FROM selections")["revision_id"]==first["id"]
        assert one(db,"SELECT author_id FROM answers WHERE id=?",(first["answer_id"],))["author_id"]==get_user(app,"expert")["id"]
    assert "1명" in b.get("/me").text


def test_private_group_acl_every_entry_point_and_revocation(app):
    owner,member,outside=(client(app,n) for n in ("owner","member","outsider"))
    r=post(owner,"/groups",name="비밀 연구실")
    sid=str(r.url).split("/groups/")[1]
    invitation=post(owner,f"/groups/{sid}/invite")
    token=re.search(r'/join/([^"<]+)',invitation.text).group(1)
    assert post(member,"/join/"+token).status_code==200
    assert post(outside,"/join/"+token).status_code==404
    r=post(owner,"/questions",title="기밀비밀표식",body="공개되면 안 되는 관찰",scope="group:"+sid)
    qid=str(r.url).split("/questions/")[1]
    post(member,f"/questions/{qid}/answers",body="기밀답변표식")
    rev=latest(app,qid)
    for browser in (outside,TestClient(app)):
        assert browser.get("/questions/"+qid).status_code==404
        assert browser.get("/revisions/"+rev["id"]).status_code==404
        assert browser.get("/?scope=group:"+sid).status_code==404
        assert "기밀비밀표식" not in browser.get("/?q=기밀").text
    assert post(member,"/questions",title="외부 공유",body="몰래 연결",scope="public",sources=rev["id"]).status_code==400
    assert post(member,f"/revisions/{rev['id']}/invest",amount="10").status_code==200
    with connect(app.state.database) as db:
        stake=one(db,"SELECT id FROM stakes")["id"]
    member_id=get_user(app,"member")["id"]
    assert post(owner,f"/groups/{sid}/remove/{member_id}").status_code==200
    assert member.get("/questions/"+qid).status_code==404
    assert "기밀答" not in member.get("/me").text
    assert "기밀비밀표식" not in member.get("/me").text
    assert post(member,f"/stakes/{stake}/withdraw").status_code==200
    assert "member" in owner.get("/questions/"+qid).text


def test_personal_scope_and_unauthorized_writes(app):
    a,b=client(app,"person_a"),client(app,"person_b")
    user=get_user(app,"person_a")
    r=post(a,"/questions",title="내 초안",body="개인 내용",scope="personal:"+user["id"])
    qid=str(r.url).split("/questions/")[1]
    assert b.get("/questions/"+qid).status_code==404
    assert post(b,f"/questions/{qid}/answers",body="침입").status_code==404
    assert post(b,"/questions",title="대신 쓰기",body="내용",scope="personal:"+user["id"]).status_code==404


def test_auth_csrf_logout_xss_and_name_is_not_identity(app):
    a,b=client(app,"user_one"),client(app,"user_two")
    assert a.post("/questions",data={"title":"CSRF","body":"wrong"}).status_code==403
    anon=TestClient(app)
    assert anon.get("/new").status_code==401
    qid=make_q(a)
    assert post(b,f"/questions/{qid}/answers",body="<script>alert('x')</script>").status_code==200
    r=latest(app,qid)
    page=a.get("/questions/"+qid)
    assert "&lt;script&gt;" in page.text and "<script>alert" not in page.text
    assert page.headers["cache-control"]=="no-store"
    assert post(b,f"/revisions/{r['id']}/select").status_code==403
    assert post(a,f"/answers/{r['answer_id']}/edit",body="hijacked",reason="bad",expected_revision=r["id"]).status_code==403
    cookie=a.cookies.get(auth.COOKIE)
    post(a,"/logout")
    a.cookies.set(auth.COOKIE,cookie)
    assert a.get("/me").status_code==401


def test_stakes_self_first_principal_conservation_idempotent_withdraw(app):
    a,b=client(app,"author"),client(app,"backer")
    qid=make_q(a)
    post(a,f"/questions/{qid}/answers",body="내 지식")
    rev=latest(app,qid)
    assert post(b,f"/revisions/{rev['id']}/invest",amount="5").status_code==400
    assert post(a,f"/revisions/{rev['id']}/invest",amount="10").status_code==200
    assert post(b,f"/revisions/{rev['id']}/invest",amount="20").status_code==200
    author=get_user(app,"author")
    with connect(app.state.database) as db:
        assert d.balance(db,author["id"])==90
        stake=one(db,"SELECT id FROM stakes WHERE user_id=?",(author["id"],))["id"]
    assert post(a,f"/stakes/{stake}/withdraw").status_code==200
    assert post(a,f"/stakes/{stake}/withdraw").status_code==200
    with connect(app.state.database) as db:
        assert d.balance(db,author["id"])==100
        liquid=one(db,"SELECT SUM(amount) n FROM ledger")["n"]
        locked=one(db,"SELECT SUM(amount) n FROM stakes WHERE withdrawn_at IS NULL")["n"]
        assert liquid+locked==200
    assert post(b,f"/revisions/{rev['id']}/invest",amount="nan").status_code==400
    assert post(a,f"/revisions/{rev['id']}/invest",amount="101").status_code==400


def test_concurrent_investment_cannot_overspend(app):
    a=client(app,"author")
    qid=make_q(a)
    post(a,f"/questions/{qid}/answers",body="답")
    rev=latest(app,qid)
    user=get_user(app,"author")
    def invest():
        try:
            with transaction(app.state.database) as db:
                d.invest(db,user,rev["id"],60)
            return True
        except Exception:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _:invest(),range(2)))==1
    with connect(app.state.database) as db:
        assert d.balance(db,user["id"])==40


def test_revision_conflict_duplicate_outcome_and_correction_credit(app):
    a=client(app,"author")
    qid=make_q(a)
    post(a,f"/questions/{qid}/answers",body="v1")
    r=latest(app,qid)
    payload=dict(body="v2",reason="정정",expected_revision=r["id"])
    assert post(a,f"/answers/{r['answer_id']}/edit",**payload).status_code==200
    assert post(a,f"/answers/{r['answer_id']}/edit",**payload).status_code==409
    assert post(a,f"/revisions/{r['id']}/invest",amount="5").status_code==400
    outcome=dict(result="failed",conditions="우리 환경",body="작동하지 않음",observed_at="2026-01-01")
    assert post(a,f"/revisions/{r['id']}/outcome",**outcome).status_code==200
    assert post(a,f"/revisions/{r['id']}/outcome",**outcome).status_code==409
    with connect(app.state.database) as db:
        assert one(db,"SELECT COUNT(*) n FROM outcomes")["n"]==1


def test_write_knowledge_and_all_pages_render(app):
    a=client(app,"writer")
    r=post(a,"/questions",kind="knowledge",title="내 현장 경험",body="상황 설명",answer="질문 없이 쓴 경험",scope="public")
    assert r.status_code==200
    qid=str(r.url).split("/questions/")[1]
    rev=latest(app,qid)
    for url in ("/","/about","/login","/new","/new?kind=knowledge","/groups","/me","/ai",f"/answers/{rev['answer_id']}/edit",f"/revisions/{rev['id']}","/static/style.css","/health"):
        assert a.get(url).status_code==200,url


def test_oauth_missing_configuration_and_stable_provider_identity(app,monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID",raising=False)
    c=TestClient(app)
    assert c.get("/auth/google").status_code==503
    with transaction(app.state.database) as db:
        a=auth.identity_user(db,"google","123","같은 이름")
        b=auth.identity_user(db,"google","456","같은 이름")
        again=auth.identity_user(db,"google","123","새 이름")
        assert a["id"]!=b["id"] and again["id"]==a["id"]


def test_ai_handoff_is_private_and_reviewed(app,monkeypatch):
    from app import ai
    monkeypatch.setattr(ai,"available",lambda:True)
    async def answer(turns, sources=None):
        return "AI의 임시 제안입니다. 현장 확인이 필요합니다."
    monkeypatch.setattr(ai,"respond",answer)
    a,b=client(app,"asker"),client(app,"other")
    r=post(a,"/ai",scope="public",prompt="사람에게 물어야 할 질문",consent="on")
    assert r.status_code==200
    cid=str(r.url).split("/ai/")[1]
    assert "임시 제안" in r.text
    assert b.get("/ai/"+cid).status_code==404
    page=a.get("/new?conversation="+cid)
    assert "사람에게 물어야 할 질문" in page.text
    with connect(app.state.database) as db:
        assert one(db,"SELECT COUNT(*) n FROM questions")["n"]==0
    assert post(a,"/ai/"+cid,prompt="동의 없음").status_code==400


def test_production_requires_secret_and_https(tmp_path,monkeypatch):
    monkeypatch.setenv("APP_ENV","production")
    monkeypatch.delenv("SESSION_SECRET",raising=False)
    with pytest.raises(RuntimeError):
        create_app(str(tmp_path/"prod.db"))


def test_transitive_source_correction_and_explicit_refresh(app):
    a=client(app,"writer")
    user=get_user(app,"writer")
    with transaction(app.state.database) as db:
        q1=d.create_question(db,user,{"title":"원천","body":"관찰","scope":"public"})
        r1=d.create_answer(db,user,q1,{"body":"첫 관찰"})
        q2=d.create_question(db,user,{"title":"해석","body":"해석","scope":"public"})
        r2=d.create_answer(db,user,q2,{"body":"해석한 답","sources":r1})
        q3=d.create_question(db,user,{"title":"재해석","body":"해석","scope":"public"})
        r3=d.create_answer(db,user,q3,{"body":"다시 해석","sources":r2})
        original=d.revision(db,user,r1)
        newer=d.create_answer(db,user,q1,{"body":"정정 관찰","reason":"관찰 정정","expected_revision":r1},original["answer_id"])
        assert d.needs_review(db,r2) and d.needs_review(db,r3)
        middle=d.revision(db,user,r2)
        refreshed=d.create_answer(db,user,q2,{"body":"정정 반영","reason":"원천 정정 반영","expected_revision":r2,"sources":newer},middle["answer_id"])
        assert not d.needs_review(db,refreshed)
        assert d.needs_review(db,r3)
        assert one(db,"SELECT source_revision FROM sources WHERE target_revision=?",(r2,))["source_revision"]==r1


def test_duplicate_invest_request_does_not_debit_again(app):
    a=client(app,"writer")
    qid=make_q(a)
    post(a,f"/questions/{qid}/answers",body="답")
    r=latest(app,qid)
    for _ in range(2):
        assert post(a,f"/revisions/{r['id']}/invest",amount="10",request_id="a"*32).status_code==200
    with connect(app.state.database) as db:
        assert d.balance(db,get_user(app,"writer")["id"])==90


def test_origin_and_future_observations_are_rejected(app):
    a=client(app,"writer")
    qid=make_q(a)
    assert post(a,f"/questions/{qid}/answers",body="답",observed_at="9999-01-01").status_code==400
    csrf=re.search(r'name="csrf" value="([^"]+)"',a.get("/me").text).group(1)
    assert a.post("/questions",data={"csrf":csrf,"body":"내용","title":"다른 출처"},headers={"origin":"https://other.example"}).status_code==403


def test_ai_group_revocation_and_no_auto_publication(app,monkeypatch):
    from app import ai
    monkeypatch.setattr(ai,"available",lambda:True)
    async def answer(turns, sources=None):
        return "기밀 대화"
    monkeypatch.setattr(ai,"respond",answer)
    a,b=client(app,"owner"),client(app,"member")
    sid=str(post(a,"/groups",name="실험실").url).split("/groups/")[1]
    token=re.search(r'/join/([^"<]+)',post(a,f"/groups/{sid}/invite").text).group(1)
    post(b,"/join/"+token)
    cid=str(post(b,"/ai",scope="group:"+sid,prompt="기밀",consent="on").url).split("/ai/")[1]
    assert a.get("/ai/"+cid).status_code==404
    post(a,f"/groups/{sid}/remove/{get_user(app,'member')['id']}")
    assert b.get("/ai/"+cid).status_code==404
    assert post(b,"/ai/"+cid,prompt="더 알려줘",consent="on").status_code==404
    assert b.get("/new?conversation="+cid).status_code==404
