# 이번 작업의 개발 도구

- Context Mode 1.0.169: 별도 개발 도구 디렉터리에 설치, SQLite 의존성 빌드, 문서 4개/21섹션 색인과 검색 실행 확인. Codex MCP 설정 추가. 전체 도구 출력을 강제로 가로채는 전역 hooks는 설치하지 않았다.
- Ponytail: 공식 GitHub `skills/ponytail`을 프로젝트의 `.agents/skills/ponytail`에 설치. 기존 구현·표준 라이브러리·기본 HTML 기능 우선 원칙을 적용. 로컬 개발 스킬은 서비스 이미지나 공개 코드에 포함하지 않는다.
- JetBrains: 설치되어 있는 PyCharm Community Edition 2024.2.4를 확인. 중복 IDE 설치나 Junie/MCP 연결은 하지 않았다. 사용자가 다른 JetBrains 도구를 뜻했다면 정확한 제품을 확인한 뒤 연결한다.

Codex CLI의 plugin 명령은 이 실행 환경에서 홈 디렉터리를 찾지 못해 사용할 수 없었다.
그래서 Ponytail은 공식 skill-installer로 프로젝트에 설치했고 Context Mode는 기존 TOML 설정을 보존하며 MCP 항목만 추가했다.
전역 Ponytail 경로는 쓰기 허용 후에도 Windows 폴더 생성이 실패하여 프로젝트 설치를 사용했다.
새 MCP 연결과 스킬의 자동 로딩은 다음 턴/앱 재시작 시 확인한다. 현재 대화에서는 Context Mode CLI 검색과 읽은 Ponytail 스킬을 사용했다.

도구 설치가 제품의 동작 검증을 대신하지 않는다. 테스트·브라우저 시나리오·배포 상태는 별도로 확인한다.

- [Context Mode](https://github.com/mksglu/context-mode)
- [Ponytail](https://github.com/DietrichGebert/ponytail)
- [JetBrains Junie](https://www.jetbrains.com/help/ai-assistant/junie-agent.html)
