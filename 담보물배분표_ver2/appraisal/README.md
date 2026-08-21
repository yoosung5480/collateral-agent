# 해야할일
우리는 문서를 두단계에 거쳐서 데이터를 추출해내야한다.
첫번째로 "gpt-5.4-mini" 모델을 사용해서 문서 목차를 통해서 문서를 가볍게 OCR하고 문서 구조 정보를 기반으로 pdf문서를 슬라이싱해서 저장한다. 그리고 "gpt-5.6-sol" 모델을 사용해서 `토지·건물 감정평가명세표 파싱결과`, `기계기구 감정평가명세표`의 형태에 맞게 파싱해와야한다.

# 금지사항
- 쓸데없는 모든 try-except등의 fallback 금지. 방어적 코딩금지
- llm 호출없는 기능 fallback 절대 금지
- llm 호출없이 실행성공은 테스트 실패.
- LangChain, Pydantic 기반 사용.
- 경로에 쓸데없이 실행기준 root 감지 금지. 
- 내가 하드코딩한 output_root, input_path 기반으로 경로 지정돼야함 
- 지정된 모델에 대해서 좃같은 env또는 환경변수 기반 지정아니고, 그냥 하드코딩 지정


# 감정평가서 문서 구조

문서는 대체로 다음 대목차가 순서대로 등장한다. 일부 부속 문서는 생략되거나 순서가 달라질 수 있다.

* 감정평가 회보
* 수수료 청구서
* 감정평가서 표지
* 심사인증서
* 공장 감정평가표
* 감정평가액 산출근거 및 결정의견
* **토지·건물 감정평가명세표**
* **기계기구 감정평가명세표**
* 토지 감정평가요항표
* 건물 감정평가요항표
* 기계기구 감정평가요항표
* 광역위치도
* 상세위치도
* 지적 및 건물개황도
* 이용 및 임대내역도
* 기계기구 배치도
* 사진용지
* 기계기구 관련 확약서
* 토지이용계획확인서
* 등기사항전부증명서
* 토지대장
* 건축물대장




# workflow
## 1. llm 문서 기반 파서
input : 감정평가서 원본 파일경로
output : output_root \ 원본파일 이름 

기능 : input 경로에서 원본 문서 읽어와서 output 경로에 각각 파일 저장 "토지·건물 감정평가명세표.pdf", "기계기구 감정평가명세표.pdf"

모델은 "gpt-5.6-sol"모델을 사용한다.

### 예시
input : "담보물배분표/datas/감정평가서 휴먼중공업.pdf"
output : "담보물배분표/output/appraisal/감정평가서 휴먼중공업/"

- 페이지 번호 감지
감정평가서 -> 토지·건물 감정평가명세표 페이지만 파싱  ex. (29, 31)
감정평가서 -> 기계기구 감정평가명세표 페이지만 파싱 ex. (31, 35)

- gpt모델은, gpt-5.4-mini모델로 지정.

실행결과
: "담보물배분표/output/appraisal/감정평가서 휴먼중공업/토지·건물 감정평가명세표.pdf"
: "담보물배분표/output/appraisal/감정평가서 휴먼중공업/기계기구 감정평가명세표.pdf"

## 2. llm 기반 문서내용 추출
input : 추출된 토지·건물 감정평가명세표, 기계기구 감정평가명세표의 경로
output : output_root \ 원본파일 이름.json

기능 : 두 감정평가 명세표를 기반으로 데이터를 각각 json으로 읽어온다

1. 토지·건물 감정평가명세표
- "소재지-지번"을 기준으로 분리


### 예시
input : "담보물배분표/output/appraisal/감정평가서 휴먼중공업/토지·건물 감정평가명세표.pdf", "담보물배분표/output/appraisal/감정평가서 휴먼중공업/기계기구 감정평가명세표.pdf"


### 그룹화 규칙
1. `location_lot_number`는 정규화한 `location`과 `lot_number`를 공백 하나로 결합한다.
2. `location_lot_number`가 같은 행만 동일 그룹으로 묶는다. 소재지가 같아도 지번이 다르면 반드시 별도 그룹으로 저장한다.
3. 건물의 동·층·부속건물 명칭이 달라도 소재지와 지번이 같으면 같은 그룹의 서로 다른 `items`로 저장한다.
4. `제시외 건물`도 `building`에 포함한다. 해당 구역에서 새로 명시된 소재지와 지번을 기준으로 그룹을 만든다.
5. `total_appraised_value`는 해당 그룹의 모든 `items[].appraised_value` 합계다. 소계·합계행의 금액을 다시 더하지 않는다.

### 필드 추출 규칙
1. `location`과 `lot_number`는 반드시 분리해 저장하고, 상위 그룹에는 결합값인 `location_lot_number`도 둔다.
2. `동소`는 같은 열의 직전 명시값을 뜻한다. 소재지 열과 지번 열을 각각 독립적으로 승계한다.
3. `land_category_or_use`에는 표의 `지목 및 용도`를 기록한다. 예: `공장용지`, `공장`, `부속건물(휴게실, 식당, 사무실, 기숙사)`, `창고`.
4. `use_zone_or_structure`에는 토지의 용도지역 또는 건물의 구조·지붕·층수를 기록한다. 여러 줄을 자연스러운 한 문자열로 복원한다.
5. `official_area_sqm`에는 `면적-공부` 열의 값을 제곱미터 숫자로 기록한다. 여러 층의 공부면적이 한 감정평가 항목으로 묶였다면 합산한 값을 기록한다.
6. `appraised_value`는 `감정평가액-금액` 열의 값만 사용한다.
7. `면적-사정`, `감정평가액-단가`, `비고`는 출력하지 않는다. 비고의 관찰가격·재조달원가·감가계수도 다른 필드에 섞지 않는다.
8. 일련번호는 출력하지 않는다.
9. 금액은 쉼표와 원화 기호를 제거한 원 단위 정수로 출력한다. 면적은 숫자로 출력한다.
10. 읽을 수 없는 문자열이나 공부면적은 추정하지 말고 `null`로 둔다. 감정평가액이 불명확하면 페이지를 확대해 다시 확인한다.
11. 출력 전 각 그룹의 `items[].appraised_value` 합계와 `total_appraised_value`가 일치하는지 검산하고, 전체 토지·건물 합계가 문서 최종 합계와 일치하는지 다시 검산한다.

#### 토지·건물 감정평가명세표 파싱결과
```json
{
  "land": [
    {
      "location_lot_number": "경상남도 함안군 칠서면 계내리 12",
      "total_appraised_value": 3610761000,
      "items": [
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "12",
          "land_category_or_use": "공장용지",
          "use_zone_or_structure": "계획관리지역",
          "official_area_sqm": 10934,
          "appraised_value": 3610761000
        }
      ]
    },
    {
      "location_lot_number": "경상남도 함안군 칠서면 계내리 21-1",
      "total_appraised_value": 145500000,
      "items": [
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "21-1",
          "land_category_or_use": "공장용지",
          "use_zone_or_structure": "계획관리지역",
          "official_area_sqm": 93,
          "appraised_value": 145500000
        }
      ]
    }
  ],
  "building": [
    {
      "location_lot_number": "경상남도 함안군 칠서면 계내리 12, 13-3, 13-4, 14-2, 18, 22 위 지상",
      "total_appraised_value": 2058134200,
      "items": [
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "12, 13-3, 13-4, 14-2, 18, 22 위 지상",
          "land_category_or_use": "공장",
          "use_zone_or_structure": "일반철골구조 판넬지붕 단층",
          "official_area_sqm": 1367.3,
          "appraised_value": 691853800
        },
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "12, 13-3, 13-4, 14-2, 18, 22 위 지상",
          "land_category_or_use": "부속건물(휴게실, 식당, 사무실, 기숙사)",
          "use_zone_or_structure": "철근콘크리트구조 (철근)콘크리트지붕 지상 4층",
          "official_area_sqm": 839.59,
          "appraised_value": 621296600
        },
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "12, 13-3, 13-4, 14-2, 18, 22 위 지상",
          "land_category_or_use": "공장",
          "use_zone_or_structure": "일반철골구조 판넬지붕 지상 2층",
          "official_area_sqm": 1472.3,
          "appraised_value": 744983800
        }
      ]
    },
    {
      "location_lot_number": "경상남도 함안군 칠서면 계내리 12, 21-1 위 지상",
      "total_appraised_value": 12960000,
      "items": [
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "12, 21-1 위 지상",
          "land_category_or_use": "콤프레샤실",
          "use_zone_or_structure": "경량철골조 판넬지붕 단층",
          "official_area_sqm": 21.0,
          "appraised_value": 3402000
        },
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "12, 21-1 위 지상",
          "land_category_or_use": "콤프레샤실",
          "use_zone_or_structure": "경량철골조 판넬지붕 단층",
          "official_area_sqm": 21.0,
          "appraised_value": 3402000
        },
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "12, 21-1 위 지상",
          "land_category_or_use": "창고",
          "use_zone_or_structure": "철파이프조 천막지붕 단층",
          "official_area_sqm": 60.0,
          "appraised_value": 2160000
        },
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "12, 21-1 위 지상",
          "land_category_or_use": "창고",
          "use_zone_or_structure": "철파이프조 천막지붕 단층",
          "official_area_sqm": 13.5,
          "appraised_value": 486000
        },
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "12, 21-1 위 지상",
          "land_category_or_use": "창고",
          "use_zone_or_structure": "경량철골조 천막지붕 단층",
          "official_area_sqm": 34.0,
          "appraised_value": 1530000
        },
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "12, 21-1 위 지상",
          "land_category_or_use": "창고",
          "use_zone_or_structure": "경량철골조 천막지붕 단층",
          "official_area_sqm": 40.0,
          "appraised_value": 1800000
        },
        {
          "location": "경상남도 함안군 칠서면 계내리",
          "lot_number": "12, 21-1 위 지상",
          "land_category_or_use": "창고",
          "use_zone_or_structure": "판넬조 판넬지붕 단층",
          "official_area_sqm": 6.0,
          "appraised_value": 180000
        }
      ]
    }
  ]
}
```


#### 기계기구 감정평가명세표
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
  },
  {
    "name": "Over Head Crane",
    "structure": "Double Girder Type, 기타 부대설비 포함",
    "specification": "Cap: 20(15+5)Ton; Span: 약 21m; Rail: 약 60m(기호 1 공용)",
    "manufacturer": "경남 HC",
    "manufacture_date": "2012.08",
    "quantity": 2,
    "unit_price": 17570000,
    "amount": 35140000
  },
  {
    "name": "Over Head Crane",
    "structure": "Double Girder Type, 기타 부대설비 포함",
    "specification": "Cap: 20Ton; Span: 약 21m; Rail: 약 60m",
    "manufacturer": "경남 HC",
    "manufacture_date": "2012.08",
    "quantity": 1,
    "unit_price": null,
    "amount": 17570000
  },
  {
    "name": "Over Head Crane",
    "structure": "Double Girder Type, 기타 부대설비 포함",
    "specification": "Cap: 20(15+5)Ton; Span: 약 21m; Rail: 약 60m(기호 3 공용)",
    "manufacturer": "경남 HC",
    "manufacture_date": "2012.08",
    "quantity": 1,
    "unit_price": null,
    "amount": 17570000
  },
  {
    "name": "Over Head Crane",
    "structure": "Double Girder Type, 기타 부대설비 포함",
    "specification": "Cap: 20Ton; Span: 약 21m; Rail: 약 60m",
    "manufacturer": "경남 HC",
    "manufacture_date": "2012.08",
    "quantity": 1,
    "unit_price": null,
    "amount": 17570000
  },
  {
    "name": "Over Head Crane",
    "structure": "Double Girder Type, 기타 부대설비 포함",
    "specification": "Cap: 20(15+5)Ton; Span: 약 21m; Rail: 약 60m(기호 5 공용)",
    "manufacturer": "경남 HC",
    "manufacture_date": "2012.08",
    "quantity": 1,
    "unit_price": null,
    "amount": 17570000
  },
  {
    "name": "Air Compressor",
    "structure": "Compressor 2식, Air Dryer(J EMACO), Air Receiver Tank, Filter 및 배관, 기타 부대설비 포함",
    "specification": "Compressor 1) Model: GA37VSD; Cap: 50HP(37KW); S'n: API480270 / Compressor 2) Model: GA37P-A-125; Cap: 50HP(37KW); S'n: WUX334846 / Air Dryer: HX426K",
    "manufacturer": "Atlas Copco",
    "manufacture_date": "2012.06",
    "quantity": 1,
    "unit_price": null,
    "amount": 10880000
  },
  {
    "name": "Air Compressor",
    "structure": "Compressor 2식, Air Dryer(J EMACO), Air Receiver Tank, Filter 및 배관, 기타 부대설비 포함",
    "specification": "Compressor 1) Model: GA37VSD; Cap: 50HP(37KW); S'n: API480343 / Compressor 2) Model: GA37P-A-125; Cap: 50HP(37KW); S'n: WUX334847 / Air Dryer: HX426K",
    "manufacturer": "Atlas Copco",
    "manufacture_date": "2012.06",
    "quantity": 1,
    "unit_price": null,
    "amount": 10880000
  },
  {
    "name": "수변전설비",
    "structure": "BOX 일체형; 자립형분전반 2면; 벽부형분전반 10면; Cable Tray; 기타 부대설비 일체 포함",
    "specification": "Cap: 500kW(Main); 22900/380V(1차); 외함: 2500 × 2800 × 3500mm; T.R: 500KVA × 1ea; MOF, CT, PT, ACB; 2차 380V/220V; T.R: 100KVA × 2ea; 동력간선외; Cable: F-CV240SQ × 1C × 1000m; Cable: F-CV70SQ × 4C × 800m; Cable: F-GV70SQ × 300m; Cable: F-GV25SQ × 800m",
    "manufacturer": "한영전기",
    "manufacture_date": "2012.08",
    "quantity": 1,
    "unit_price": null,
    "amount": 35140000
  }
]
```

output : "output/appraisal/감정평가서 휴먼중공업.json"

- 페이지 번호 감지
감정평가서 -> 토지·건물 감정평가명세표 페이지만 파싱  ex. (29, 31)
감정평가서 -> 기계기구 감정평가명세표 페이지만 파싱 ex. (31, 35)

- gpt모델은, gpt-5.6-sol모델로 지정.
- "detail=high" 지정

해당 페이지를 기주


