# 가스안전 Q&A

현재 시행 중인 가스 안전 법령(법률·시행령·시행규칙·행정규칙)과 현행 KGS Code를 근거로 질문에 답하는 Streamlit 앱입니다. 요구사항은 [PRD.md](PRD.md)를 참고하세요.

> 현재 상태: **3단계(UI)**. KGS Code 현행 187건이 색인되어 있고, 법령은 API 키(OC) 발급 후 수집해야 합니다.

## 로컬 실행

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .streamlit\secrets.toml.example .streamlit\secrets.toml   # 필요한 값만 채운다
streamlit run app.py
```

키가 하나도 없어도 앱은 실행됩니다. 키별 동작:

| 키 | 용도 | 없을 때 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 답변 생성 (Claude) | 요약 답변 없이 검색된 근거만 표시 |
| `LAW_API_OC` | 법령 수집 | 법령 수집 불가 (KGS Code는 키 없이 수집) |
| `EMBEDDING_API_KEY` | 의미 검색 (Voyage AI) | 키워드(BM25) 검색만 사용 |
| `ADMIN_PASSWORD` | 관리자 페이지 | 관리자 페이지 비활성 |
| `[supabase] url`, `key` | 질문 로그 저장 | `data/logs/queries.jsonl` (개발용) |

## 데이터 수집·색인

```bash
python -m ingest.build_index                 # 법령 + KGS Code
python -m ingest.build_index --sources kgs   # KGS Code만 (키 불필요, 첫 실행 시 PDF 약 800MB 다운로드)
python -m ingest.build_index --sources law   # 법령만
python -m ingest.build_index --force         # 변경 없는 문서도 다시 처리
python -m ingest.law_api --search-admrul     # 행정규칙 후보 검색 → config/target_laws.yaml의 admin_rules에 기입
```

- 문서별 버전(KGS: `pblcCd`, 법령: 법령일련번호)을 비교해 **바뀐 문서만** 다시 처리합니다.
- 현행 목록에서 빠진 문서(폐지·효력만료)는 청크 파일을 **삭제**해 옛 기준이 검색되지 않게 합니다.
- 결과는 `data/chunks/`(커밋 대상)와 `data/meta.json`에 저장됩니다. 원문 PDF(`data/raw/`)는 git에서 제외됩니다.
- 검색 색인(BM25)은 앱 시작 시 청크로부터 만듭니다(약 5초).

### 법령 수집 시 주의 (국가법령정보 Open API)

1. [open.law.go.kr](https://open.law.go.kr)에서 Open API 사용 신청 → `OC` 발급
2. 신청 시 **호출할 서버의 IP/도메인을 등록**해야 합니다. 미등록 IP에서는 "사용자 정보 검증에 실패" 오류가 납니다.
3. GitHub Actions 러너는 IP가 바뀌므로 법령 수집은 **등록한 PC에서 실행 후 커밋**하세요.

## 테스트

```bash
pytest                                         # 전체
pytest tests/test_retriever.py                 # 파일 하나
pytest tests/test_chunker.py::test_split_long_respects_limit_and_keeps_text   # 테스트 하나
python -m scripts.eval_gate --verbose          # 근거 게이트를 실데이터로 평가 (tests/gate_questions.yaml)
```

`pytest`는 `tests/fixtures/`의 샘플만 사용하며 외부 서비스·키를 쓰지 않습니다. `scripts.eval_gate`는 실제 색인(`data/chunks`)으로 관련 질문은 통과, 무관한 질문은 차단되는지 확인하며, 데이터·토크나이저·문턱값을 바꾼 뒤 반드시 실행합니다(주간 워크플로에서도 실행).

## 배포 후 확인 (사용량 제한)

사용자별 한도는 `X-Forwarded-For`의 **오른쪽에서 `TRUSTED_PROXY_HOPS`번째** IP로 구분합니다(맨 앞 값은 위조 가능). 배포 후 관리자 → 데이터 관리 → **접속 정보**에서 '선택된 IP'가 접속할 때마다 같은 값(본인 IP 앞자리)으로 나오는지 확인하고, 프록시 IP로 나오면 `TRUSTED_PROXY_HOPS`를 조정하세요.

## 배포 (GitHub + Streamlit Community Cloud)

1. GitHub에 푸시 (`data/chunks`, `data/meta.json` 포함)
2. share.streamlit.io → 저장소·`main`·`app.py` 지정, Python 3.13 선택
3. App Settings → Secrets에 `secrets.toml.example` 형식으로 키 등록
4. Supabase를 쓰면 `docs/supabase_schema.sql`을 SQL Editor에서 실행
5. GitHub Actions `Update data` 워크플로가 매주 KGS Code를 갱신·커밋 → Streamlit Cloud 자동 재배포

## 폴더 구조

| 경로 | 설명 |
|---|---|
| `app.py` | 진입점: 페이지 구성(`st.navigation`)만 담당 |
| `views/chat.py` | 질의응답 화면 (질문·답변·근거·피드백) |
| `views/admin.py` | 관리자 화면 (`/admin`): 질문 로그, 근거 없음 목록, 데이터 상태, 재색인 |
| `ui/` | 화면 조각(`components`), 화면 상수·CSS(`styles`) |
| `.streamlit/config.toml` | 라이트·다크 테마 |
| `core/` | 검색(`retriever`), 답변 생성(`generator`, `prompts`), 로그(`logger`), 사용량 제한(`rate_limit`), 임베딩, 설정 |
| `ingest/` | KGS Code 크롤러, 법령 Open API, 청킹, 색인 실행(`build_index`) |
| `config/target_laws.yaml` | 수집 대상 법령 목록 (우선순위 A/B/C) |
| `data/chunks/` | 문서별 청크 JSON (커밋 대상) |
| `docs/supabase_schema.sql` | 질문 로그 테이블 |
| `tests/` | 단위 테스트, 고정 샘플, 품질 평가 질문셋 |
