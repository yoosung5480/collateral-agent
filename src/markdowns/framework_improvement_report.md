# 프레임워크 개선 결과보고서

작성일: 2026-08-04  
대상: 담보물배분표 자동작성 프레임워크

## 1. 변경된 실행 Workflow

### Before

```text
python main.py
  → 등기부등본 LLM 분석(순차)
    → 통합 등기부 분할
    → 각 등기부 title/encumbrance 추출(순차)
  → 감정평가서 LLM 분석(순차)
    → 구간 분할
    → 토지·건물 추출
    → 기계기구 추출
  → 공장저당법목록 LLM 분석
    → 목록번호별 분할
    → 목록별 추출(일부 비동기)
  → pipeline stage 1~5
  → 코드 기반 Excel 작성
```

### After

```text
python main.py
  → 3종 원본 PDF LLM 분석 병렬 실행
    ├─ 등기부등본
    │   ├─ 통합 등기부 시작 페이지 LLM 판정
    │   ├─ 각 등기부의 표제부/을구 페이지 LLM 판정 병렬화
    │   └─ title.pdf / encumbrance.pdf LLM 추출 병렬화
    ├─ 감정평가서
    │   ├─ 명세표 구간 LLM 판정
    │   └─ 토지·건물 / 기계기구 명세표 LLM 추출 병렬화
    └─ 공장저당법목록
        ├─ 목록번호별 분할 LLM 판정
        └─ 목록별 기계기구 추출 LLM 병렬화
  → run_manifest.json 생성
  → pipeline stage 1~5
    ├─ 01_registry_merge
    ├─ 02_real_estate
    ├─ 03_machinery_base
    ├─ 04_machinery_match(목록별 LLM 매칭 비동기)
    └─ 05_excel/workbook_plan.json
  → 06_excel_format
    ├─ GT/담보물배분표GT.xlsx 서식 요약
    ├─ gpt-5.4 Excel 서식 개선 노드 실행
    └─ excel_format_plan.json / validation 생성
  → 검증된 workbook_plan + excel_format_plan 기반 최종 Excel 작성
```

최종 산출물:

- `output/registry/**/title.json`, `output/registry/**/encumbrance.json`
- `output/appraisal/1. 감정평가서_문화충전소_동구 초량동 1149-3.json`
- `output/mortgage_list/부산 동구 초량동 1149-3(목록포함).json`
- `1. 감정평가서_문화충전소_동구 초량동 1149-3/pipeline_artifacts/**`
- `1. 감정평가서_문화충전소_동구 초량동 1149-3/담보물배분표.xlsx`

## 2. 수정 또는 API Before-After

| 항목 | Before | After |
| --- | --- | --- |
| 실행 진입점 | `python main.py` | 유지 |
| 프로젝트 루트 | 특정 절대경로 고정 | `main.py` 위치 기준으로 고정해 백업 디렉터리에서도 실행 가능 |
| LLM 구성 | `gpt-5.4-mini`, `gpt-5.6-sol`, tool LLM | 기존 구성 유지 + 서식 전용 `gpt-5.4` 추가 |
| 등기부 워크플로우 | 분할/추출 대부분 순차 | 등기부별 section 판정 및 title/encumbrance 추출 병렬화 |
| 감정평가서 워크플로우 | 토지·건물 추출 후 기계기구 추출 | 두 명세표 추출을 `asyncio.gather`로 병렬화 |
| 저당목록 워크플로우 | 목록별 추출 비동기 존재 | 전체 실행 경로에서 비동기 호출 사용 + summary/source 메타데이터 기록 |
| 기계기구 매칭 | `ThreadPoolExecutor` 기반 동시 호출 | `async_stage4_machinery_match()` 추가, main에서 실제 async API 호출 |
| Excel 작성 API | `write_collateral_workbook(plan, output)` | 기존 호출 호환 유지 + `format_plan=` 선택 인자 추가 |
| Excel 서식 개선 | 코드 고정 최소 프로토타입 | GT workbook 요약 → gpt-5.4 서식 plan → 안전 적용 |

## 3. 수정된 파일 설명

### `main.py`

- `main_async()` 기반으로 전환했다.
- 등기부등본/감정평가서/공장저당법목록 분석을 `asyncio.gather()`로 병렬 실행한다.
- `PROJECT_ROOT`를 `Path(__file__).resolve().parent`로 바꿔 현재 백업 디렉터리 실행을 지원한다.
- `FORMAT_MODEL_NAME = "gpt-5.4"`를 추가하고, `build_excel_format_plan()`을 최종 Excel 작성 직전에 실행한다.

### `registry/pdf_llm_parser.py`

- `async_find_registry_start_pages()`와 `async_find_section_pages()`를 추가했다.
- `async_split_registry_sections()`를 추가해 등기부별 표제부/을구 페이지 판정을 병렬화했다.
- 기존 동기 API는 유지했다.

### `appraisal/parser.py`

- 토지·건물 추출과 기계기구 추출의 async 메서드를 추가했다.
- `AppraisalContentExtractor.async_extract()`에서 두 LLM 추출을 병렬 실행한다.
- `AppraisalParser.async_invoke()`를 추가하고 기존 `invoke()`는 유지했다.

### `mortgage_list/parser.py`

- 목록별 `source_pdf`, `source_pages`, `extraction_summary`를 JSON에 남기도록 확장했다.
- 전체 `summary`, `warnings`를 추가해 중간산출물만 보고도 추출 상태와 warning 개수를 확인할 수 있게 했다.
- OpenAI structured output 스키마 제약을 만족하도록 `MortgageListExtractionSummary` Pydantic 모델을 사용했다.

### `pipeline.py`

- `async_run_one_machinery_match()`와 `async_stage4_machinery_match()`를 추가했다.
- `workbook_plan.json`에 `source_citations`를 추가해 등기부 JSON, 감정평가 JSON, 저당목록 분할 PDF/page 정보를 연결했다.
- `summarize_gt_workbook()`, `run_excel_format_llm()`, `build_excel_format_plan()`, `validate_excel_format_plan()`을 추가했다.
- Excel 서식 LLM 노드는 `gpt-5.4`를 사용하며, 금액·날짜·수량 등 업무 데이터는 반환/변경하지 않고 표시 형식만 반환하도록 제한했다.

### `excel.py`

- `write_collateral_workbook(..., format_plan=None)` 선택 인자를 추가했다.
- `format_plan` 적용 범위는 시트명, 허용된 헤더 문구, 열 너비, 행 높이, 날짜/금액 number format, 정렬로 제한했다.
- 날짜 셀은 GT 기준 `mm-dd-yy` 같은 표시 형식 적용이 가능해졌다.
- 숫자/내용 변환 없이 openpyxl 표시 형식만 변경한다.

## 4. 개선사항 및 결과

### 속도 성능

- 전체 PDF 분석의 독립 구간을 병렬화했다.
- 통합 검증 실행 결과: `python main.py` 전체 완료, 실행 시간 `122.787초`.
- 기존 순차 호출 대비 LLM 대기 시간이 겹치도록 개선됐다.

### 중간산출물 개선

- 저당목록 JSON에 다음 필드가 추가됐다.
  - `summary.status`
  - `summary.mortgage_list_count`
  - `summary.machinery_item_count`
  - `summary.item_count_by_list`
  - `summary.warning_count_by_list`
  - 각 `mortgage_lists[].source_pdf`
  - 각 `mortgage_lists[].source_pages`
  - 각 `mortgage_lists[].extraction_summary`
- `workbook_plan.json`에 `source_citations`가 추가되어 최종 Excel plan에서 원천 JSON/PDF 경로를 추적할 수 있다.

### Excel 반환 형식

- `GT/담보물배분표GT.xlsx`를 직접 읽어 서식 요약 JSON을 만든다.
- `gpt-5.4` LLM 노드가 별도 `excel_format_plan.json`을 생성한다.
- Excel writer는 이 plan을 적용하되 업무 데이터 값은 변경하지 않는다.
- 스모크 확인 결과 최종 workbook의 `D6` 설정일자 표시 형식은 `mm-dd-yy`, D열 너비는 `15.5703125`, 정렬은 `center`로 적용됐다.

## 5. 검증 결과

| 검증 | 결과 |
| --- | --- |
| `python -m py_compile main.py pipeline.py excel.py appraisal/parser.py mortgage_list/parser.py registry/pdf_llm_parser.py registry/parse_registry_info.py` | 통과 |
| `python main.py` | 통과 |
| `pipeline_artifacts/05_excel/workbook_validation.json` | `status: ok`, `errors: []` |
| `pipeline_artifacts/06_excel_format/excel_format_validation.json` | `status: ok`, `errors: []` |
| 최종 Excel 파일 존재 | 통과 |
| 최종 Excel 서식 스모크 | `D6.number_format=mm-dd-yy`, `D.width=15.5703125`, `D6.align=center` |

참고: `validate_output.py`의 GT 비교는 현재 입력 샘플이 문화충전소/초량동이고 GT 파일은 휴먼중공업 예시라 업무 데이터 값이 달라 실패한다. 이는 현재 개선 작업의 실패가 아니라 샘플 불일치에 따른 비교 기준 불일치다.
