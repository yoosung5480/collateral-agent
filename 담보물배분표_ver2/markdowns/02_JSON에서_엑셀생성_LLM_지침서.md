# JSON 데이터에서 담보물 배분표를 만들기 위한 LLM 지침서

## 0. LLM의 역할

LLM은 셀 서식이나 좌표를 직접 계산하는 주체가 아니다. LLM의 핵심 역할은 서로 표현이 다른 문서 JSON을 의미적으로 대조하여 **확정된 Excel 입력 계획**을 만드는 것이다.

정확도 우선순위는 다음과 같다.

1. 원문 수치와 식별자를 보존한다.
2. 같은 대상을 다른 표현으로 쓴 경우 의미적으로 연결한다.
3. 하나의 평가 항목을 둘 이상의 표에 중복 합산하지 않는다.
4. 불확실한 값은 추정하지 않고 검토 대상으로 반환한다.
5. 배분액과 배분잔액은 항상 공란으로 둔다.

LLM은 원문을 교정하는 주체가 아니다. 서로 충돌하는 값이 있으면 더 그럴듯한 문자열을 선택하지 말고 충돌 사실과 후보를 반환한다.

## 0.1 실행 상태

| 상태 | 의미 | Excel 처리 |
| --- | --- | --- |
| `ok` | 모든 핵심 매칭과 합계가 확정됨 | 최종본 생성 가능 |
| `review_required` | 주소·지번·목록 항목 등 핵심 판단이 하나 이상 미확정 | 기본적으로 최종본 생성 중단 |
| `invalid` | JSON 누락·중복·스키마 오류·합계 불일치 | 생성 금지 |

---

# 1. 입력 JSON 계약

## 1.0 입력 manifest

LLM 호출에는 이번 실행에 사용할 파일 목록을 명시적으로 전달한다. 작업 폴더 전체를 재귀 탐색하여 JSON을 수집하지 않는다. 같은 `registry_id`의 재추출본·구버전이 함께 존재할 수 있기 때문이다.

```json
{
  "registry_files": [
    {"title_path": ".../title.json", "encumbrance_path": ".../encumbrance.json"}
  ],
  "real_estate_appraisal_path": ".../real_estate_appraisal.json",
  "machinery_appraisal_path": ".../machinery_appraisal.json",
  "mortgage_list_paths": [".../list_2012_399.json"]
}
```

manifest 내부에서도 파일명으로 짝을 확정하지 않는다. 각 JSON의 `registry_id`를 읽은 뒤 동일한 `registry_XX`인지 검증한다.

## 1.1 등기부 표제부 `title.json`

```json
{
  "registry_id": "../.../registry_01_p1-18/title.pdf",
  "property_type": "토지",
  "location": "경상남도 함안군 칠서면 계내리 12",
  "details": "공장용지 10934㎡"
}
```

| 필드 | 역할 |
| --- | --- |
| `registry_id` | 같은 등기부의 title·encumbrance 연결 |
| `property_type` | 토지·건물 구분 |
| `location` | 소재지 및 지번 비교 |
| `details` | 지목, 면적, 구조, 층별 면적 표시 및 감정평가 대조 보조 |

## 1.2 등기부 을구 `encumbrance.json`

```json
{
  "registry_id": "../.../registry_01_p1-18/encumbrance.pdf",
  "mortgages": [
    {
      "registration": "2012.8.20",
      "maximum_claim": "금 5,040,000,000원",
      "mortgagee": "중소기업은행",
      "shared_collateral": "공용담보",
      "mortgage_list_no": "공장 및 광업재단 저당법 제6조 목록 제2012-399호"
    }
  ]
}
```

`mortgages` 배열의 항목은 중요도가 아니라 문서상의 담보설정 항목이다. 최종 `순위`는 설정일자 오름차순에 따른 출력 순서이다.

## 1.3 토지·건물 감정평가 JSON

```json
{
  "land": [
    {
      "location_lot_number": "경상남도 함안군 칠서면 계내리 12",
      "total_appraised_value": 3610761000,
      "items": []
    }
  ],
  "building": [
    {
      "location_lot_number": "경상남도 함안군 칠서면 계내리 12, 13-3, 13-4, 14-2, 18, 22 위 지상",
      "total_appraised_value": 2058134200,
      "items": []
    }
  ]
}
```

대조와 합산의 기본 단위는 `location_lot_number` 그룹이다. `items`는 구조와 용도를 검증하기 위한 보조 근거이다.

## 1.4 기계기구 감정평가 JSON

```json
[
  {
    "name": "Over Head Crane",
    "structure": "Double Girder Type, 기타 부대설비 포함",
    "specification": "Cap: 20Ton; Span: 약 21m; Rail: 약 60m",
    "manufacturer": "경남 HC",
    "manufacture_date": "2012.08",
    "quantity": 1,
    "unit_price": null,
    "amount": 17570000
  }
]
```

대조표의 행은 이 배열 순서를 기준으로 만든다. 행 수와 수량 합계를 혼동하지 않는다.

## 1.5 저당목록 JSON의 권장 최소 형식

저당목록 파서 결과는 최소한 다음 의미를 가져야 한다. 실제 필드명이 다르면 LLM이 아래 논리 필드로 정규화한다.

```json
[
  {
    "mortgage_list_no": "공장 및 광업재단 저당법 제6조 목록 제2012-399호",
    "items": [
      {
        "source_order": 1,
        "name": "Over Head Crane",
        "structure": "Double Girder Type",
        "specification": "Cap: 20Ton; Span: 약 21m",
        "manufacturer": "경남 HC",
        "manufacture_date": "2012.08",
        "quantity": 1
      }
    ]
  }
]
```

저당목록에는 금액이 없어도 된다. 존재 여부 대조에 필요한 명칭·제작자·제작일자·구조·규격·수량·문서 내 순서가 중요하다.

현재 실제 저당목록 파서 JSON은 제공되지 않았으므로 위 구조는 **운영 계약의 권장 최소 형식**이다. 실제 산출물이 들어오면 필드 매핑을 먼저 검증해야 하며, 알 수 없는 필드를 추측해 연결하지 않는다.

---

# 2. 정규화 규칙

정규화 값은 비교에만 사용하고, Excel 표시 문자열은 가능한 한 원문을 보존한다.

| 유형 | 정규화 |
| --- | --- |
| `registry_id` | `registry_\d+` 식별자 추출 |
| 날짜 | 실제 날짜로 파싱하되 원본 순서 인덱스 보존 |
| 금액 | `금`, `원`, 쉼표, 공백 제거 후 정수 |
| 공담 | `공용담보`, `공동담보`, `공담`을 `공담`으로 통일 |
| 목록번호 | 공백 차이만 정리하고 전체 식별 문자열 보존 |
| 주소 | 시·도·군·면·리와 지번 토큰 분리, 도로명주소는 보조 근거 |
| 기계 명칭 | 대소문자·연속 공백·하이픈 차이를 비교용으로만 정규화 |
| 제작일자 | `YYYY.MM`, `YYYY-MM`, `YYYY년 MM월`을 같은 연월로 비교 |

숫자, 지번, 목록번호는 유사도만으로 바꾸지 않는다.

## 2.1 원문 보존과 provenance

모든 확정값에는 최소한 다음을 남긴다.

- `source_file`: 값을 읽은 JSON 파일
- `source_path`: JSONPath와 같은 필드 위치
- `source_value`: 원문 값
- `normalized_value`: 비교에 사용한 값
- `decision`: `matched`, `not_matched`, `unresolved`
- `reason`: 판단에 사용한 일치·불일치 근거

정규화 값은 원문을 대체하지 않는다.

---

# 3. 등기부 연결과 부동산 표 그룹화

## 3.1 title·encumbrance 연결

1. 각 파일의 `registry_id`에서 `registry_XX`를 추출한다.
2. 같은 식별자의 title과 encumbrance를 한 레코드로 연결한다.
3. 각 manifest 쌍의 title과 encumbrance에서 추출한 식별자가 서로 같은지 확인한다.
4. 한쪽이 누락되거나 같은 종류가 중복되면 자동 선택하지 않고 `invalid`로 반환한다.
5. `registry_id`를 추출할 수 없을 때 파일명으로 대체하지 않는다.

## 3.2 담보설정현황 동일성

각 mortgage를 다음 정규화 튜플로 만든다.

```text
(registration, maximum_claim, mortgagee, shared_collateral, mortgage_list_no)
```

두 등기부의 전체 튜플 배열이 항목 수와 순서까지 같으면 동일 담보설정현황 그룹으로 본다. 비교 전에 날짜·금액·공담 표기만 정규화한다.

완전한 GT 회귀 입력의 기대 결과는 다음과 같다.

- 그룹 1: `registry_01`, `registry_03`, `registry_04`
- 그룹 2: `registry_02`

단, 이번에 새로 제공된 6개 파일만 입력하면 `registry_04`가 없으므로 실제 결과는 `{registry_01, registry_03}`과 `{registry_02}`이다. 완전한 GT 회귀 입력에 `registry_04`가 있을 때만 첫 그룹에 추가한다.

## 3.3 순위 부여

그룹 내부 mortgage를 `registration` 오름차순으로 안정 정렬한다.

- 순위는 `1, 2, 3, ...`의 출력 순서이다.
- 순위는 중요도, 담보가치, 법률적 우선순위를 새로 평가한 점수가 아니다.
- 같은 날짜면 원본 `mortgages` 배열 순서를 유지한다.

---

# 4. 부동산 가치평가액 대조

## 4.1 대조 목표

각 담보설정현황 그룹의 title 레코드들과 감정평가서의 `land`·`building` 그룹을 연결한 뒤, 연결된 `total_appraised_value`만 합산한다.

이 작업은 단순 문자열 완전일치로 처리하면 안 된다. 예를 들어 등기부에는 `계내리 12 외 5필지 제에이동호`로 쓰이고 감정평가서에는 `12, 13-3, 13-4, 14-2, 18, 22 위 지상`으로 풀어 쓸 수 있다.

## 4.2 판단 순서

1. `property_type`과 감정평가 최상위 분류 `land`·`building`을 맞춘다.
2. 소재지 행정구역을 비교한다.
3. 지번 집합을 비교한다. `외 N필지`, 쉼표 나열, `위 지상` 표현 차이를 해석한다.
4. 건물은 동호명, 용도, 구조, 공부면적을 보조 근거로 사용한다.
5. 후보가 하나로 확정될 때만 연결한다.
6. 하나의 감정평가 그룹은 하나의 부동산 출력 그룹에 한 번만 합산한다.
7. 지번이 여러 그룹과 겹치면 부동산 유형과 구조·용도를 함께 보고 판단한다.
8. 확정할 수 없으면 `review_required`에 근거와 후보를 기록하고 금액을 임의 합산하지 않는다.

## 4.3 하드 충돌과 확정 조건

다음은 단순 표기 차이가 아니라 하드 충돌이다.

- 행정구역 토큰이 명시적으로 다름: 예) `함안군`과 `합천군`, `칠서면`과 `철서면`
- 지번 숫자가 다르며 `외 N필지` 또는 다필지 전개로 설명되지 않음
- `property_type`이 토지와 건물로 다름
- 후보 둘 이상이 같은 수준의 지번·구조 근거를 가짐
- 감정평가 그룹을 이미 다른 출력 그룹에 배정함

하드 충돌이 있으면 문자열 편집거리나 문맥상 그럴듯함으로 교정하지 않는다. `appraised_value`를 확정하지 않고 `review_required`로 반환한다.

현재 `title(7).json`의 `경상남도 합천군 철서면 계내리 21-1`과 감정평가 JSON의 `경상남도 함안군 칠서면 계내리 21-1`은 이 규칙에 따른 하드 충돌이다. 기존 GT의 145,500,000원은 회귀 기준값이지만, 이 충돌한 새 title을 그대로 사용하는 실행에서 자동 기입할 수 있는 값은 아니다.

확정 매칭은 다음 조건을 만족해야 한다.

1. 유형이 일치한다.
2. 행정구역과 지번 집합이 일치하거나, 다필지 축약·전개 관계가 근거로 설명된다.
3. 건물은 동호명·구조·용도·면적 중 둘 이상의 보조 근거가 모순되지 않는다.
4. 후보가 하나만 남는다.
5. 동일 감정평가 그룹의 중복 배정이 없다.

## 4.4 완전한 GT 회귀 데이터의 기대 대조

| 출력 그룹 | 감정평가 구성 | 합계 |
| --- | --- | ---: |
| `registry_01·03·04` | 계내리 12 토지 + 주건물 + 제시외건물 | 5,681,855,200 |
| `registry_02` | 계내리 21-1 토지 | 145,500,000 |
| 전체 | 위 두 그룹 | 5,827,355,200 |

`12, 21-1 위 지상` 제시외건물은 토지가 아니라 건물 항목이며 현재 GT에서는 주 공장 건물 그룹에 포함된다. 이 사례를 문자열 규칙으로 하드코딩하지 말고, 유형·용도·구조와 GT 관계를 근거로 판단한다.

---

# 5. 기계기구 담보물 배분표 데이터 생성

## 5.1 대상 담보설정 그룹

다음 근거를 종합해 기계기구 담보설정 그룹을 선택한다.

1. 비어 있지 않은 `mortgage_list_no` 배열이 저당목록 JSON의 목록번호 집합과 일치하는가
2. 목록번호가 순위별로 연속적으로 연결되는가
3. 담보 소재지가 감정평가 대상 공장 소재지와 일치하는가

완전한 GT 회귀 입력에서는 목록번호 6개를 모두 가진 `registry_01·03·04` 그룹이 대상이다. 새 6개 JSON만 입력하면 동일한 목록번호 배열을 가진 `registry_01·03` 그룹이 후보다. 목록번호 개수만으로 후보가 둘 이상이면 자동 선택하지 않는다.

## 5.2 수량과 가치평가액

```text
machine_quantity_total = sum(quantity)
machine_appraised_value = sum(amount)
```

현재 데이터의 검산값은 수량 `10`, 금액 `179,890,000`이다. 기계기구 배열 행 수는 9이므로 `9식`으로 쓰면 안 된다.

---

# 6. 기계기구와 저당목록 대조

## 6.1 행 기준

감정평가서 기계기구 배열이 기준 행이다. 감정평가 항목을 저당목록 순서로 재정렬하지 않는다.

## 6.2 비교 필드 우선순위

1. 문서에 공통 고유번호가 있으면 고유번호
2. `name`
3. `manufacturer`
4. `manufacture_date`
5. `structure`
6. `specification`
7. `quantity`
8. 동일 항목의 문서 내 n번째 등장 순서

`Over Head Crane`처럼 같은 명칭이 반복되므로 명칭만 같다고 모든 행에 `O`를 주면 안 된다. 용량, Span, 공용 Rail 표기, 제작연월, 수량과 반복 순서를 함께 확인해야 한다.

## 6.3 대조 결과

| 판단 | 값 |
| --- | --- |
| 동일 기계기구가 해당 목록에서 확인됨 | `O` |
| 해당 목록 전체를 확인했으나 동일 항목이 없음 | `X` |
| OCR 누락·중복 후보 등으로 확정 불가 | `null` + `review_required` |

`null`은 Excel 작성 시 공란이 된다. 불확실한 상태를 `X`로 바꾸면 안 된다.

## 6.4 단가 처리

- `unit_price`가 있으면 원문 값을 사용한다.
- `unit_price`가 `null`이면 Excel 단가도 공란이다.
- `amount / quantity`로 단가를 역산하지 않는다. 계산 가능한 값과 원문에 명시된 값은 다른 provenance를 갖기 때문이다.
- 숫자 `unit_price`가 있으면 `unit_price * quantity == amount`를 검증한다. 불일치하면 자동 수정하지 않고 `invalid`로 반환한다.

---

# 7. LLM 출력 형식

LLM은 Excel 좌표가 아니라 다음과 같은 의미 계획 JSON을 반환한다.

```json
{
  "status": "ok",
  "borrower_name": "휴먼중공업 주식회사",
  "real_estate_tables": [
    {
      "group_id": "group_01",
      "registry_ids": ["registry_01", "registry_03", "registry_04"],
      "mortgagee": "중소기업은행",
      "properties": [],
      "mortgages": [
        {
          "rank": 1,
          "registration": "2012.08.20",
          "maximum_claim": 5040000000,
          "mortgagee": "중소기업은행",
          "shared_collateral": "공담",
          "mortgage_list_no": "공장 및 광업재단 저당법 제6조 목록 제2012-399호",
          "allocation_amount": null,
          "allocation_balance": null
        }
      ],
      "appraisal_matches": [
        {
          "appraisal_group_id": "land:경상남도 함안군 칠서면 계내리 12",
          "appraised_value": 3610761000,
          "decision": "matched",
          "reason": "property_type, 행정구역, 지번 일치",
          "evidence": []
        }
      ],
      "total_appraised_value": 5681855200
    }
  ],
  "machinery_table": {
    "source_group_id": "group_01",
    "locations": [],
    "quantity_total": 10,
    "total_appraised_value": 179890000,
    "mortgages": []
  },
  "machinery_comparison": {
    "mortgage_list_numbers": [],
    "rows": [
      {
        "appraisal_row_index": 1,
        "name": "Over Head Crane",
        "manufacturer": "경남 HC",
        "manufacture_date": "2012.08",
        "quantity": 1,
        "unit_price": null,
        "amount": 17570000,
        "membership": {
          "공장 및 광업재단 저당법 제6조 목록 제2012-399호": "O"
        },
        "match_evidence": []
      }
    ],
    "quantity_total": 10,
    "amount_total": 179890000
  },
  "review_required": [],
  "blocking_errors": []
}
```

각 `appraisal_matches`에는 최소한 감정평가 그룹 식별값, 연결된 registry 그룹, 금액, 판단 근거를 남긴다. 각 기계기구 행에는 목록번호별 `O`·`X`·`null`을 저장한다.

`allocation_amount`와 `allocation_balance`는 항상 `null`이어야 한다. `maximum_claim`은 배분내역의 설정금액일 뿐 배분액이 아니다.

`review_required`의 각 항목은 `code`, `affected_output`, `source_values`, `candidates`, `reason`, `required_action`을 갖는다. 핵심 금액이나 O/X 판정에 영향을 주는 항목이 하나라도 있으면 `status`는 `review_required`여야 한다.

---

# 8. 필수 검증

LLM은 출력 전에 다음을 검사한다.

- title과 encumbrance가 등기부별로 정확히 1:1 연결됨
- 부동산 그룹별 mortgages가 완전히 같은 경우에만 병합됨
- 모든 순위가 날짜순이며 같은 날짜는 원본 순서 유지
- 감정평가 그룹이 둘 이상의 부동산 표에 중복 합산되지 않음
- 부동산 표 가치평가액 합계가 배정된 감정평가액 합계와 일치
- 기계기구 수량 합계와 금액 합계가 원본 JSON 합계와 일치
- 목록번호 열 수가 고유 목록번호 수와 일치
- `O`는 명칭 단독 유사도가 아니라 항목 단위 근거가 있을 때만 생성
- 배분액과 배분잔액은 모두 `null`
- `unit_price: null`이 역산값으로 바뀌지 않음
- 하드 충돌이 있는 부동산은 가치평가액이 확정되지 않음

완전한 GT 회귀 입력에서는 최소 다음이 일치해야 한다.

```text
부동산 표 수 = 2
첫 표 registry = 01, 03, 04
둘째 표 registry = 02
부동산 평가 합계 = 5,827,355,200
기계기구 행 수 = 9
기계기구 수량 합계 = 10
기계기구 금액 합계 = 179,890,000
저당목록 열 수 = 6
```

새로 제공된 6개 등기부 JSON만 검사할 때는 다음을 별도로 확인한다.

```text
확인된 registry = 01, 02, 03
동일 담보설정현황 그룹 = {01, 03}, {02}
registry_04 자동 생성 금지
registry_02 소재지와 감정평가 소재지의 행정구역 충돌 = review_required
```
