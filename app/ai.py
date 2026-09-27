"""Private, optional AI help. Generated text is never published automatically."""
import os
import httpx


def available():
    return bool(os.getenv("ANTHROPIC_API_KEY") and os.getenv("ANTHROPIC_MODEL"))


async def respond(turns):
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post("https://api.anthropic.com/v1/messages", headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"
        }, json={"model": os.environ["ANTHROPIC_MODEL"], "max_tokens": 1200,
            "system": "당신은 답설의 질문 정리 도우미입니다. 한국어로 답하세요. 사용자의 관찰과 당신의 추정을 구분하세요. 출처나 경험을 지어내지 마세요. 모르는 부분, 필요한 조건, 사람이 확인할 질문을 밝혀 주세요. 답변했다고 문제가 해결되었다고 단정하지 마세요.",
            "messages": [{"role": t["role"], "content": t["body"]} for t in turns]})
        response.raise_for_status()
        blocks = response.json().get("content", [])
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if not text:
            raise ValueError("Empty AI response")
        return text[:18000]
