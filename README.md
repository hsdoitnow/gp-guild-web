# GP길드 PK 팀 생성 / 전적 관리 웹사이트

기존 PyQt5 프로그램의 핵심 DFS 캐릭터 배정, 4vs4 팀 구성, 점수차 가중 선택을 Python 엔진으로 분리한 웹 버전입니다.

## 기능
- 8명 닉네임, 캐릭터별 가능 여부 및 티어, ALL 선택
- 캐릭만 나누기 / 팀까지 짜기 (각 캐릭 2명, 각 팀 캐릭 1명)
- 점수차 제한, 최근 N판 캐릭 제외, 조합 결과 복사
- 공용 프로필 등록/수정/삭제, JSON 내보내기/가져오기, 티어 점수 설정
- 팀 결과 A/B 승리 등록, 닉네임별 전체/캐릭별 승률, 랭킹, 최근 경기 조회

## 로컬 실행 (Windows PowerShell)

```powershell
cd gp-guild-web
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

http://127.0.0.1:8000 접속. DATABASE_URL 미설정 시 `gp_local.db` SQLite 파일 사용.

## Neon + Render 무료 배포

1. GitHub에 **이 폴더의 파일들을** 새 저장소에 업로드합니다. `.env`, DB 파일, 비밀번호는 업로드하지 않습니다.
2. Neon에서 무료 PostgreSQL 프로젝트를 만들고 연결 문자열을 복사합니다. `sslmode=require`가 포함된 연결 문자열을 사용합니다.
3. Render에서 GitHub 저장소를 연결해 새 Web Service를 생성합니다. `render.yaml` Blueprint를 사용하거나 아래 값을 입력합니다.
   - Runtime: Python / Plan: Free (현재 제공 여부 확인)
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - Environment variable: `DATABASE_URL` = Neon 연결 문자열
4. 배포가 끝나면 Render에서 제공한 `https://...onrender.com` 주소를 공유합니다.
5. `/health`가 `{"status":"ok"}`를 반환하는지 확인하고 프로필 저장, 팀 생성, 승리 등록, 통계 조회를 테스트합니다.

**중요:** Neon 연결 문자열은 브라우저 코드나 GitHub 공개 저장소에 넣지 말고 Render의 환경 변수에만 입력하세요.

## 데이터 및 운영 정책
- 로그인/인증 없음: **누구나** 프로필, 점수, 승리 결과를 수정/등록할 수 있습니다. 실제 공개 운영 시 관리자 권한 또는 초대 코드 도입을 권장합니다.
- 경기 등록 후 수정/삭제 UI는 제공하지 않습니다. 오입력 복구는 DB 관리자 작업이 필요합니다.
- 조합 생성은 DB에 기록되지 않고 **승리 등록**할 때 한 경기가 확정됩니다.
- 최근 N판 제외는 DB에 등록된 최근 경기의 캐릭터를 참조합니다. 원본 앱은 결과 복사 시 이력을 갱신했으므로 웹에서는 의미가 약간 다릅니다.
- 같은 경기 중복 등록을 방지하려고 성공 후 결과 등록 버튼을 숨기지만, 여러 탭/동시 요청에 대한 강한 중복 방지 기능은 아직 없습니다.
- 무료 플랜은 절전/용량/사용량 제한이 있고 정책이 변경될 수 있습니다. 주기적으로 Neon DB를 백업하세요.
- 이 버전은 **MVP**입니다. 고급 계정 관리, 경기 수정/삭제, 관리자 감사 로그, 자동 마이그레이션, 악의적 요청 방어는 추가 개발 대상입니다.

## 파일
- `app.py`: FastAPI, DB 테이블 및 REST API
- `engine.py`: 원본 Python 조합 알고리즘
- `static/index.html`: 반응형 웹 UI
- `requirements.txt`: Python 패키지
- `render.yaml`: Render 배포 템플릿
