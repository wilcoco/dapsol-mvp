# Railway 배포와 운영 설정

이 서비스는 기존 서비스와 분리된 GitHub 저장소와 Railway 프로젝트에 배포한다.

## 필수

1. GitHub main을 소스로 연결한다. Dockerfile로 빌드한다.
2. 서비스에 1GB 이상 영속 볼륨을 `/data`에 연결한다. 복제본은 하나만 사용한다.
3. 환경변수:
   - `APP_ENV=production`
   - `BASE_URL=https://<서비스 도메인>` (끝 슬래시 없음)
   - `SESSION_SECRET=<암호학적 난수 32자 이상>`
   - `DATABASE_PATH=/data/dapsol.db`
   - `PORT=8080`
   - `LOCAL_AUTH_ENABLED=true`
4. `/health`가 HTTP 200을 반환하는지 확인한다.
5. 테스트용 계정·자료는 로컬에서만 만든다. 운영 첫 배포에는 예시 고객 데이터를 자동 생성하지 않는다.

`SESSION_SECRET`을 바꾸면 진행 중인 OAuth/폼 세션이 무효화된다. 서버 로그인 토큰은 DB에서 해시로 저장하며 7일 후 만료된다.
라이브 요청 로그에는 초대 링크와 OAuth 코드가 남지 않도록 Uvicorn access log를 끈다.
SQLite 온라인 백업 API 또는 Railway 볼륨 스냅샷으로 백업한다. WAL 모드 DB 파일만 실행 중 복사하면 불완전할 수 있다.
초기 운영 후 볼륨 백업·복구 절차와 주기를 반드시 검증한다. 자동 백업이 설정되어 있다고 가정하지 않는다.

## Google · 네이버 · 카카오

각 제공자의 개발자 콘솔에서 이 서비스용 앱을 준비하고 정확한 callback URL을 등록한다.

| 제공자 | 환경변수 | Callback |
|---|---|---|
| Google | GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET | `/auth/google/callback` |
| 네이버 | NAVER_CLIENT_ID, NAVER_CLIENT_SECRET | `/auth/naver/callback` |
| 카카오 | KAKAO_CLIENT_ID, KAKAO_CLIENT_SECRET | `/auth/kakao/callback` |

Callback은 `BASE_URL` 뒤에 위 경로를 붙인 전체 HTTPS URL이다.
Google은 OIDC·PKCE, 네이버·카카오는 OAuth state와 서버 프로필 응답으로 실제 계정을 확인한다.
카카오 CLIENT_ID는 REST API 키이며 client secret 활성화 구성을 사용한다.
계정 식별자는 provider+subject다. 같은 표시 이름이나 이메일이라는 이유로 계정을 병합하지 않는다.
필수 이름/닉네임 동의 항목 및 제공자의 공개 운영 심사 설정을 확인한다.
시크릿 값은 Railway Variables에서 설정하고 GitHub·채팅·스크린샷에 올리지 않는다.
키가 없으면 로그인 버튼에 연결 준비 중이 표시된다. 실제 제공자 왕복은 키 설정 후 별도 확인해야 한다.

## AI

`ANTHROPIC_API_KEY`와 사용할 `ANTHROPIC_MODEL`을 설정한다.
AI는 본인 대화 내용만 명시적 동의를 받아 전송한다. 그룹 지식을 자동 조회·전송하지 않는다.
하루 20회, 대화당 10회, 출력 1,200토큰, 45초 timeout이다. 실패 요청도 일일 사용량에 포함한다.
대화 공개는 자동으로 이뤄지지 않는다. 사람에게 묻기를 선택하면 검토 가능한 질문 초안으로 넘어간다.

## 파일럿 경계

아이디 비밀번호는 scrypt로 저장한다. 아이디 가입은 이메일·실명 검증을 의미하지 않는다.
계정 복구, 삭제·신고, 보상 분쟁 운영 도구는 다음 단계다. 민감한 실데이터를 받는 대규모 운영으로 확대하기 전에 이 경로를 갖춘다.
MVP에 현금·배당·현상금 기능은 없다. UI와 원장만 보고 정산 가능한 경제 시스템이라고 표현하지 않는다.

## 공식 참고

- [Railway 볼륨](https://docs.railway.com/volumes)
- [Railway 상태 확인](https://docs.railway.com/deployments/healthchecks)
- [Authlib Starlette OAuth](https://docs.authlib.org/en/latest/client/starlette.html)
- [네이버 로그인 API](https://developers.naver.com/docs/login/api/api.md)
- [카카오 로그인 REST API](https://developers.kakao.com/docs/latest/ko/kakaologin/rest-api)
