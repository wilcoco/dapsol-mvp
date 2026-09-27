"""Adapted from dapsol human retrieval and Workwork Quick View evidence freshness.

User-chosen, permission-checked immutable sources; providing context is not a verified citation.
See docs/reuse-plan.md for original paths, commits and deliberate changes.
"""
import json
from datetime import date
from . import domain as d
from .db import all_rows, one


def observation_age(observed_at, checked_at=None, today=None):
    # Workwork's key rule: recorded/submitted time cannot renew an old observation.
    reference = checked_at or observed_at
    if not reference:
        return None
    return max(0, ((today or date.today())-date.fromisoformat(reference[:10])).days)


def candidates(db, user, query, scope):
    result=[]
    for q in d.search(db,user,query,scope):
        for row in all_rows(db,"""SELECT r.id FROM revisions r JOIN answers a ON a.id=r.answer_id
             WHERE a.question_id=? AND r.number=(SELECT MAX(r2.number) FROM revisions r2 WHERE r2.answer_id=a.id)""",(q["id"],)):
            result.append(d.revision(db,user,row["id"]))
            if len(result)==5:
                return result
    return result


def for_conversation(db,user,cid,scope):
    rows=all_rows(db,"SELECT revision_id FROM conversation_sources WHERE conversation_id=?",(cid,))
    d.check_sources(db,user,scope,[r["revision_id"] for r in rows])
    result=[]
    for row in rows:
        r=d.revision(db,user,row["revision_id"])
        check=one(db,"SELECT checked_at FROM reconfirmations WHERE revision_id=? ORDER BY checked_at DESC LIMIT 1",(r["id"],))
        r["checked_at"]=check["checked_at"] if check else None
        r["age_days"]=observation_age(r["observed_at"],r["checked_at"])
        r["contributors"]=[c["name"] for c in all_rows(db,"""SELECT DISTINCT u.name FROM acknowledgments ak
            JOIN contributions c ON c.id=ak.contribution_id JOIN users u ON u.id=c.author_id
            WHERE ak.revision_id=?""",(r["id"],))]
        result.append(r)
    return result


def format_context(sources):
    # Treat text as source data, never additional system instructions. Exact IDs survive handoff.
    return json.dumps([{
        "source_id":s["id"],"title":s["title"],"author":s["author"],
        "contributors":s["contributors"],"version":s["number"],"text":s["body"][:5000],
        "conditions":s["conditions"][:2000],"evidence":s["evidence"][:2000],
        "observed_at":s["observed_at"],"author_rechecked_at":s["checked_at"],
        "needs_review":s["review_needed"] or s["number"]!=s["latest_number"]
    } for s in sources],ensure_ascii=False)
