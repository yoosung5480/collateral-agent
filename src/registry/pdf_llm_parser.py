import asyncio
import os
from dotenv import load_dotenv

load_dotenv('.env')
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

import base64
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader, PdfWriter

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .json_model import JsonModel

REGISTRY_BOUNDARY_SYSTEM_PROMPT = """
너는 여러 개의 대한민국 부동산 등기사항전부증명서가 연속 결합된 PDF의 문서 경계를 찾는 판정기다.

목표:
각 독립된 등기사항전부증명서가 시작하는 PDF 페이지 번호만 찾아 오름차순으로 반환한다.

판정 기준:
- 새 문서는 일반적으로 '등기사항전부증명서' 제목과 함께 시작한다.
- 토지 또는 건물 등 문서 종류가 새로 표시된다.
- 문서 내부 페이지 표기가 1/N 형태로 초기화된다.
- 고유번호와 소재지번이 이전 문서와 달라질 수 있다.
- 위 단서 중 하나만 기계적으로 사용하지 말고 서로 교차 확인한다.
- PDF의 첫 페이지 번호는 1이다.

제한:
- 표제부, 갑구, 을구의 내용을 추출하거나 요약하지 않는다.
- 문서 종류, 고유번호, 소재지 등 부가정보를 출력하지 않는다.
- 각 문서의 시작 페이지 번호 외에는 아무 정보도 반환하지 않는다.
- 같은 시작 페이지를 중복 반환하지 않는다.
- 반드시 지정된 출력 스키마만 따른다.
""".strip()


SECTION_PAGE_SYSTEM_PROMPT = """
너는 단 하나의 대한민국 부동산 등기사항전부증명서 PDF에서 구역별 페이지를 찾는 판정기다.

목표:
표제부가 존재하는 PDF 페이지 번호와 을구가 존재하는 PDF 페이지 번호만 각각 반환한다.

표제부 판정:
- '[표제부]' 제목이 시작되는 페이지를 포함한다.
- 표제부 표가 다음 페이지로 이어지면 제목이 반복되지 않더라도 그 연속 페이지를 모두 포함한다.
- 토지의 표시, 건물의 표시 등 표제부 내용이 존재하는 페이지를 포함한다.

을구 판정:
- '[을구]' 또는 '소유권 이외의 권리에 관한 사항'이 시작되는 페이지를 포함한다.
- 을구 표가 다음 페이지로 이어지면 제목이 반복되지 않더라도 그 연속 페이지를 모두 포함한다.
- 근저당권 등의 내용뿐 아니라 '기록사항 없음'만 적힌 을구 페이지도 포함한다.
- 공동담보목록이나 별도 첨부 목록은 을구 본문 페이지로 오인하지 않는다.

중요한 페이지 경계 규칙:
- 한 PDF 페이지에 앞 구역의 끝과 새 구역의 시작이 함께 있으면, 해당 페이지는 새 구역에도 포함한다.
- 따라서 한 페이지에 표제부와 을구가 모두 실제로 존재하면 두 목록에 같은 페이지 번호가 들어갈 수 있다.
- PDF의 첫 페이지 번호는 1이다.

제한:
- 등기 내용, 권리자, 금액, 말소 여부를 추출하거나 판단하지 않는다.
- 갑구 페이지를 표제부나 을구로 분류하지 않는다.
- 페이지 번호 외에는 아무 정보도 반환하지 않는다.
- 페이지 번호는 각 목록 안에서 중복 없이 오름차순으로 반환한다.
- 반드시 지정된 출력 스키마만 따른다.
""".strip()

class RegistrySplitPlan(JsonModel):
    start_pages: list[int] = Field(
        min_length=1,
        description=(
            "각 독립 등기사항전부증명서가 시작하는 원본 PDF 페이지 번호. "
            "1부터 시작하며 중복 없이 오름차순이어야 한다."
        ),
    )

    @field_validator("start_pages")
    @classmethod
    def validate_start_pages(cls, pages: list[int]) -> list[int]:
        if not pages or pages[0] != 1:
            raise ValueError("첫 번째 문서의 시작 페이지는 1이어야 합니다.")
        if any(page < 1 for page in pages):
            raise ValueError("페이지 번호는 1 이상이어야 합니다.")
        if pages != sorted(set(pages)):
            raise ValueError("페이지 번호는 중복 없이 오름차순이어야 합니다.")
        return pages


class RegistrySectionPlan(JsonModel):
    title_pages: list[int] = Field(
        min_length=1,
        description="현재 등기부등본 PDF에서 표제부가 존재하는 1-based 페이지 번호",
    )
    encumbrance_pages: list[int] = Field(
        min_length=1,
        description="현재 등기부등본 PDF에서 을구가 존재하는 1-based 페이지 번호",
    )

    @field_validator("title_pages", "encumbrance_pages")
    @classmethod
    def validate_page_list(cls, pages: list[int]) -> list[int]:
        if any(page < 1 for page in pages):
            raise ValueError("페이지 번호는 1 이상이어야 합니다.")
        if pages != sorted(set(pages)):
            raise ValueError("페이지 번호는 중복 없이 오름차순이어야 합니다.")
        return pages

def encode_pdf(path: str | Path) -> str:
    with Path(path).open("rb") as file:
        return base64.b64encode(file.read()).decode("utf-8")


def build_pdf_message(path: str | Path, instruction: str) -> HumanMessage:
    path = Path(path)
    return HumanMessage(
        content=[
            {
                "type": "text",
                "text": instruction,
            },
            {
                "type": "file",
                "base64": encode_pdf(path),
                "filename": path.name,
                "mime_type": "application/pdf",
            },
        ]
    )


def build_page_ranges(
    split_plan: RegistrySplitPlan,
    total_pages: int,
) -> list[tuple[int, int]]:
    starts = split_plan.start_pages
    if starts[-1] > total_pages:
        raise ValueError("시작 페이지가 PDF 전체 페이지 수를 초과합니다.")

    return [
        (
            start,
            starts[index + 1] - 1
            if index + 1 < len(starts)
            else total_pages,
        )
        for index, start in enumerate(starts)
    ]


def write_page_range(
    reader: PdfReader,
    start_page: int,
    end_page: int,
    output_path: str | Path,
) -> str:
    writer = PdfWriter()
    for page_index in range(start_page - 1, end_page):
        writer.add_page(reader.pages[page_index])

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as file:
        writer.write(file)
    return str(output_path)


def write_selected_pages(
    reader: PdfReader,
    pages: list[int],
    output_path: str | Path,
) -> str:
    writer = PdfWriter()
    for page in pages:
        writer.add_page(reader.pages[page - 1])

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as file:
        writer.write(file)
    return str(output_path)


def validate_section_plan(
    plan: RegistrySectionPlan,
    total_pages: int,
) -> None:
    for section_name, pages in (
        ("표제부", plan.title_pages),
        ("을구", plan.encumbrance_pages),
    ):
        invalid_pages = [page for page in pages if page > total_pages]
        if invalid_pages:
            raise ValueError(
                f"{section_name} 페이지가 PDF 범위를 초과했습니다: {invalid_pages} "
                f"(전체 {total_pages}페이지)"
            )



# registry_page_parser = llm.with_structured_output(
#     RegistrySplitPlan,
#     method="json_schema",
# )

# section_page_parser = llm.with_structured_output(
#     RegistrySectionPlan,
#     method="json_schema",
# )


class RawRegistryParser:
    def __init__(self, llm):
        self.llm = llm
        self.registry_page_parser = llm.with_structured_output(
            RegistrySplitPlan,
            method="json_schema",
        )

        self.section_page_parser = llm.with_structured_output(
            RegistrySectionPlan,
            method="json_schema",
        )


    def find_registry_start_pages(self, path: str | Path) -> RegistrySplitPlan:
        message = build_pdf_message(
            path,
            "이 통합 PDF에서 각 독립 등기사항전부증명서의 시작 페이지 번호만 반환하라.",
        )
        return self.registry_page_parser.invoke(
            [SystemMessage(content=REGISTRY_BOUNDARY_SYSTEM_PROMPT), message]
        )

    async def async_find_registry_start_pages(self, path: str | Path) -> RegistrySplitPlan:
        message = build_pdf_message(
            path,
            "이 통합 PDF에서 각 독립 등기사항전부증명서의 시작 페이지 번호만 반환하라.",
        )
        return await self.registry_page_parser.ainvoke(
            [SystemMessage(content=REGISTRY_BOUNDARY_SYSTEM_PROMPT), message]
        )

    def find_section_pages(self, path: str | Path) -> RegistrySectionPlan:
        message = build_pdf_message(
            path,
            "이 단일 등기부등본에서 표제부 페이지 번호와 을구 페이지 번호만 반환하라.",
        )
        return self.section_page_parser.invoke(
            [SystemMessage(content=SECTION_PAGE_SYSTEM_PROMPT), message]
        )

    async def async_find_section_pages(self, path: str | Path) -> RegistrySectionPlan:
        message = build_pdf_message(
            path,
            "이 단일 등기부등본에서 표제부 페이지 번호와 을구 페이지 번호만 반환하라.",
        )
        return await self.section_page_parser.ainvoke(
            [SystemMessage(content=SECTION_PAGE_SYSTEM_PROMPT), message]
        )


    def split_registry_sections(
        self,
        source_path: str | Path,
        output_path: str | Path,
    ) -> list[dict]:
        """통합 등기부 PDF를 부동산별 표제부/을구 PDF로 분리한다."""

        source_path = Path(source_path)
        output_path = Path(output_path)
        source_reader = PdfReader(str(source_path))
        total_pages = len(source_reader.pages)

        registry_plan = self.find_registry_start_pages(source_path)
        page_ranges = build_page_ranges(registry_plan, total_pages)
        results = []

        # 중간 등기부 PDF는 LLM 판정에만 사용하고 결과 디렉토리에는 남기지 않는다.
        with tempfile.TemporaryDirectory(prefix="registry_split_") as temp_dir:
            temp_dir = Path(temp_dir)

            for index, (start_page, end_page) in enumerate(page_ranges, start=1):
                registry_name = f"registry_{index:02d}_p{start_page}-{end_page}"
                temp_registry_path = temp_dir / f"{registry_name}.pdf"

                write_page_range(
                    source_reader,
                    start_page,
                    end_page,
                    temp_registry_path,
                )

                section_plan = self.find_section_pages(temp_registry_path)
                registry_reader = PdfReader(str(temp_registry_path))
                validate_section_plan(section_plan, len(registry_reader.pages))

                registry_output_dir = output_path / registry_name
                title_path = write_selected_pages(
                    registry_reader,
                    section_plan.title_pages,
                    registry_output_dir / "title.pdf",
                )
                encumbrance_path = write_selected_pages(
                    registry_reader,
                    section_plan.encumbrance_pages,
                    registry_output_dir / "encumbrance.pdf",
                )

                results.append(
                    {
                        "registry": registry_name,
                        "original_pages": [start_page, end_page],
                        "title_pages_in_registry": section_plan.title_pages,
                        "encumbrance_pages_in_registry": section_plan.encumbrance_pages,
                        "title_path": title_path,
                        "encumbrance_path": encumbrance_path,
                    }
                )

        return results

    async def async_split_registry_sections(
        self,
        source_path: str | Path,
        output_path: str | Path,
    ) -> list[dict]:
        """통합 등기부 PDF를 부동산별 표제부/을구 PDF로 비동기 분리한다.

        통합 PDF의 시작 페이지 판정은 선행되어야 하지만, 각 등기부별 표제부/을구
        페이지 판정은 독립 LLM 호출이므로 동시에 실행한다.
        """

        source_path = Path(source_path)
        output_path = Path(output_path)
        source_reader = PdfReader(str(source_path))
        total_pages = len(source_reader.pages)

        registry_plan = await self.async_find_registry_start_pages(source_path)
        page_ranges = build_page_ranges(registry_plan, total_pages)
        results = []

        with tempfile.TemporaryDirectory(prefix="registry_split_") as temp_dir:
            temp_dir = Path(temp_dir)
            registry_inputs: list[tuple[int, int, int, str, Path]] = []
            for index, (start_page, end_page) in enumerate(page_ranges, start=1):
                registry_name = f"registry_{index:02d}_p{start_page}-{end_page}"
                temp_registry_path = temp_dir / f"{registry_name}.pdf"
                write_page_range(source_reader, start_page, end_page, temp_registry_path)
                registry_inputs.append((index, start_page, end_page, registry_name, temp_registry_path))

            section_plans = await asyncio.gather(
                *(self.async_find_section_pages(temp_registry_path) for *_, temp_registry_path in registry_inputs)
            )

            for (_, start_page, end_page, registry_name, temp_registry_path), section_plan in zip(registry_inputs, section_plans, strict=True):
                registry_reader = PdfReader(str(temp_registry_path))
                validate_section_plan(section_plan, len(registry_reader.pages))

                registry_output_dir = output_path / registry_name
                title_path = write_selected_pages(
                    registry_reader,
                    section_plan.title_pages,
                    registry_output_dir / "title.pdf",
                )
                encumbrance_path = write_selected_pages(
                    registry_reader,
                    section_plan.encumbrance_pages,
                    registry_output_dir / "encumbrance.pdf",
                )

                results.append(
                    {
                        "registry": registry_name,
                        "original_pages": [start_page, end_page],
                        "title_pages_in_registry": section_plan.title_pages,
                        "encumbrance_pages_in_registry": section_plan.encumbrance_pages,
                        "title_path": title_path,
                        "encumbrance_path": encumbrance_path,
                    }
                )

        return results

# input_path = "../datas/부동산등기부등본.pdf"
# output_path = "../preprocess/registry_split"

# llm = ChatOpenAI(
#     model="gpt-5.4-mini",
#     api_key=OPENAI_API_KEY,
#     temperature=0,
# )

# parser = RawRegistryParser(llm=llm)
# results = parser.split_registry_sections(
#     source_path=input_path,
#     output_path=output_path,
# )

# for result in results:
#     print(result)
