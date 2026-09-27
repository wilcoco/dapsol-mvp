import json
from datetime import date
from app import ai, human_context
from app.db import connect,one
from test_journey import app, client, post, make_q, latest


def test_context_keeps_author_revision_and_handoff_sources(app,monkeypatch):
    author,reader=client(app,"expert"),client(app,"reader")
    qid=make_q(author)
    post(author,f"/questions/{qid}/answers",body="확인한 경험",conditions="세 명 팀",observed_at="2026-01-01")
    source=latest(app,qid)
    monkeypatch.setattr(ai,"available",lambda:True)
    sent=[]
    async def respond(turns,sources=None):
        sent.extend(sources)
        return "expert의 첫 버전 사례를 참고했습니다."
    monkeypatch.setattr(ai,"respond",respond)
    response=post(reader,"/ai",scope="public",prompt="이 사례를 어떻게 적용할까요?",sources=source["id"],consent="on")
    assert response.status_code==200
    cid=str(response.url).split("/ai/")[1]
    assert sent[0]["author"]=="expert" and sent[0]["id"]==source["id"]
    handoff=reader.get("/new?conversation="+cid)
    assert source["id"] in handoff.text and "expert" in handoff.text
    post(author,f"/revisions/{source['id']}/reconfirm",observed_at="2026-02-01",note="현재도 같은 조건입니다")
    with connect(app.state.database) as db:
        saved=json.loads(one(db,"SELECT snapshot FROM turn_context")["snapshot"])
        assert saved[0]["author_rechecked_at"] is None
    # A later reconfirmation cannot retroactively rewrite the context the model saw.
    assert reader.get("/ai/"+cid).status_code==200


def test_reconfirmation_preserves_observation_and_author_only(app):
    a,b=client(app,"author"),client(app,"other")
    qid=make_q(a)
    post(a,f"/questions/{qid}/answers",body="경험",observed_at="2026-01-01")
    r=latest(app,qid)
    assert post(b,f"/revisions/{r['id']}/reconfirm",observed_at="2026-02-01",note="저도 봤어요").status_code==403
    assert post(a,f"/revisions/{r['id']}/reconfirm",observed_at="2026-02-01",note="변화 없음").status_code==200
    assert latest(app,qid)["observed_at"]=="2026-01-01"
    assert human_context.observation_age("2026-01-01","2026-02-01",date(2026,2,3))==2
    assert human_context.observation_age("",None,date(2026,2,3)) is None
    assert post(a,f"/revisions/{r['id']}/reconfirm",observed_at="2025-12-31",note="과거").status_code==400


def test_ai_cannot_receive_private_source_through_public_conversation(app,monkeypatch):
    from test_journey import get_user
    a,b=client(app,"author"),client(app,"other")
    who=get_user(app,"author")
    q=post(a,"/questions",title="사적인 경험",body="비밀",scope="personal:"+who["id"])
    qid=str(q.url).split("/questions/")[1]
    post(a,f"/questions/{qid}/answers",body="외부 공개 금지")
    r=latest(app,qid)
    monkeypatch.setattr(ai,"available",lambda:True)
    async def respond(*args):
        raise AssertionError("Unauthorized context must never reach the provider")
    monkeypatch.setattr(ai,"respond",respond)
    payload=dict(scope="public",prompt="보여줘",sources=r["id"],consent="on")
    assert post(b,"/ai",**payload).status_code==404
    assert post(a,"/ai",**payload).status_code==400
