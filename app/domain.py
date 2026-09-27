"""Shared visibility and immutable knowledge provenance, used by every entry point."""
from datetime import date
from fastapi import HTTPException
from .db import all_rows, one, uid, now


def reject(message, code=400):
    raise HTTPException(code, message)


def text_field(data, key, label, maximum=12000, required=False):
    value = str(data.get(key, "")).strip()
    if len(value) > maximum or (required and not value):
        reject(f"{label}: {'내용을 입력해 주세요' if not value else f'{maximum}자 이내로 입력해 주세요'}.")
    return value


def observed(data):
    value = text_field(data, "observed_at", "경험한 날짜", 10)
    if value:
        try:
            valid = date.fromisoformat(value) <= date.today()
        except ValueError:
            valid = False
        if not valid:
            reject("경험한 날짜는 오늘 또는 과거 날짜로 입력해 주세요.")
    return value


def scopes(db, user):
    result = ["public"]
    if user:
        result += ["personal:" + user["id"]]
        result += ["group:" + r["space_id"] for r in all_rows(db,
            "SELECT space_id FROM memberships WHERE user_id=?", (user["id"],))]
    return result


def allowed_scope(db, user, scope):
    if scope not in scopes(db, user):
        reject("찾을 수 없거나 접근할 수 없는 공간입니다.", 404)
    return scope


def question(db, user, question_id):
    q = one(db, "SELECT q.*,u.name author FROM questions q JOIN users u ON u.id=q.author_id WHERE q.id=?", (question_id,))
    if not q:
        reject("질문을 찾을 수 없습니다.", 404)
    allowed_scope(db, user, q["scope"])
    return q


def revision(db, user, revision_id):
    r = one(db, """SELECT r.*,a.question_id,a.author_id,u.name author FROM revisions r
        JOIN answers a ON a.id=r.answer_id JOIN users u ON u.id=a.author_id WHERE r.id=?""", (revision_id,))
    if not r:
        reject("답을 찾을 수 없습니다.", 404)
    q = question(db, user, r["question_id"])
    r["scope"] = q["scope"]
    r["title"] = q["title"]
    r["latest_number"] = one(db, "SELECT MAX(number) n FROM revisions WHERE answer_id=?", (r["answer_id"],))["n"]
    r["review_needed"] = needs_review(db, revision_id)
    return r


def needs_review(db, revision_id):
    # UNION de-duplicates shared ancestors; a copied citation is never another observation.
    return bool(one(db, """WITH RECURSIVE ancestors(id) AS (
        SELECT source_revision FROM sources WHERE target_revision=?
        UNION SELECT s.source_revision FROM sources s JOIN ancestors a ON s.target_revision=a.id
      ) SELECT 1 found FROM ancestors a JOIN revisions r ON r.id=a.id
      WHERE r.number < (SELECT MAX(latest.number) FROM revisions latest WHERE latest.answer_id=r.answer_id)
      LIMIT 1""", (revision_id,)))


def source_ids(data):
    values = str(data.get("sources", "")).replace(",", " ").split()
    result = list(dict.fromkeys(values))
    if len(result) > 5 or any(len(v) != 32 for v in result):
        reject("참고할 답은 최대 5개까지 연결할 수 있습니다.")
    return result


def check_sources(db, user, scope, ids, target_answer=None):
    for source_id in ids:
        r = revision(db, user, source_id)
        if r["scope"] not in ("public", scope):
            reject("참고한 답의 공개 범위가 다릅니다. 같은 공간에서 이어 주세요.")
        if target_answer and r["answer_id"] == target_answer:
            reject("자기 답의 이전 버전은 자동으로 보존되므로 출처에 추가하지 않아도 됩니다.")


def create_question(db, user, data, kind="question"):
    scope = allowed_scope(db, user, str(data.get("scope", "public")))
    ids = source_ids(data)
    check_sources(db, user, scope, ids)
    values = [text_field(data, k, label, maximum, required) for k, label, maximum, required in (
        ("title", "제목", 180, True), ("body", "질문 또는 경험", 12000, True),
        ("conditions", "적용 조건", 3000, False), ("attempts", "시도한 것", 4000, False),
        ("need", "필요한 도움", 3000, False))]
    qid = uid()
    db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?,?)",
               (qid, user["id"], scope, *values, kind, now()))
    for rid in ids:
        db.execute("INSERT INTO sources(target_question,source_revision) VALUES (?,?)", (qid, rid))
    return qid


def create_answer(db, user, qid, data, answer_id=None):
    q = question(db, user, qid)
    body = text_field(data, "body", "답변", required=True)
    conditions = text_field(data, "conditions", "적용 조건", 3000)
    evidence = text_field(data, "evidence", "근거와 경험", 4000)
    observation = observed(data)
    ids = source_ids(data)
    check_sources(db, user, q["scope"], ids, answer_id)
    reason = text_field(data, "reason", "수정한 이유", 2000, bool(answer_id))
    number = 1
    previous = None
    if answer_id:
        answer = one(db, "SELECT * FROM answers WHERE id=? AND question_id=?", (answer_id, qid))
        if not answer or answer["author_id"] != user["id"]:
            reject("답변 작성자만 수정할 수 있습니다.", 403)
        previous = one(db, "SELECT * FROM revisions WHERE answer_id=? ORDER BY number DESC LIMIT 1", (answer_id,))
        if data.get("expected_revision") != previous["id"]:
            reject("답이 먼저 수정되었습니다. 새로고침한 뒤 다시 작성해 주세요.", 409)
        number = previous["number"] + 1
    else:
        answer_id = uid()
        db.execute("INSERT INTO answers VALUES (?,?,?,?)", (answer_id, qid, user["id"], now()))
    rid = uid()
    db.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rid, answer_id, number, body, conditions, evidence, observation, reason,
         1 if data.get("ai_assisted") == "on" else 0, now()))
    # A revision retains inherited provenance; a subsequent edit cannot erase a contributor.
    if previous:
        replacements = {revision(db,user,sid)["answer_id"] for sid in ids}
        inherited = all_rows(db, """SELECT s.source_revision,r.answer_id FROM sources s
            JOIN revisions r ON r.id=s.source_revision WHERE s.target_revision=?""", (previous["id"],))
        ids += [r["source_revision"] for r in inherited if r["answer_id"] not in replacements]
        db.execute("INSERT INTO acknowledgments SELECT ?, contribution_id FROM acknowledgments WHERE revision_id=?", (rid, previous["id"]))
    for sid in set(ids):
        db.execute("INSERT INTO sources(target_revision,source_revision) VALUES (?,?)", (rid, sid))
    cid = str(data.get("contribution", "")).strip()
    if cid:
        c = one(db, """SELECT c.id FROM contributions c JOIN revisions r ON r.id=c.revision_id
                    WHERE c.id=? AND r.answer_id=?""", (cid, answer_id))
        if not c:
            reject("이 답에 남긴 보강·정정만 반영할 수 있습니다.")
        db.execute("INSERT OR IGNORE INTO acknowledgments VALUES (?,?)", (rid, cid))
    return rid


def balance(db, user_id):
    return one(db, "SELECT COALESCE(SUM(amount),0) total FROM ledger WHERE user_id=?", (user_id,))["total"]


def invest(db, user, rid, amount, request_id=None):
    r = revision(db, user, rid)
    if request_id:
        import re
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            reject("투자 요청을 확인할 수 없습니다. 새로고침해 주세요.")
        prior = one(db, "SELECT * FROM stakes WHERE id=?", (request_id,))
        if prior:
            if prior["user_id"] != user["id"] or prior["revision_id"] != rid or prior["amount"] != amount:
                reject("중복 요청의 내용이 다릅니다.", 409)
            return request_id
    if r["number"] != r["latest_number"]:
        reject("수정 전 답에는 새로 투자할 수 없습니다. 최신 답을 확인해 주세요.")
    if amount < 1 or amount > 100:
        reject("1~100포인트를 입력해 주세요.")
    if user["id"] != r["author_id"] and not one(db,
        "SELECT id FROM stakes WHERE revision_id=? AND user_id=? AND withdrawn_at IS NULL", (rid,r["author_id"])):
        reject("작성자가 이 버전에 먼저 투자해야 참여할 수 있습니다.")
    if balance(db, user["id"]) < amount:
        reject("사용할 수 있는 포인트가 부족합니다.")
    sid = request_id or uid()
    db.execute("INSERT INTO stakes VALUES (?,?,?,?,?,NULL)", (sid, rid, user["id"], amount, now()))
    db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?)", (uid(), user["id"], -amount, "stake", sid, now()))
    return sid


def withdraw(db, user, sid):
    s = one(db, "SELECT * FROM stakes WHERE id=? AND user_id=?", (sid, user["id"]))
    if not s:
        reject("투자를 찾을 수 없습니다.", 404)
    if s["withdrawn_at"]:
        return
    # Removal from a group never traps the former member's own principal.
    db.execute("UPDATE stakes SET withdrawn_at=? WHERE id=?", (now(), sid))
    db.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?)", (uid(), user["id"], s["amount"], "withdraw", sid, now()))


def sources_for(db, user, key, target):
    assert key in {"target_question", "target_revision"}
    result = []
    for row in all_rows(db, f"SELECT source_revision FROM sources WHERE {key}=?", (target,)):
        result.append(revision(db, user, row["source_revision"]))
    return result


def search(db, user, query="", scope="public", waiting=False):
    allowed_scope(db, user, scope)
    terms = query.casefold().split()[:8]
    rows = all_rows(db, """SELECT q.*,u.name author,
       (SELECT COUNT(*) FROM answers a WHERE a.question_id=q.id) answer_count,
       (SELECT COUNT(*) FROM selections s WHERE s.question_id=q.id) selection_count
       FROM questions q JOIN users u ON u.id=q.author_id WHERE q.scope=? ORDER BY q.created_at DESC LIMIT 500""", (scope,))
    result = []
    for q in rows:
        if waiting and q["selection_count"]:
            continue
        rs = all_rows(db, """SELECT r.* FROM revisions r JOIN answers a ON a.id=r.answer_id
            WHERE a.question_id=? AND r.number=(SELECT MAX(r2.number) FROM revisions r2 WHERE r2.answer_id=a.id)""", (q["id"],))
        haystack = " ".join([q["title"],q["body"],q["conditions"]]+[r["body"] for r in rs]).casefold()
        if terms and not all(t in haystack for t in terms):
            continue
        q["preview"] = (rs[0]["body"] if rs else q["body"])[:170]
        q["review_needed"] = any(needs_review(db,r["id"]) for r in rs)
        q["score"] = sum(3 if t in q["title"].casefold() else 1 for t in terms)
        q["outcome_count"] = one(db, """SELECT COUNT(DISTINCT o.user_id) n FROM outcomes o
            JOIN revisions r ON r.id=o.revision_id JOIN answers a ON a.id=r.answer_id
            WHERE a.question_id=? AND o.user_id!=a.author_id""", (q["id"],))["n"]
        result.append(q)
    # Outcome evidence breaks text ties only; investment does not buy relevance.
    return sorted(result,key=lambda q:(q["score"],min(q["outcome_count"],3),q["created_at"]),reverse=True)[:50]
