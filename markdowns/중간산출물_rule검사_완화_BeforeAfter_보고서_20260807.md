# 중간산출물 Rule 검사 완화 Before / After 보고서

작성일: 2026-08-07  
대상 작업: `pipeline.py` 중심의 중간산출물 검사 하드차단 완화  
서버 영향: **서버 재시작, 프로세스 종료, DB 수정, runtime job 삭제 없음**  
보고서 작성 방식: 현재 코드 파일을 읽고, 직전 수정 전 확인된 동작/코드 패턴과 수정 후 상태를 비교

---

## 1. 작업 배경

기존 시스템은 중간산출물의 `status`, `review_required`, `blocking_errors`, 매칭 검증 결과를 매우 엄격하게 검사했다.

그 결과, 실제 업무 요구사항인:

> 주어진 서류에 대해서는 어쨌든 최종 결과 Excel을 만들어내야 한다.

와 충돌했다.

특히 다른 문서나 범용 테스트셋에서 다음 상황이 발생하면 최종 Excel 생성이 멈출 수 있었다.

- 부동산/기계기구 매칭이 일부 `review_required`로 남음
- 등기부의 저당목록 번호와 파싱된 저당목록 JSON 번호가 정확히 맞지 않음
- 기계기구 매칭 LLM 호출 중 일부 목록에서 실패
- Excel format LLM 호출 실패
- workbook validation에서 `review_required` 존재 자체를 invalid로 판정

이번 수정의 목적은 **검증을 제거하는 것이 아니라, 검증 결과를 경고/감사 기록으로 남기고 Excel 생성은 계속 진행**하게 만드는 것이다.

---

## 2. 수정 범위 요약

실제 수정 파일은 2개다.

| 파일 | 수정 목적 | 서버 실행 영향 |
|---|---|---|
| `담보물배분표_ver2/pipeline.py` | 중간산출물 검증, 기계기구 매칭, format plan 생성 실패를 soft-fail 처리 | 없음. 파일 수정만 수행 |
| `담보물배분표_ver2/excel.py` | Excel writer 쪽 legacy hard validation 완화 | 없음. 서버 재시작/실행 없음 |

수정하지 않은 영역:

- `web_service/server.py`
- `담보물배분표_ver2/main.py`
- DB (`web_service/runtime/app.db`)
- 기존 runtime job 산출물
- 웹 UI
- PDF 파서 스키마
- LLM 프롬프트의 핵심 매칭 기준
- 비용 추적 로직
- 서버 프로세스

---

## 3. 핵심 정책 변경

### Before

중간산출물에 의심 지점이 있으면 최종 산출을 막는 구조였다.

예시:

```text
review_required 있음 → workbook_plan invalid → Excel 확정 불가
blocking_errors 있음 → Excel 확정 불가
목록번호 KeyError → pipeline 중단
LLM 매칭 일부 실패 → 전체 stage 중단
format LLM 실패 → Excel 작성 전 중단
```

### After

의심 지점은 계속 기록하되, Excel 생성은 계속한다.

예시:

```text
review_required 있음 → workbook_plan에는 남김 → Excel 생성 계속
blocking_errors 있음 → warning으로 남김 → Excel 생성 계속
목록번호 누락 → fallback X 처리 + review_required 기록 → 계속
LLM 매칭 일부 실패 → 해당 목록 fallback X 처리 → 계속
format LLM 실패 → 내부 기본 서식 사용 → 계속
```

---

## 4. Before / After 상세 비교

## 4.1 workbook validation 완화

대상:

- `담보물배분표_ver2/pipeline.py`
- 현재 위치: `validate_workbook_plan()` around line 1406
- 보조 함수: `_soft_validation_result()` around line 292

### Before

`validate_workbook_plan()`은 아래 항목을 error로 취급했다.

| 조건 | 기존 처리 |
|---|---|
| `plan.status != "ok"` | error 추가 |
| `review_required`가 비어 있지 않음 | error 추가 |
| `blocking_errors`가 비어 있지 않음 | error 추가 |
| 합계 검산 불일치 | error 추가 |
| 기계기구 목록번호 membership 집합 불일치 | error 추가 |
| 단가 × 수량 != 금액 | error 추가 |

최종 반환은 다음 구조였다.

```json
{
  "status": "invalid",
  "errors": ["..."]
}
```

즉, 검증상 불일치가 있으면 산출물 확정 흐름에서 막힐 수 있었다.

### After

검증 불일치는 `warnings`로 남긴다.

반환 구조는 다음처럼 바뀌었다.

```json
{
  "run_id": "...",
  "status": "ok",
  "errors": [],
  "warnings": ["검증 경고 내용"],
  "blocking": false
}
```

의미:

- `review_required`는 더 이상 Excel 생성을 막는 조건이 아니다.
- `blocking_errors`도 기존 산출물 구조에는 남을 수 있지만, validation artifact에서는 경고화된다.
- 합계/단가 검산 불일치도 사람이 확인할 warning으로 남는다.
- 시스템은 가능한 한 Excel 파일을 생성한다.

---

## 4.2 Excel writer hard validation 완화

대상:

- `담보물배분표_ver2/excel.py`
- 현재 위치: `_validate_workbook_plan()` around line 727

### Before

Excel writer 쪽 legacy validator는 다음 조건에서 직접 예외를 던졌다.

```text
status != ok → ValueError
review_required 있음 → ValueError
blocking_errors 있음 → ValueError
부동산 가치평가액 합계 불일치 → ValueError
기계기구 수량 합계 불일치 → ValueError
기계기구 금액 합계 불일치 → ValueError
```

즉, workbook_plan이 만들어졌더라도 이 검사를 타면 Excel 저장이 중단될 수 있었다.

### After

`_validate_workbook_plan()`은 더 이상 위 조건으로 예외를 던지지 않는다.

변경 후 의도:

```text
검증 함수는 legacy audit hook으로만 유지
Excel 파일 생성은 차단하지 않음
검증 상세는 pipeline artifact의 review_required / warnings에서 확인
```

참고:

현재 `write_collateral_workbook()` 내부에서는 이 validator 호출이 직접 보이지 않지만, 함수 자체가 hard-fail 상태로 남아 있으면 향후 호출 재도입 시 같은 문제가 재발할 수 있어 함께 완화했다.

---

## 4.3 저당목록 번호 누락 시 fallback 처리

대상:

- `담보물배분표_ver2/pipeline.py`
- 신규 함수: `_resolve_mortgage_list_for_matching()` around line 937
- 적용 위치:
  - 동기 stage4 around line 1016
  - 비동기 stage4 around line 1126

### Before

기계기구 매칭 단계에서 등기부 기준 목록번호를 파싱된 저당목록 JSON에서 직접 조회했다.

개념적으로는 다음 구조였다.

```python
mortgage_list = lists_by_no[normalize_list_no(list_no)]
```

문제:

- 등기부에는 목록번호가 있는데, 저당목록 parser 결과 JSON에 해당 번호가 없으면 `KeyError` 발생
- `KeyError`가 stage 전체를 중단
- 최종 Excel 생성 불가

### After

목록번호가 없어도 fallback decision을 만든다.

처리 방식:

```text
해당 목록번호 없음
→ missing_mortgage_list_source 기록
→ 해당 목록의 기계기구 membership은 fallback 처리
→ Excel 생성 계속
```

남는 기록 예시:

```json
{
  "code": "missing_mortgage_list_source",
  "list_no": "공장 및 광업재단 저당법 제6조 목록 제2099-1호",
  "message": "등기부 기준 저당목록 번호가 파싱된 목록 JSON에 없어 해당 목록은 X/fallback으로 처리하고 Excel 생성을 계속했습니다."
}
```

---

## 4.4 기계기구 매칭 LLM 실패 시 전체 중단 방지

대상:

- `담보물배분표_ver2/pipeline.py`
- 신규 함수:
  - `_run_one_machinery_match_or_fallback()` around line 969
  - `_async_run_one_machinery_match_or_fallback()` around line 981
- 적용 위치:
  - 동기 stage4 around line 1027
  - 비동기 stage4 around line 1137

### Before

동기 경로:

```text
future.result()에서 예외 발생 → stage4 전체 중단
```

비동기 경로:

```text
asyncio.gather() 안의 task 하나 실패 → gather 전체 실패 → stage4 중단
```

문제:

- 목록 6개 중 1개만 LLM 호출 실패해도 전체 Excel 생성이 막힐 수 있었다.

### After

각 목록 단위로 예외를 잡고 fallback decision을 만든다.

처리 방식:

```text
특정 목록 LLM 매칭 실패
→ machinery_match_fallback 기록
→ 해당 목록은 보수적으로 unmatched/X 처리
→ 다른 목록과 전체 Excel 생성은 계속
```

남는 기록 예시:

```json
{
  "code": "machinery_match_fallback",
  "list_no": "...",
  "error": "예외 타입과 메시지"
}
```

---

## 4.5 저당목록 번호가 0개일 때 ThreadPoolExecutor 오류 방지

대상:

- `담보물배분표_ver2/pipeline.py`
- 적용 위치: stage4 동기 경로 around line 1010 이후

### Before

목록번호 개수를 그대로 `max_workers` 계산에 사용했다.

```text
max_workers = min(6, len(mortgage_list_numbers))
```

문제:

- `mortgage_list_numbers`가 빈 리스트면 `max_workers=0`
- `ThreadPoolExecutor(max_workers=0)`는 오류 가능
- 문서 구조에 따라 Excel 생성 전 중단 가능

### After

목록번호가 있을 때만 ThreadPoolExecutor를 생성한다.

```text
목록번호 있음 → 병렬 매칭 실행
목록번호 없음 → 빈 comparison 구조로 계속 진행
```

---

## 4.6 Excel format LLM 실패 시 기본 서식 fallback

대상:

- `담보물배분표_ver2/pipeline.py`
- 신규 함수: `default_excel_format_plan()` around line 1334
- 적용 위치: `build_excel_format_plan()` around line 1365

### Before

Excel format LLM 호출이 실패하면 그대로 예외가 전파될 수 있었다.

```text
run_excel_format_llm() 실패 → build_excel_format_plan() 실패 → Excel 작성 전 중단
```

### After

format LLM이 실패하면 내부 기본 서식을 사용한다.

기본값:

| 항목 | fallback 값 |
|---|---|
| sheet_name | 내부 계약의 기본 시트명 또는 `Sheet1` |
| date_number_format | 내부 계약 값 또는 `mm-dd-yy` |
| amount_number_format | 내부 계약 값 또는 `#,##0` |
| column_widths | 내부 계약의 column widths |
| notes | fallback 이유 기록 |

의미:

- 서식 LLM 실패는 더 이상 Excel 생성 차단 사유가 아니다.
- 업무 데이터 값, O/X, 금액, 행 개수는 format plan에서 만들지 않으므로 기본 서식 fallback이 안전한 편이다.

---

## 4.7 Excel format validation도 soft-fail 처리

대상:

- `담보물배분표_ver2/pipeline.py`
- 현재 위치: `validate_excel_format_plan()` around line 1380

### Before

format plan의 열 너비, 행 높이, number format 등이 기준을 벗어나면:

```json
{
  "status": "invalid",
  "errors": ["..."]
}
```

형태가 될 수 있었다.

### After

format validation도 경고만 남기고 `blocking=false`로 반환한다.

```json
{
  "status": "ok",
  "errors": [],
  "warnings": ["서식 관련 경고"],
  "blocking": false
}
```

---

## 5. 최종 동작 변화 요약

| 상황 | Before | After |
|---|---|---|
| `review_required` 존재 | Excel 생성 중단 가능 | Excel 생성 계속, review 기록 유지 |
| `blocking_errors` 존재 | Excel 생성 중단 가능 | warning 성격으로 남기고 계속 |
| workbook status가 `review_required` | invalid 처리 가능 | soft validation 결과 `ok`, warning 기록 |
| 저당목록 번호 누락 | `KeyError`로 중단 | fallback 처리 후 계속 |
| 기계기구 매칭 LLM 일부 실패 | stage 전체 중단 | 해당 목록만 fallback 처리 |
| format LLM 실패 | Excel 작성 전 중단 가능 | 내부 기본 서식으로 계속 |
| format validation 불일치 | invalid 가능 | warning으로 기록 |

---

## 6. 검증 결과

검증은 서버를 건드리지 않고, `conda activate langchain` 환경에서 별도 Python 프로세스로 수행했다.

확인된 환경:

```text
Python 3.10.20
/Users/yujin/anaconda3/envs/langchain/bin/python
pydantic 2.13.4
langchain_core import OK
```

수행한 검증:

| 검증 | 결과 |
|---|---|
| `pipeline.py`, `excel.py`, `main.py`, `web_service/server.py` 문법 검사 | 통과 |
| `pipeline`, `excel` import | 통과 |
| `review_required` / `blocking_errors`가 있는 workbook validation | `status=ok`, `blocking=false`, warnings 기록 확인 |
| 누락된 저당목록 번호 fallback smoke test | `missing_mortgage_list_source` 기록 후 table plan 생성 확인 |
| `review_required`가 있는 workbook_plan으로 `.xlsx` 저장 | 성공 |

Excel 저장 smoke 결과:

```text
review_required_should_still_write.xlsx 생성 성공
파일 크기: 6982 bytes
```

---

## 7. 산출물에서 사람이 확인해야 하는 위치

이제 시스템은 멈추기보다 산출을 우선한다. 따라서 불확실성은 아래 파일들에서 확인해야 한다.

| 산출물 | 확인 내용 |
|---|---|
| `pipeline_artifacts/02_real_estate/real_estate_table_plan.json` | 부동산 매칭 관련 `review_required` |
| `pipeline_artifacts/04_machinery_match/machinery_table_plan.json` | 기계기구 매칭 fallback, unresolved, missing list |
| `pipeline_artifacts/04_machinery_match/machinery_match_verification.json` | 기계기구 매칭 검증/경고 |
| `pipeline_artifacts/05_excel/workbook_plan.json` | 최종 workbook plan의 `review_required`, `blocking_errors` |
| `pipeline_artifacts/06_excel_format/excel_format_validation.json` | 서식 관련 warnings |

---

## 8. 위험 및 주의사항

이번 수정 후 시스템 철학이 바뀐다.

### 기존 철학

```text
확신이 없으면 Excel을 만들지 않는다.
```

### 변경 후 철학

```text
확신이 부족해도 Excel은 만들고, 불확실성은 review_required/warnings로 남긴다.
```

따라서 다음 점을 주의해야 한다.

1. Excel 파일이 생성됐다는 사실이 모든 매칭이 확정됐다는 뜻은 아니다.
2. `review_required`가 남은 Excel은 사람이 검토해야 한다.
3. fallback 처리된 기계기구 목록은 보수적으로 `X` 또는 공란/검토필요에 가까운 결과가 될 수 있다.
4. 운영 화면이나 리포트에서 `review_required`를 사용자에게 어떻게 보여줄지 별도 UX 정책이 필요할 수 있다.

---

## 9. 수정 범위의 의도적 제한

이번 작업은 사용자가 요청한 대로 **중간 rule 검사 때문에 문서 작성이 멈추는 부분만 완화**했다.

의도적으로 하지 않은 것:

- 매칭 정확도 개선
- 새 LLM 프롬프트 설계
- parser 구조 변경
- DB 스키마 변경
- 서버 API 변경
- 웹 UI 변경
- 기존 job 재처리
- 서버 재시작
- 실행 중인 서버 프로세스 조작

---

## 10. 한 줄 결론

이번 수정은 `pipeline.py` 중심의 중간산출물 rule 검사를 **hard stop에서 soft warning으로 바꾸고**, `excel.py`의 legacy hard validation까지 완화하여, 범용 문서에서도 가능한 한 최종 Excel을 생성하도록 만든 작업이다.
