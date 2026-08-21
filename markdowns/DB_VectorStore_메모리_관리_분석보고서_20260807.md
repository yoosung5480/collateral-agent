# DB / Vector Store / 메모리 관리 분석보고서

작성일: 2026-08-07  
분석 기준: `web_service/server.py` 중심, `rag_chat/manager.py`, PDF parser 모듈, 현재 로컬 runtime 상태

---

## 1. 요약 결론

현재 웹서비스에는 **업무 데이터용 정규화 DB**가 없다. SQLite는 `jobs` 테이블 하나만 가지고 있고, 각 작업의 상태 스냅샷을 JSON 문자열로 통째 저장한다. PDF 원본, 슬라이싱 PDF, 파서 결과, pipeline 산출물, RAG 인덱스는 DB가 아니라 `web_service/runtime/jobs/{job_id}/...` 파일시스템에 저장된다.

Vector store도 Chroma/FAISS/Qdrant 같은 별도 벡터 DB가 아니다. RAG 빌드 시 `pipeline_artifacts/**/*.json`과 `framework_output/**/*.json`을 읽어 chunk를 만들고, OpenAI embedding 결과를 `rag/rag_index.json` 하나에 통째 저장하는 **로컬 JSON vector store** 구조다.

메모리 측면에서는 병목이 명확하다.

1. 업로드 요청이 PDF를 base64 JSON으로 받는다.
2. 서버가 base64 문자열 전체를 메모리에 들고 있다가 다시 bytes로 디코딩한다.
3. 각 parser가 PDF 파일 전체를 다시 읽어 base64로 만든다.
4. 감정평가서/저당목록 parser는 PDF page image/contact sheet를 리스트로 전부 만든 뒤 LLM 메시지에 붙인다.
5. `asyncio.gather()`로 등기부/감정평가서/저당목록 파싱을 동시에 실행하고, 각 내부에서도 여러 LLM 호출을 동시에 실행한다.
6. 완료 후에도 `JOBS`, `RAG_SESSIONS`, `RagChatManager`, `InMemorySaver`, `chat_history`, `initial_report`가 서버 메모리에 남는다.

따라서 “이전 state가 뒤에서도 살아있어야 하는가?”에 대한 답은: **일부 UI 상태와 RAG 세션 외에는 오래 살아있을 필요가 없다.** 현재는 작업 완료 후에도 서버 메모리와 디스크에 많이 남는 구조다.

---

## 2. 현재 DB에 무엇이 들어가는가

### 2.1 DB 위치

`web_service/server.py`에서 DB 위치는 다음처럼 정의된다.

- `DATABASE_PATH = LOCAL_DATABASE_PATH` 환경변수가 있으면 그 값
- 없으면 `web_service/runtime/app.db`

근거: `web_service/server.py:36-40`

현재 로컬 확인 결과:

| 항목 | 값 |
|---|---:|
| DB 파일 | `web_service/runtime/app.db` |
| 파일 크기 | 299,008 bytes |
| 테이블 수 | 1개 |
| row 수 | 13개 |
| payload 총량 | 188,810 bytes |
| 최대 payload | 35,753 bytes |
| 상태별 row | completed 6개, failed 7개 |

### 2.2 테이블 구조

실제 테이블은 하나다.

```sql
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
)
```

근거: `web_service/server.py:430-443`

즉, DB 컬럼으로 `status`, `paths`, `cost_summary` 등이 나뉘어 있지 않다. 전부 `payload` JSON 문자열 안에 들어간다.

### 2.3 payload 안에 저장되는 내용

`_write_job_snapshot()`은 `JobState`를 거의 통째 JSON으로 저장한다.

저장되는 대표 필드:

- `job_id`
- `status`
- `current_step`
- `created_at`, `started_at`, `finished_at`
- `error`
- `progress`
- `input_files`
- `paths`
- `steps`
- `cost_summary`
- `rag_cost_summary`
- `rag_ready`
- `rag_chunk_count`
- `initial_report`
- `chat_history`

근거: `web_service/server.py:384-420`

중요한 점: **PDF 원본 bytes, 슬라이싱 PDF bytes, vector embedding 배열 자체는 SQLite DB에 들어가지 않는다.** DB에는 경로와 요약 상태만 들어간다.

### 2.4 서버 재시작 시 동작

서버 시작 시 `restore_jobs_from_database()`가 DB의 모든 payload를 읽어서 `JOBS` 메모리 dict에 복원한다.

- `queued`, `running` 상태였던 작업은 재시작 후 `failed`로 바뀐다.
- 완료/실패 작업은 모두 메모리에 다시 올라온다.

근거: `web_service/server.py:446-481`

의미:

- DB row가 많아지면 서버 시작 시 모든 job metadata가 메모리에 올라온다.
- `initial_report`, `chat_history`, `cost_summary`도 payload 안에 있으므로 누적되면 메모리/DB가 같이 커진다.

---

## 3. 파일시스템에는 무엇이 들어가는가

`paths_for_job()` 기준 job별 저장 구조는 다음과 같다.

```text
web_service/runtime/jobs/{job_id}/
├── uploads/
├── framework_output/
├── pipeline_artifacts/
├── rag/
└── final/담보물배분표.xlsx
```

근거: `web_service/server.py:260-269`

현재 로컬에 남아 있는 완료 job 예시:

| 디렉토리 | 파일 수 | 크기 |
|---|---:|---:|
| `uploads/` | 3 | 94,714,921 bytes |
| `framework_output/` | 29 | 49,244,221 bytes |
| `pipeline_artifacts/` | 23 | 181,682 bytes |
| `rag/` | 4 | 10,309 bytes |
| `final/` | 1 | 10,880 bytes |
| `cost_tracking_data/` | 4 | 51,000 bytes |

해당 job에서는 `rag/rag_index.json`이 아직 없었다. 즉 initial report는 생성됐지만, vector index build API는 실행되지 않은 상태로 보인다.

---

## 4. Vector store는 어떻게 관리되는가

### 4.1 Vector DB가 아니라 JSON 파일이다

RAG manager는 `rag_index.json`을 index 파일로 사용한다.

- `INDEX_FILENAME = "rag_index.json"`
- `USAGE_FILENAME = "rag_usage_summary.json"`
- embedding 모델은 서버에서 `text-embedding-3-small`로 생성된다.

근거:

- `rag_chat/manager.py:23-27`
- `web_service/server.py:44-48`
- `web_service/server.py:213-219`

### 4.2 어떤 파일을 vector store에 넣는가

RAG index build 시 아래 두 루트의 모든 JSON 파일을 수집한다.

- `pipeline_artifacts_dir`
- `framework_output_dir`

근거:

- `RagChatManager.build_chunks()`가 `json_artifact_paths(self.pipeline_artifacts_dir, self.framework_output_dir)`를 호출함: `rag_chat/manager.py:305-309`
- `json_artifact_paths()`는 `root.rglob("*.json")`으로 모든 JSON을 찾음: `rag_chat/manager.py:135-146`

즉 vector store에는 대략 다음이 들어간다.

- `run_manifest.json`
- `merged_title.json`
- `merged_encumbrance.json`
- `real_estate_match_decisions.json`
- `real_estate_match_verification.json`
- `real_estate_table_plan.json`
- `machinery_distribution_base.json`
- `list_match_*.json`
- `machinery_match_verification.json`
- `machinery_table_plan.json`
- `workbook_plan.json`
- `excel_format_plan.json`
- `framework_output/registry/**/*.json`
- `framework_output/appraisal/**/*.json`
- `framework_output/mortgage_list/**/*.json`

### 4.3 chunk 방식

특수 처리:

- `workbook_plan.json`은 별도 함수 `chunks_from_workbook_plan()`으로 상세 chunk 생성
- 나머지 dict/list JSON은 상위 요약과 list/dict 단위 chunk로 변환
- list는 최대 80개, 루트 list는 최대 120개까지만 chunk화

근거:

- `rag_chat/manager.py:153-188`
- `rag_chat/manager.py:191-232`

### 4.4 embedding 저장 방식

`build_index()` 흐름:

1. 모든 JSON artifact를 로드한다.
2. chunk list를 만든다.
3. `texts = [chunk.text for chunk in chunks]` 생성
4. `self.embeddings.embed_documents(texts)`로 embedding 생성
5. 아래 구조를 `rag/rag_index.json`에 저장

```json
{
  "embeddingModel": "text-embedding-3-small",
  "createdAt": "...",
  "chunkCount": 123,
  "workbookSummary": "...",
  "chunks": [
    {
      "source": ".../workbook_plan.json",
      "jsonPath": "$.machinery_comparison.rows[0]",
      "title": "기계기구 대조 행 1",
      "text": "...",
      "embedding": [0.01, -0.02, ...]
    }
  ]
}
```

근거: `rag_chat/manager.py:311-332`

### 4.5 검색 방식

질문 시:

1. `load_index()`가 `rag_index.json` 전체를 메모리로 읽는다.
2. 질문 embedding을 생성한다.
3. 모든 chunk embedding과 cosine similarity를 계산한다.
4. 상위 k개를 LLM 답변 context로 넣는다.

근거: `rag_chat/manager.py:334-351`, `rag_chat/manager.py:371-390`

의미:

- chunk가 많아질수록 매 질문마다 `rag_index.json` 전체 로드 비용이 생긴다.
- embedding 배열까지 JSON 파싱하므로 vector index가 커지면 메모리/CPU 비용이 커진다.
- 현재 구조는 소규모 job에는 단순하지만, 운영형 vector store로는 비효율적이다.

---

## 5. 서버 메모리에 계속 남는 state

### 5.1 전역 메모리 dict

서버에는 전역 dict가 있다.

```python
JOBS: dict[str, JobState] = {}
RAG_SESSIONS: dict[str, RagSession] = {}
```

근거: `web_service/server.py:152-155`

`JOBS`에는 모든 job 상태가 들어간다. `restore_jobs_from_database()`가 DB의 모든 job을 다시 `JOBS`에 올린다.

`RAG_SESSIONS`에는 job별 `RagChatManager`와 `CostTracker`가 들어간다. 한 번 생성된 RAG session은 제거 로직이 없다.

근거:

- RAG session 생성/캐시: `web_service/server.py:800-823`
- 제거 로직 없음: 현재 파일 내 `RAG_SESSIONS.pop(...)` 류 없음

### 5.2 JobState 내부에서 메모리에 남는 것

`JobState`는 다음을 계속 들고 있다.

- paths
- steps
- cost_summary
- rag_cost_summary
- initial_report 전문
- chat_history 전체

근거: `web_service/server.py:109-127`

초기 리포트는 `job.initial_report`와 `chat_history` 양쪽에 들어간다.

근거: `web_service/server.py:850-861`

### 5.3 RAG session 내부에서 메모리에 남는 것

`RagChatManager.__init__()`은 다음을 들고 있다.

- chat model 객체
- embeddings 객체
- `InMemorySaver()`
- workbook summary 문자열
- LangGraph agent

근거: `rag_chat/manager.py:235-262`

`InMemorySaver`는 이름 그대로 프로세스 메모리 기반 checkpointer다. 채팅을 많이 하면 session별 대화 상태가 메모리에 누적될 수 있다.

---

## 6. PDF 처리 과정의 메모리 구조

### 6.1 업로드 단계

브라우저는 PDF를 base64 JSON으로 보낸다. 서버 모델은 base64 문자열을 `content_base64`로 받는다.

근거: `web_service/server.py:75-87`

`save_uploads()`에서 각 PDF마다:

1. base64 문자열이 이미 request 객체 안에 있음
2. `decode_pdf()`가 전체 base64를 bytes로 디코딩
3. `destination.write_bytes(...)`로 전체 bytes를 파일에 기록

근거:

- `decode_pdf()`: `web_service/server.py:253-257`
- `save_uploads()`: `web_service/server.py:484-500`

메모리 관점:

- PDF 원본 N MB
- base64 문자열은 대략 1.33N MB
- 디코딩 bytes N MB
- 요청 처리 중에는 둘이 동시에 존재할 수 있음
- 현재 완료 job의 업로드 PDF 총량은 약 94.7MB였으므로, 요청 순간 base64만 대략 126MB 수준이었을 가능성이 있다.

### 6.2 세 문서 파싱이 동시에 돈다

`run_framework_job_async()`는 등기부, 감정평가서, 저당목록 파싱을 `asyncio.gather()`로 동시에 실행한다.

근거: `web_service/server.py:633-660`

메모리 관점:

- 등기부 parser의 PDF base64
- 감정평가서 parser의 PDF base64 + page images
- 저당목록 parser의 PDF base64 + contact sheets
- LLM 응답 객체
- split 결과 object

이 세 계열이 동시에 피크를 만든다.

### 6.3 등기부 parser

등기부 split parser는 PDF를 통째 base64로 만들어 LLM 메시지에 넣는다.

근거: `담보물배분표_ver2/registry/pdf_llm_parser.py:113-133`

split 단계에서는 `PdfReader`로 원본 PDF를 열고, 임시 디렉토리에 등기부별 temp PDF를 만들며, async 버전에서는 각 temp registry에 대한 section page 판정을 동시에 실행한다.

근거: `담보물배분표_ver2/registry/pdf_llm_parser.py:338-372`

긍정적인 점:

- temp registry PDF는 `TemporaryDirectory` 안에 있어 context 종료 후 디스크에서 제거된다.

위험한 점:

- `source_reader`는 split 종료까지 살아 있다.
- `registry_inputs`에 temp path 목록이 유지된다.
- section 판정 LLM 호출이 동시에 실행되므로 여러 temp PDF base64가 동시에 생길 수 있다.

### 6.4 감정평가서 parser

감정평가서 parser의 메모리 위험이 크다.

- `encode_pdf()`가 파일 전체를 base64로 만든다: `담보물배분표_ver2/appraisal/parser.py:173-190`
- `render_pdf_page_images()`는 모든 page image base64를 list에 담는다: `담보물배분표_ver2/appraisal/parser.py:195-203`
- `build_pdf_with_page_images_message()`는 PDF base64와 모든 page image base64를 한 메시지에 넣는다: `담보물배분표_ver2/appraisal/parser.py:206-229`
- `render_numbered_contact_sheets()`는 page image를 `rendered` 리스트에 전부 담은 뒤 sheet를 만든다: `담보물배분표_ver2/appraisal/parser.py:234-271`
- 토지/건물 추출과 기계기구 추출을 동시에 실행한다: `담보물배분표_ver2/appraisal/parser.py:409-418`

즉 감정평가서 추출 시에는 다음이 동시에 존재할 수 있다.

- split PDF 원본 bytes의 base64
- 고해상도 page PNG base64 리스트
- LangChain message content list
- land/building 추출 task
- machinery 추출 task

현재 코드에는 이 큰 리스트들을 명시적으로 비우거나 `gc.collect()`하는 부분은 없다. 함수가 반환되면 참조가 끊기며 Python GC 대상이 되지만, 동시 실행 중 피크 메모리는 낮추지 못한다.

### 6.5 저당목록 parser

저당목록 parser도 유사하다.

- `encode_pdf()`가 PDF 전체를 base64로 만든다: `담보물배분표_ver2/mortgage_list/parser.py:195-212`
- `render_numbered_contact_sheets()`가 모든 page image를 `rendered` list에 담은 뒤 contact sheet를 만든다: `담보물배분표_ver2/mortgage_list/parser.py:215-251`
- split plan 메시지는 원본 PDF base64 + 전체 contact sheet image base64를 함께 보낸다: `담보물배분표_ver2/mortgage_list/parser.py:254-281`
- 각 목록번호별 split PDF를 모두 만든 뒤, 모든 split PDF 추출 task를 동시에 실행한다: `담보물배분표_ver2/mortgage_list/parser.py:403-412`
- 최종 결과는 `data`와 `self.last_result`에 남는다: `담보물배분표_ver2/mortgage_list/parser.py:443-475`

위험:

- 목록번호가 많으면 split PDF 수만큼 `async_extract_group()`이 동시에 PDF base64를 만든다.
- `self.last_result`가 전체 `MortgageAssetsReport`를 잡고 있어 parser 객체 생존 기간 동안 결과가 남는다.
- 웹서비스에서는 `parser` 지역변수라 함수 종료 후 해제 가능하지만, 실행 중에는 결과 전체를 들고 있다.

### 6.6 RAG build 단계

RAG build 시도 메모리 피크가 있다.

- 모든 JSON artifact를 읽는다.
- 모든 chunk를 만든다.
- 모든 text list를 만든다.
- 모든 embedding vector list를 만든다.
- vector를 포함한 payload 전체를 만든다.
- JSON으로 serialize해 파일에 쓴다.

근거: `rag_chat/manager.py:305-332`

검색 시에도 `load_index()`가 `rag_index.json` 전체를 로드한다.

근거: `rag_chat/manager.py:334-351`

---

## 7. 메모리 해제 파트가 있는가

### 7.1 명시적 해제 있음

일부 있다.

- PyMuPDF document는 `document.close()` 호출이 있음
  - appraisal: `담보물배분표_ver2/appraisal/parser.py:195-203`, `234-251`
  - mortgage_list: `담보물배분표_ver2/mortgage_list/parser.py:215-228`
- registry temp split PDF는 `TemporaryDirectory`로 제거됨
  - `담보물배분표_ver2/registry/pdf_llm_parser.py:347-372`
- Excel 작성에서는 `del prototype_workbook`이 있음
  - `담보물배분표_ver2/excel.py:897`

### 7.2 명시적 해제 없음

아래는 없다.

- request base64 문자열 조기 제거
- decode된 PDF bytes 조기 제거
- parser별 `HumanMessage` content 조기 제거
- image base64 list 조기 제거
- contact sheet `rendered` list streaming 처리
- `asyncio.gather()` 동시성 제한 semaphore
- 완료 job의 `JOBS` 메모리 eviction
- 완료 RAG session의 `RAG_SESSIONS` eviction
- `InMemorySaver` 대화 상태 정리
- 오래된 runtime job 디렉토리 삭제 정책
- SQLite jobs row TTL/archiving

---

## 8. 지금 구조에서 꼭 살아있어야 하는 state / 없어도 되는 state

### 8.1 요청 처리 중 꼭 필요한 것

| state | 필요한 기간 |
|---|---|
| 업로드 PDF bytes | 파일 저장 완료 전까지만 |
| 원본 PDF 파일 | parser 완료 전, 재현/감사용이면 디스크 보존 |
| split PDF | 해당 추출 LLM 호출 완료 전, 감사/디버깅용이면 디스크 보존 |
| parser Pydantic 결과 | JSON 저장 완료 전까지만 |
| pipeline 중간 dict | 다음 stage JSON 생성 전까지만 |
| workbook_plan | Excel 생성 및 RAG 요약 생성 전까지 |
| format_plan | Excel 생성 전까지 |

### 8.2 장기 메모리에 없어도 되는 것

| state | 현재 위치 | 장기 메모리 필요성 |
|---|---|---|
| 완료 job의 상세 steps 전체 | `JOBS` | 낮음. DB에서 필요 시 조회 가능 |
| `initial_report` 전문 | `JOBS`, DB, `rag/initial_report.md` | 낮음. 파일 경로만 둬도 됨 |
| 전체 `chat_history` | `JOBS`, DB | 낮음~중간. 최근 N개만 메모리 보관 가능 |
| RAG manager 객체 | `RAG_SESSIONS` | 낮음. 질문 시 재생성 가능 |
| InMemorySaver 전체 대화 | `RagChatManager` | 낮음. 최근 대화만 보관하거나 DB/file 기반으로 전환 가능 |
| parser `last_result` | `MortgageParser` | 웹 job에서는 함수 종료 후 불필요 |

---

## 9. 주요 위험도 순위

### 1순위: base64 JSON 업로드

대용량 PDF일수록 가장 먼저 터질 수 있다. request body에 base64 문자열이 들어오고, 저장 시 bytes로 한 번 더 만든다.

개선 방향:

- multipart upload로 전환
- 업로드 파일 stream write
- object storage/direct upload 고려
- 요청 body size 제한 명시

### 2순위: parser의 PDF base64 + page image base64 중복

LLM 메시지마다 PDF 원본 base64와 image base64가 동시에 들어간다. 특히 감정평가서의 `render_pdf_page_images()`는 전체 페이지 이미지를 list에 담는다.

개선 방향:

- page image를 한 번에 모두 만들지 말고 필요한 페이지/청크 단위로 생성
- PDF file input과 image input을 중복 전송하지 않는 방향 검토
- contact sheet는 전체 `rendered` list 없이 sheet 단위 streaming 생성

### 3순위: `asyncio.gather()` 무제한 동시성

세 문서 파싱이 동시에 돌고, 내부에서도 split별 task가 동시에 돈다.

개선 방향:

- job 내부 LLM/PDF 처리 semaphore 적용
- 등기부/감정평가서/저당목록 중 대형 이미지 생성 단계는 순차화
- 목록번호별 저당목록 추출 동시성 제한

### 4순위: RAG JSON vector store

현재는 작은 규모에는 괜찮지만, `rag_index.json`이 커지면 매 검색마다 전체 로드 + 전체 cosine 계산이 발생한다.

개선 방향:

- embedding을 SQLite 별도 테이블 또는 FAISS/Chroma로 분리
- source metadata와 vector 분리 저장
- `rag_index.json` 전체 로드 대신 mmap/DB query/index 사용
- artifact 전체가 아니라 workbook_plan 중심으로 indexing 범위 축소

### 5순위: 완료 job/RAG session 메모리 누적

`JOBS`와 `RAG_SESSIONS`에 eviction이 없다.

개선 방향:

- 완료 후 `JOBS[job_id]`는 compact payload만 유지
- `RAG_SESSIONS` TTL 적용
- initial_report 전문은 메모리가 아니라 파일에서 lazy load
- chat_history는 최근 N개만 메모리 보관

---

## 10. DB 관리 개선 제안

### 10.1 현재 DB의 문제

- jobs 테이블 하나에 JSON payload 통째 저장
- status별 query가 SQL로 직접 안 됨
- payload가 커질수록 dashboard/restore가 무거워짐
- chat_history, initial_report까지 payload에 중복 저장
- runtime 파일과 DB row의 lifecycle이 분리되어 있음

### 10.2 최소 개선

현재 구조를 크게 바꾸지 않는다면:

1. `jobs` 테이블에 자주 쓰는 컬럼 추가
   - `status`
   - `current_step`
   - `progress`
   - `finished_at`
   - `final_output_path`
   - `rag_ready`
2. `payload`에는 상세 snapshot만 유지
3. `initial_report` 전문은 DB에 넣지 말고 path만 저장
4. `chat_history`는 별도 `job_messages` 테이블로 분리
5. 완료/실패 후 N일 지난 job은 runtime 디렉토리와 DB row를 함께 archive/delete

### 10.3 권장 스키마 방향

```text
jobs
- job_id PK
- status
- current_step
- progress
- created_at
- started_at
- finished_at
- error
- input_files_json
- paths_json
- cost_summary_path
- rag_ready
- rag_index_path

job_steps
- job_id
- step_id
- status
- detail
- started_at
- finished_at

job_messages
- id
- job_id
- role
- message
- sources_json
- created_at
- type

job_artifacts
- id
- job_id
- kind
- path
- size_bytes
- created_at
```

---

## 11. Vector store 관리 개선 제안

### 11.1 현재 구조 유지 시 최소 개선

- `build_index()` 전에 기존 `rag_index.json` 있으면 재사용
- artifact hash가 바뀐 경우에만 rebuild
- `framework_output` 전체가 아니라 필요한 JSON만 포함
- embedding 저장 JSON에서 `text`와 `embedding`을 분리
- 검색 시 `rag_index.json` 전체를 매번 읽지 않고 session 내부 cache를 쓰되 TTL로 제거

### 11.2 구조 개선 시

운영형으로 가려면:

- SQLite vector table + sqlite-vss 또는 sqlite-vec
- FAISS local index
- Chroma persistent client

중 하나가 적합하다.

현 프로젝트 규모에서는 외부 서버형 Qdrant까지는 과할 수 있다. 다만 job이 누적되고 질문이 많아지면 JSON vector store는 병목이 된다.

---

## 12. 메모리 최적화 우선순위

### 즉시 효과 큰 순서

1. **업로드를 base64 JSON에서 multipart streaming으로 변경**
2. **세 문서 parser 동시 실행 제한**
3. **감정평가서/저당목록 page image/contact sheet 생성 streaming화**
4. **저당목록 split PDF별 추출 동시성 제한**
5. **완료 job의 `JOBS` payload compact화**
6. **`RAG_SESSIONS` TTL/eviction 추가**
7. **initial_report/chat_history DB 중복 저장 축소**
8. **RAG index 범위 축소 또는 vector DB 전환**
9. **runtime job 보존기간/삭제정책 도입**

### 가장 먼저 봐야 할 코드 위치

| 목적 | 파일/위치 |
|---|---|
| 업로드 메모리 | `web_service/server.py:75-87`, `253-257`, `484-500` |
| job 동시 파싱 | `web_service/server.py:633-660` |
| 감정평가서 이미지 메모리 | `담보물배분표_ver2/appraisal/parser.py:195-229`, `234-298` |
| 저당목록 contact sheet 메모리 | `담보물배분표_ver2/mortgage_list/parser.py:215-281` |
| 저당목록 split 동시성 | `담보물배분표_ver2/mortgage_list/parser.py:403-412` |
| DB snapshot | `web_service/server.py:384-420` |
| DB restore | `web_service/server.py:446-481` |
| RAG session cache | `web_service/server.py:800-823` |
| RAG index build/load | `rag_chat/manager.py:305-351` |

---

## 13. 최종 판단

현재 시스템은 “파일 기반 batch 처리 + SQLite 상태 스냅샷 + JSON vector store” 구조다. DB에 업무 원천 데이터가 체계적으로 들어가는 구조가 아니라, 대부분의 실데이터는 runtime 파일로 흩어져 있다.

메모리 비용 문제는 단일 원인이 아니라 다음 4개가 겹친 결과다.

1. base64 업로드
2. PDF 전체 base64 재생성
3. page image/contact sheet 전체 list 생성
4. 무제한에 가까운 동시 LLM/PDF 처리

따라서 DB 관리보다 먼저, 대용량 PDF 처리 메모리 피크를 낮추는 것이 더 시급하다. DB는 이후 운영 안정성을 위해 정규화/TTL/아카이빙 정책을 넣는 순서가 맞다.
