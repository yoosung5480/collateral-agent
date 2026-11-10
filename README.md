# 성현회계법인 담보물배분표 자동화

등기부등본, 감정평가서, 공장 및 광업재단 저당법 제6조 목록 PDF를 분석해 회계 검토용 `담보물배분표.xlsx`를 생성하는 Python/LLM 기반 자동화 프로젝트입니다. CLI 배치 실행과 FastAPI 웹서비스를 함께 제공합니다.

## 1. 프로젝트 배경

회생·담보 평가 업무에서는 여러 PDF 문서의 담보권, 감정평가액, 기계기구 목록을 사람이 대조해 Excel 배분표를 작성해야 합니다. 이 저장소는 다음 반복 작업을 자동화합니다.

- 등기부등본에서 표제부·을구 담보설정 정보를 구조화
- 감정평가서에서 토지·건물 및 기계기구 감정평가명세표를 구조화
- 저당목록에서 목록번호별 기계기구 항목을 구조화
- 문서 간 담보물·평가액·목록번호를 대조
- 검증된 계획을 기반으로 최종 Excel 담보물배분표 생성
- 웹 UI에서 PDF 업로드, 진행상태 확인, 결과 다운로드, 결과 RAG 질의응답 제공

> 주의: 실제 PDF, JSON 중간산출물, Excel 결과물, `.env` 비밀값은 GitHub에 올리지 않도록 `.gitignore`에서 제외합니다.

## 2. 저장소 구조

```text
.
├── README.md
├── requirements.txt
├── rag_chat/                    # 결과 JSON 기반 RAG/채팅 매니저
├── web_service/                 # FastAPI 서버와 정적 웹 UI
│   ├── server.py
│   └── static/
├── src/              # 핵심 담보물배분표 자동화 프레임워크
│   ├── main.py                  # CLI 배치 실행 진입점
│   ├── excel.py                 # Excel 생성 로직
│   ├── pipeline.py              # 문서 대조/검증 파이프라인
│   ├── matching.py
│   ├── validate_output.py
│   ├── registry/                # 등기부등본 파서
│   ├── appraisal/               # 감정평가서 파서
│   ├── mortgage_list/           # 저당목록 파서
│   ├── pdf_validater/           # 입력 PDF 형식 검사
│   ├── cost_tracking/           # LLM 사용량/비용 추적 코드
│   └── markdowns/               # 개발 문서와 구현 지침
└── markdowns/                   # 프로젝트 분석/인수인계 문서
```

로컬에만 존재해야 하는 폴더/파일:

- `src/.env`
- `src/datas/`
- `src/output*/`
- `src/output_sample/`
- `src/GT/`
- `web_service/runtime/`
- 모든 PDF/JSON/XLSX 중간산출물

## 3. 설치 방법

### 3.1 Python 환경 준비

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3.2 환경변수 설정

```bash
cp src/.env.example src/.env
```

`src/.env`에 OpenAI API 키를 입력합니다.

```env
OPENAI_API_KEY=sk-...
```

선택값:

```env
USD_KRW_RATE=1350
PDF_VALIDATION_MODEL_NAME=gpt-5.4-mini
```

### 3.3 입력 PDF 준비

CLI 배치 실행은 `src/main.py` 상단의 경로 상수를 사용합니다. 기본 위치는 다음과 같습니다.

```text
src/datas/
├── 건물_동구 초량동 1149-3 제4호.pdf
├── 1. 감정평가서_문화충전소_동구 초량동 1149-3.pdf
└── 부산 동구 초량동 1149-3(목록포함).pdf
```

다른 파일을 사용할 경우 `src/main.py`의 `REGISTRY_PDF_PATH`, `APPRAISAL_PDF_PATH`, `MORTGAGE_LIST_PDF_PATH` 상수를 수정합니다.

## 4. 실행 방법

### 4.1 CLI 배치 실행

```bash
cd src
python main.py
```

성공 시 감정평가서 파일명 기반 폴더 아래에 최종 Excel과 단계별 산출물이 생성됩니다.

```text
src/{감정평가서 파일명}/
├── 담보물배분표.xlsx
└── pipeline_artifacts/
```

### 4.2 웹서비스 실행

프로젝트 루트에서 실행합니다.

```bash
uvicorn web_service.server:app --host 127.0.0.1 --port 8000
```

접속 URL:

- 사용자 화면: <http://127.0.0.1:8000/>
- 개발자 화면: <http://127.0.0.1:8000/developer>
- 헬스체크: <http://127.0.0.1:8000/api/health>

웹서비스는 업로드된 PDF를 `web_service/runtime/jobs/` 아래에 저장하고, 작업별 결과와 RAG 인덱스를 로컬에 생성합니다. 이 런타임 폴더는 Git 추적 대상이 아닙니다.

## 5. 데이터 플로우

```text
입력 PDF 3종
  ├─ 등기부등본 PDF
  ├─ 감정평가서 PDF
  └─ 저당목록 PDF
       ↓
PDF 형식 검사(pdf_validater)
       ↓
문서별 LLM 파싱
  ├─ registry: 표제부/을구 JSON
  ├─ appraisal: 토지·건물/기계기구 감정평가 JSON
  └─ mortgage_list: 목록번호별 기계기구 JSON
       ↓
pipeline.py
  ├─ run_manifest 생성
  ├─ 등기부 담보설정 병합
  ├─ 부동산 감정평가 대조
  ├─ 기계기구 기준 그룹 구성
  ├─ 기계기구-저당목록 대조
  └─ workbook_plan 검증
       ↓
excel.py
       ↓
최종 담보물배분표.xlsx
       ↓
rag_chat / web_service
       ↓
결과 설명 리포트 및 질의응답
```

## 6. 주요 API

웹서비스 주요 엔드포인트:

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/api/health` | 서버 상태와 런타임 경로 확인 |
| `POST` | `/api/jobs` | 등기부등본·감정평가서·저당목록 PDF 업로드 및 작업 생성 |
| `GET` | `/api/jobs/{job_id}` | 작업 진행상태 조회 |
| `GET` | `/api/jobs/{job_id}/download` | 최종 Excel 다운로드 |
| `POST` | `/api/jobs/{job_id}/rag/build` | 완료 작업의 RAG 인덱스 생성 |
| `POST` | `/api/jobs/{job_id}/chat` | 결과 기반 질의응답 |
| `GET` | `/api/dashboard` | 사용자 대시보드 데이터 |
| `GET` | `/api/developer/dashboard` | 개발자 대시보드 데이터 |

## 7. 검증 방법

문법 검증:

```bash
python -m py_compile \
  rag_chat/manager.py \
  web_service/server.py \
  src/main.py \
  src/pipeline.py \
  src/excel.py \
  src/validate_output.py
```

최종 Excel 검증 스크립트는 GT 파일이 로컬에 있을 때 `src` 폴더에서 실행합니다:

```bash
cd src
python validate_output.py {감정평가서 파일명}/담보물배분표.xlsx --gt GT/담보물배분표GT.xlsx
```

전체 실행은 OpenAI API 키와 실제 입력 PDF가 필요합니다.

## 8. GitHub 업로드 정책

이 저장소는 소스코드와 문서만 업로드합니다. 다음 항목은 업로드하지 않습니다.

- 비밀값: `.env`, API 키, 로컬 설정
- 고객/예시 원본자료: PDF, Excel, CSV 등
- LLM 파싱 결과: JSON 중간산출물
- 실행 결과: `output/`, `output_sample/`, `pipeline_artifacts/`, `web_service/runtime/`
- 로컬 에이전트 상태: `.omx/`, `Agent.md`

업로드 전 확인 명령:

```bash
git status --ignored --short
git ls-files | grep -E '\.(env|pdf|json|xlsx|xls|csv)$' || true
```
