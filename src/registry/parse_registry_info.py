import os
import asyncio
import base64
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .json_model import JsonModel

load_dotenv(".env")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


# =========================================================
# 시스템 프롬프트
# =========================================================

TITLE_SYSTEM_PROMPT = """
너는 대한민국 부동산 등기부등본을 검토하는 회계사다.
입력된 등기부등본 전체에서 [표제부]의 현재 유효한 부동산 정보만 추출하라.

판단 규칙:
1. 빨간선·취소선·빗금 등으로 말소된 문구는 절대 포함하지 않는다.
2. 면적, 지목, 구조, 용도 등이 변경된 경우 변경 이력을 나열하지 말고
   최종적으로 유효한 현재 정보만 반환한다.
3. '마지막 정보'란 단순히 문서의 마지막 행이 아니라,
   변경·말소 내역을 반영한 현재 살아 있는 정보를 의미한다.
4. 토지는 소재지, 현재 지목과 면적을 추출한다.
5. 건물은 소재지와 동·호, 현재 구조·용도·층별 면적 등의 유효 문구를
   원문의 자연어 형식을 최대한 유지하여 하나의 details 문자열로 작성한다.
6. 갑구와 을구의 권리 정보는 표제부 결과에 섞지 않는다.
7. 문서에 없는 정보는 추측하지 않는다.
""".strip()


ENCUMBRANCE_SYSTEM_PROMPT = """
너는 대한민국 부동산 등기부등본을 검토하는 회계사다.
입력된 등기부등본 전체에서 [을구]의 현재 유효한 근저당권만 추출하라. 

판단 규칙:
1. 빨간선·취소선·빗금 등으로 말소된 근저당권은 포함하지 않는다.
2. 말소등기 또는 해지등기가 확인된 권리는 현재 유효하지 않은 것으로 제외한다.
3. 8, 8-1, 8-2처럼 동일한 원등기에 부기등기가 이어지는 경우,
   각각을 별도 근저당권으로 만들지 말고 변경 내용을 원등기에 반영한다.
4. 변경 전 정보가 말소되고 변경 후 정보가 살아 있다면 변경 후 정보만 사용한다.
5. registration은 원래 근저당권이 설정된 등기일자를 사용한다.
   변경등기 날짜로 대체하지 않는다.
6. 채권최고액과 근저당권자는 변경등기를 모두 반영한 현재 유효 값을 반환한다.
7. 압류, 가압류, 지상권 등 근저당권이 아닌 권리는 포함하지 않는다.
8. 문서에 없는 정보는 추측하지 않는다.

### 예시 
#### 등기목적
근저당권설정

#### 접수
2012년12월20일 제31426호

#### 권리자 및 기타사항
채권최고액  금120,000,000원
채무자  휴먼중공업주식회사
    경상남도 함안군 칠서면 계룡로 110
근저당권자  중소기업은행 110135-0000903
    서울특별시 중구 을지로2가 50
    ( 팔용동지점 )
공동탐보    
    토지 경상남도 함안군 칠서면 계내리 12
    건물 경상남도 함안군 칠서면 계내리 12 외 5필지 제에이동호
공장 및 광업재단 저당법 제6조 목록 제 2012-720호


### 결과
mortgages["registration"] : "2012.12.20",       # "접수" 기준
mortgages["maximum_claim"]: "금 120,000,000원",
mortgages["mortgagee"]: "중소기업은행",
mortgages["shared_collateral"]: "공용담보"
mortgages["mortgage_list_no"]: "공장 및 광업재단 저당법 제6조 목록 제 2012-720호"


""".strip()


# =========================================================
# 출력 스키마
# =========================================================

class TitleSection(JsonModel):
    registry_id: str = Field(
        description="출처가 되는 분할 등기부등본 PDF 경로"
    )
    property_type: Literal["토지", "건물"] = Field(
        description="등기부등본의 부동산 종류"
    )
    location: str = Field(
        description="현재 유효한 소재지와 건물의 동·호 정보"
    )
    details: str = Field(
        description=(
            "말소된 내용과 변경 전 과거 정보는 제외하고, 현재 유효한 "
            "부동산 내역만 원문의 자연어 형식으로 작성"
        )
    )


class Mortgage(BaseModel):
    registration: str = Field(
        description="접수 기준 근저당권의 최초 설정 등기일자. 예: 2012.8.20"
    )
    maximum_claim: str = Field(
        description="변경등기를 반영한 현재 채권최고액. 예: 금 5,040,000,000원"
    )
    mortgagee: str = Field(
        description="변경등기를 반영한 현재 근저당권자"
    )
    shared_collateral: str = Field(
        default="",
        description=(
            "해당 근저당권에 공동담보가 설정되어 있으면 '공용담보'로 반환하고, "
            "공동담보 관련 기재가 없으면 빈 문자열('')로 반환"
        )
    )
    mortgage_list_no: str = Field(
        default="",
        description=(
            "해당 근저당권에 연결된 공장 및 광업재단 저당법상 목록의 명칭과 번호를 "
            "원문대로 반환. 예: '공장 및 광업재단 저당법 제6조 목록 제2018-234호'. "
            "관련 목록이 기재되어 있지 않으면 빈 문자열('')로 반환"
        )
    )

class EulSection(JsonModel):
    registry_id: str = Field(
        description="출처가 되는 분할 등기부등본 PDF 경로"
    )
    mortgages: list[Mortgage] = Field(
        default_factory=list,
        description="을구에서 현재 유효한 근저당권 목록"
    )


# =========================================================
# PDF 메시지
# =========================================================

def build_pdf_message(path: str, instruction: str) -> HumanMessage:
    with open(path, "rb") as file:
        pdf_base64 = base64.b64encode(file.read()).decode("utf-8")

    return HumanMessage(
        content=[
            {
                "type": "text",
                "text": instruction,
            },
            {
                "type": "file",
                "base64": pdf_base64,
                "filename": Path(path).name,
                "mime_type": "application/pdf",
                "detail": "high",
            },
        ]
    )



# 정확도 우선이면 mini보다 gpt-4.1 사용 권장



# title_extractor = llm.with_structured_output(
#     TitleSection,
#     method="json_schema",
# )

# encumbrance_extractor = llm.with_structured_output(
#     EulSection,
#     method="json_schema",
# )

from pathlib import Path

PathInput = str | Path


class RegistryParser:
    def __init__(self, llm: ChatOpenAI):
        self.llm = llm
        self.title_extractor = llm.with_structured_output(
            TitleSection,
            method="json_schema",
        )
        self.encumbrance_extractor = llm.with_structured_output(
            EulSection,
            method="json_schema",
        )

    def extract_title(self, source_path: PathInput) -> TitleSection:
        source_path = Path(source_path)

        user_message = build_pdf_message(
            source_path,
            "첨부한 등기부등본 전체를 확인하고 표제부의 현재 유효 정보만 추출하라.",
        )

        result = self.title_extractor.invoke([
            SystemMessage(content=TITLE_SYSTEM_PROMPT),
            user_message,
        ])

        result.registry_id = str(source_path)
        return result

    def extract_encumbrance(self, source_path: PathInput) -> EulSection:
        source_path = Path(source_path)

        user_message = build_pdf_message(
            source_path,
            "첨부한 등기부등본 전체를 확인하고 을구의 현재 유효한 근저당권만 추출하라.",
        )

        result = self.encumbrance_extractor.invoke([
            SystemMessage(content=ENCUMBRANCE_SYSTEM_PROMPT),
            user_message,
        ])

        result.registry_id = str(source_path)
        return result

    async def async_extract_title(
        self,
        source_path: PathInput,
    ) -> TitleSection:
        source_path = Path(source_path)

        user_message = build_pdf_message(
            source_path,
            "첨부한 등기부등본 전체를 확인하고 표제부의 현재 유효 정보만 추출하라.",
        )

        result = await self.title_extractor.ainvoke([
            SystemMessage(content=TITLE_SYSTEM_PROMPT),
            user_message,
        ])

        result.registry_id = str(source_path)
        return result

    async def async_extract_encumbrance(
        self,
        source_path: PathInput,
    ) -> EulSection:
        source_path = Path(source_path)

        user_message = build_pdf_message(
            source_path,
            "첨부한 등기부등본 전체를 확인하고 을구의 현재 유효한 근저당권만 추출하라.",
        )

        result = await self.encumbrance_extractor.ainvoke([
            SystemMessage(content=ENCUMBRANCE_SYSTEM_PROMPT),
            user_message,
        ])

        result.registry_id = str(source_path)
        return result

    async def async_parse_registry(
        self,
        source_path: PathInput,
    ) -> tuple[TitleSection, EulSection]:
        title_result, eul_result = await asyncio.gather(
            self.async_extract_title(source_path),
            self.async_extract_encumbrance(source_path),
        )

        return title_result, eul_result

# ==================================================================
# import time

# llm = ChatOpenAI(
#     model="gpt-5.6-sol",
#     api_key=OPENAI_API_KEY,
#     temperature=0,
#     use_responses_api=True,
#     max_retries=3,
# )

# parser = RegistryParser(llm=llm)

# title_source_path = "../preprocess/registry_split/registry_01_p1-18/title.pdf"
# eul_source_path = "../preprocess/registry_split/registry_01_p1-18/encumbrance.pdf"
# print("==================표제부추출====================")
# start_time = time.perf_counter()
# title_result = parser.extract_title(source_path=title_source_path)
# end_time = time.perf_counter()
# print(f"실행 시간: {end_time - start_time:.3f}초")
# print(title_result)
# print("==========================================")


# print("===================을구추출=================")
# start_time = time.perf_counter()
# eul_result = parser.extract_encumbrance(source_path=eul_source_path)
# end_time = time.perf_counter()
# print(f"실행 시간: {end_time - start_time:.3f}초")
# print(eul_result)
# print("==========================================")
