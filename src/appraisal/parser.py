"""감정평가서 PDF 2단계 LLM 분리·추출 워크플로우."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import fitz
from PIL import Image, ImageDraw
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator, model_validator
from pypdf import PdfReader, PdfWriter

PathInput = str | Path


SECTION_PAGE_SYSTEM_PROMPT = """
너는 대한민국 감정평가서 PDF에서 목차와 문서 제목을 근거로 필요한 표 구간의 PDF 페이지 범위를 찾는 판정기다.

목표:
원본 감정평가서에서 다음 두 구간의 시작 페이지와 끝 페이지를 1-based PDF 페이지 번호로 반환한다.
1. 토지·건물 감정평가명세표
2. 기계기구 감정평가명세표

판정 규칙:
- 문서 목차, 대목차 제목, 표 제목, 표 머리글을 함께 확인한다.
- 반환값은 문서 안에 인쇄된 Page 번호가 아니라 PDF 뷰어 기준 물리 페이지 번호다. 첨부 이미지의 빨간 PDF PAGE 라벨을 기준으로 한다.
- 토지·건물 구간은 상단 제목이 정확히 "토지·건물 감정평가명세표"인 첫 페이지에서 시작한다. "감정평가액의 산출근거 및 결정 의견", "건물 감정평가액", "토지 감정평가액" 산출근거 페이지는 포함하지 않는다.
- 기계기구 구간은 상단 제목이 정확히 "기계기구 감정평가명세표"인 첫 페이지에서 시작한다.
- 표 본문이 다음 페이지로 이어지면 제목이 반복되지 않아도 같은 구간에 포함한다.
- 다음 대목차(예: 토지 감정평가요항표, 건물 감정평가요항표, 기계기구 감정평가요항표, 위치도 등)가 시작되기 직전 페이지까지만 포함한다.
- 한 PDF 페이지에 앞 구간의 끝과 다음 구간의 시작이 함께 있으면 두 구간에 같은 페이지가 들어갈 수 있다.
- PDF 페이지 번호는 첫 페이지가 1이다.

제한:
- 내용을 추출하지 말고 페이지 범위만 판단한다.
- 추측으로 넓은 범위를 넣지 않는다. 해당 명세표 표가 실제로 보이는 페이지만 포함한다.
- 반드시 지정된 출력 스키마만 따른다.
""".strip()


LAND_BUILDING_SYSTEM_PROMPT = """
너는 대한민국 회계법인의 담보물 배분표 자동작성용 감정평가 명세표 분석가다.
첨부된 '토지·건물 감정평가명세표' PDF만 읽고 README의 예시 형태로 토지와 건물을 구조화한다.

그룹화 규칙:
1. location_lot_number는 정규화한 location과 lot_number를 공백 하나로 결합한다.
2. location_lot_number가 같은 행만 동일 그룹으로 묶는다. 소재지가 같아도 지번이 다르면 별도 그룹이다.
3. 건물의 동·층·부속건물 명칭이 달라도 소재지와 지번이 같으면 같은 그룹의 서로 다른 items로 저장한다.
4. 제시외 건물도 building에 포함한다. 해당 구역에서 새로 명시된 소재지와 지번을 기준으로 그룹을 만든다.
5. total_appraised_value는 해당 그룹의 모든 items[].appraised_value 합계다. 소계·합계행 금액을 다시 더하지 않는다.

필드 규칙:
- location과 lot_number는 반드시 분리한다.
- '동소'는 같은 열의 직전 명시값이다. 소재지 열과 지번 열을 각각 독립적으로 승계한다.
- land_category_or_use에는 표의 '지목 및 용도'를 기록한다.
- use_zone_or_structure에는 토지의 용도지역 또는 건물의 구조·지붕·층수를 기록한다.
- official_area_sqm에는 '면적-공부' 열의 값을 제곱미터 숫자로 기록한다. 여러 층의 공부면적이 한 항목이면 합산한다.
- appraised_value는 '감정평가액-금액' 열의 값만 사용한다.
- 면적-사정, 감정평가액-단가, 비고는 출력하지 않는다.
- 일련번호는 출력하지 않는다.
- 금액은 쉼표와 원화 기호를 제거한 원 단위 정수다. 면적은 숫자다.
- 읽을 수 없는 문자열이나 공부면적은 추정하지 말고 null로 둔다.
- 각 그룹 total_appraised_value와 items[].appraised_value 합계가 일치해야 한다.
""".strip()


MACHINERY_SYSTEM_PROMPT = """
너는 대한민국 회계법인의 담보물 배분표 자동작성용 기계기구 감정평가명세표 분석가다.
첨부된 '기계기구 감정평가명세표' PDF만 읽고 README의 예시 배열 형태로 기계기구를 구조화한다.

추출 규칙:
- 표의 기계기구 일련번호가 실제 품목의 기준 행이다. 합계, 이하 여백, 주석 행은 품목으로 만들지 않는다.
- 명칭, 구조, 규격, 제작자, 제작일자가 여러 줄로 나뉘면 같은 품목으로 합친다.
- name은 기계명, structure는 형식/구조 및 부대설비 설명, specification은 용량·Span·Rail·Model·Serial 등 세부 규격이다.
- manufacture_date는 원문 형식의 연월(예: 2012.08)로 기록한다.
- quantity는 숫자, unit_price는 단가가 명시된 경우 정수, 없으면 null, amount는 감정평가액 금액 열의 원 단위 정수다.
- 금액은 쉼표와 원화 기호를 제거한다.
- 문서에 없는 값은 추정하지 말고 null로 둔다.
- 반드시 지정된 출력 스키마만 따른다.
""".strip()


class JsonModel(BaseModel):
    def save_json(self, output_path: PathInput, *, indent: int = 2) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(self.model_dump_json(indent=indent), encoding="utf-8")
        return output_path

    @classmethod
    def load_json(cls, input_path: PathInput):
        return cls.model_validate_json(Path(input_path).read_text(encoding="utf-8"))


class AppraisalSectionPlan(JsonModel):
    land_building_start_page: int = Field(description="상단 제목이 정확히 토지·건물 감정평가명세표인 첫 물리 PDF 페이지 번호(1-based). 인쇄된 Page 번호가 아니라 빨간 PDF PAGE 라벨 기준")
    land_building_end_page: int = Field(description="토지·건물 감정평가명세표 끝 PDF 페이지 번호(1-based, 포함)")
    machinery_start_page: int = Field(description="상단 제목이 정확히 기계기구 감정평가명세표인 첫 물리 PDF 페이지 번호(1-based). 인쇄된 Page 번호가 아니라 빨간 PDF PAGE 라벨 기준")
    machinery_end_page: int = Field(description="기계기구 감정평가명세표 끝 PDF 페이지 번호(1-based, 포함)")

    @field_validator("land_building_start_page", "land_building_end_page", "machinery_start_page", "machinery_end_page")
    @classmethod
    def positive_page(cls, page: int) -> int:
        if page < 1:
            raise ValueError("PDF 페이지 번호는 1 이상이어야 합니다.")
        return page

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.land_building_start_page > self.land_building_end_page:
            raise ValueError("토지·건물 명세표 시작 페이지가 끝 페이지보다 큽니다.")
        if self.machinery_start_page > self.machinery_end_page:
            raise ValueError("기계기구 명세표 시작 페이지가 끝 페이지보다 큽니다.")
        return self


class LandBuildingAssetItem(BaseModel):
    location: str | None = Field(description="소재지")
    lot_number: str | None = Field(description="지번 또는 건물 지상 표기")
    land_category_or_use: str | None = Field(description="지목 및 용도")
    use_zone_or_structure: str | None = Field(description="용도지역 또는 구조")
    official_area_sqm: float | int | None = Field(description="면적-공부 열의 제곱미터 숫자")
    appraised_value: int = Field(description="감정평가액-금액 열의 원 단위 정수")


class LandBuildingGroup(BaseModel):
    location_lot_number: str = Field(description="소재지와 지번 결합값")
    total_appraised_value: int = Field(description="그룹 내 평가금액 합계")
    items: list[LandBuildingAssetItem] = Field(min_length=1)


class LandBuildingExtraction(JsonModel):
    land: list[LandBuildingGroup] = Field(default_factory=list, min_length=1)
    building: list[LandBuildingGroup] = Field(default_factory=list, min_length=1)


class MachineryItem(BaseModel):
    name: str | None = None
    structure: str | None = None
    specification: str | None = None
    manufacturer: str | None = None
    manufacture_date: str | None = None
    quantity: float | int | None = None
    unit_price: int | None = None
    amount: int = Field(description="감정평가액 금액 열의 원 단위 정수")


class MachineryExtraction(JsonModel):
    machinery: list[MachineryItem] = Field(default_factory=list, min_length=1)


class AppraisalExtraction(JsonModel):
    land: list[LandBuildingGroup] = Field(default_factory=list, min_length=1)
    building: list[LandBuildingGroup] = Field(default_factory=list, min_length=1)
    machinery: list[MachineryItem] = Field(default_factory=list, min_length=1)


class AppraisalWorkflowResult(BaseModel):
    source_path: str
    split_dir: str
    land_building_pdf: str
    machinery_pdf: str
    json_path: str
    section_plan: AppraisalSectionPlan
    data: AppraisalExtraction


def encode_pdf(path: PathInput) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("utf-8")


def build_pdf_message(path: PathInput, instruction: str) -> HumanMessage:
    path = Path(path)
    return HumanMessage(
        content=[
            {"type": "text", "text": instruction},
            {
                "type": "file",
                "base64": encode_pdf(path),
                "filename": path.name,
                "mime_type": "application/pdf",
                "detail": "high",
            },
        ]
    )




def render_pdf_page_images(path: PathInput, *, zoom: float = 2.5) -> list[str]:
    document = fitz.open(str(path))
    images: list[str] = []
    matrix = fitz.Matrix(zoom, zoom)
    for page in document:
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        images.append(base64.b64encode(pixmap.tobytes("png")).decode("utf-8"))
    document.close()
    return images


def build_pdf_with_page_images_message(path: PathInput, instruction: str) -> HumanMessage:
    path = Path(path)
    content: list[dict[str, Any]] = [
        {"type": "text", "text": instruction},
        {
            "type": "file",
            "base64": encode_pdf(path),
            "filename": path.name,
            "mime_type": "application/pdf",
            "detail": "high",
        },
    ]
    for index, image_base64 in enumerate(render_pdf_page_images(path), start=1):
        content.append({"type": "text", "text": f"{path.name} page {index}"})
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{image_base64}",
                    "detail": "high",
                },
            }
        )
    return HumanMessage(content=content)




def render_numbered_contact_sheets(
    path: PathInput,
    *,
    start_page: int = 1,
    end_page: int | None = None,
    pages_per_sheet: int = 4,
    zoom: float = 0.9,
) -> list[str]:
    document = fitz.open(str(path))
    last_page = min(end_page or len(document), len(document))
    rendered: list[tuple[int, Image.Image]] = []
    matrix = fitz.Matrix(zoom, zoom)
    for page_no in range(start_page, last_page + 1):
        page = document[page_no - 1]
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
        rendered.append((page_no, image))
    document.close()

    sheets: list[str] = []
    for offset in range(0, len(rendered), pages_per_sheet):
        chunk = rendered[offset : offset + pages_per_sheet]
        max_width = max(image.width for _, image in chunk)
        max_height = max(image.height for _, image in chunk)
        label_height = 36
        sheet = Image.new("RGB", (max_width * 2, (max_height + label_height) * 2), "white")
        draw = ImageDraw.Draw(sheet)
        for index, (page_no, image) in enumerate(chunk):
            x = (index % 2) * max_width
            y = (index // 2) * (max_height + label_height)
            draw.text((x + 10, y + 8), f"PDF PAGE {page_no}", fill=(220, 0, 0))
            sheet.paste(image, (x, y + label_height))
        import io

        buffer = io.BytesIO()
        sheet.save(buffer, format="PNG")
        sheets.append(base64.b64encode(buffer.getvalue()).decode("utf-8"))
    return sheets


def build_pdf_with_contact_sheets_message(path: PathInput, instruction: str, *, end_page: int) -> HumanMessage:
    path = Path(path)
    content: list[dict[str, Any]] = [
        {"type": "text", "text": instruction},
        {
            "type": "file",
            "base64": encode_pdf(path),
            "filename": path.name,
            "mime_type": "application/pdf",
            "detail": "high",
        },
        {"type": "text", "text": f"아래 이미지는 원본 PDF의 1번부터 {end_page}번까지 물리 페이지를 빨간 라벨로 묶은 contact sheet다. 반드시 빨간 PDF PAGE 번호를 반환하라."},
    ]
    for sheet_index, image_base64 in enumerate(render_numbered_contact_sheets(path, end_page=end_page), start=1):
        content.append({"type": "text", "text": f"contact sheet {sheet_index}"})
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{image_base64}",
                    "detail": "high",
                },
            }
        )
    return HumanMessage(content=content)

def write_page_range(reader: PdfReader, start_page: int, end_page: int, output_path: PathInput) -> Path:
    writer = PdfWriter()
    for page in range(start_page, end_page + 1):
        writer.add_page(reader.pages[page - 1])

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as file:
        writer.write(file)
    return output_path


def validate_section_plan(plan: AppraisalSectionPlan, total_pages: int) -> None:
    pages = [
        plan.land_building_start_page,
        plan.land_building_end_page,
        plan.machinery_start_page,
        plan.machinery_end_page,
    ]
    invalid = [page for page in pages if page > total_pages]
    if invalid:
        raise ValueError(f"명세표 페이지가 PDF 범위를 초과했습니다: {invalid} / 전체 {total_pages}페이지")


class AppraisalSectionSplitter:
    def __init__(self, llm: Any):
        self.llm = llm
        self.section_parser = llm.with_structured_output(AppraisalSectionPlan, method="json_schema")

    def find_section_pages(self, source_path: PathInput) -> AppraisalSectionPlan:
        reader = PdfReader(str(source_path))
        message = build_pdf_with_contact_sheets_message(
            source_path,
            "첨부 감정평가서에서 토지·건물 감정평가명세표와 기계기구 감정평가명세표의 시작/끝 물리 PDF 페이지 번호만 반환하라. 이미지의 빨간 PDF PAGE 라벨을 페이지 번호로 사용하라.",
            end_page=min(len(reader.pages), 60),
        )
        return self.section_parser.invoke([SystemMessage(content=SECTION_PAGE_SYSTEM_PROMPT), message])

    def split(self, source_path: PathInput, output_root: PathInput) -> dict[str, Any]:
        source_path = Path(source_path)
        output_dir = Path(output_root) / source_path.stem
        reader = PdfReader(str(source_path))
        section_plan = self.find_section_pages(source_path)
        validate_section_plan(section_plan, len(reader.pages))

        land_building_pdf = write_page_range(
            reader,
            section_plan.land_building_start_page,
            section_plan.land_building_end_page,
            output_dir / "토지·건물 감정평가명세표.pdf",
        )
        machinery_pdf = write_page_range(
            reader,
            section_plan.machinery_start_page,
            section_plan.machinery_end_page,
            output_dir / "기계기구 감정평가명세표.pdf",
        )
        section_plan.save_json(output_dir / "section_plan.json")
        return {
            "section_plan": section_plan,
            "output_dir": output_dir,
            "land_building_pdf": land_building_pdf,
            "machinery_pdf": machinery_pdf,
        }


class AppraisalContentExtractor:
    def __init__(self, llm: Any):
        self.llm = llm
        self.land_building_extractor = llm.with_structured_output(LandBuildingExtraction, method="json_schema")
        self.machinery_extractor = llm.with_structured_output(MachineryExtraction, method="json_schema")

    def extract_land_building(self, source_path: PathInput) -> LandBuildingExtraction:
        message = build_pdf_with_page_images_message(
            source_path,
            "첨부 토지·건물 감정평가명세표에서 land와 building 배열을 README 규칙대로 추출하라. PDF 원본과 이어지는 고해상도 페이지 이미지를 모두 확인하라.",
        )
        return self.land_building_extractor.invoke([SystemMessage(content=LAND_BUILDING_SYSTEM_PROMPT), message])

    async def async_extract_land_building(self, source_path: PathInput) -> LandBuildingExtraction:
        message = build_pdf_with_page_images_message(
            source_path,
            "첨부 토지·건물 감정평가명세표에서 land와 building 배열을 README 규칙대로 추출하라. PDF 원본과 이어지는 고해상도 페이지 이미지를 모두 확인하라.",
        )
        return await self.land_building_extractor.ainvoke([SystemMessage(content=LAND_BUILDING_SYSTEM_PROMPT), message])

    def extract_machinery(self, source_path: PathInput) -> MachineryExtraction:
        message = build_pdf_with_page_images_message(
            source_path,
            "첨부 기계기구 감정평가명세표에서 기계기구 품목 배열을 README 규칙대로 추출하라. PDF 원본과 이어지는 고해상도 페이지 이미지를 모두 확인하라.",
        )
        return self.machinery_extractor.invoke([SystemMessage(content=MACHINERY_SYSTEM_PROMPT), message])

    async def async_extract_machinery(self, source_path: PathInput) -> MachineryExtraction:
        message = build_pdf_with_page_images_message(
            source_path,
            "첨부 기계기구 감정평가명세표에서 기계기구 품목 배열을 README 규칙대로 추출하라. PDF 원본과 이어지는 고해상도 페이지 이미지를 모두 확인하라.",
        )
        return await self.machinery_extractor.ainvoke([SystemMessage(content=MACHINERY_SYSTEM_PROMPT), message])

    def extract(self, land_building_pdf: PathInput, machinery_pdf: PathInput) -> AppraisalExtraction:
        land_building = self.extract_land_building(land_building_pdf)
        machinery = self.extract_machinery(machinery_pdf)
        return AppraisalExtraction(
            land=land_building.land,
            building=land_building.building,
            machinery=machinery.machinery,
        )

    async def async_extract(self, land_building_pdf: PathInput, machinery_pdf: PathInput) -> AppraisalExtraction:
        land_building, machinery = await asyncio.gather(
            self.async_extract_land_building(land_building_pdf),
            self.async_extract_machinery(machinery_pdf),
        )
        return AppraisalExtraction(
            land=land_building.land,
            building=land_building.building,
            machinery=machinery.machinery,
        )


class AppraisalParser:
    def __init__(self, *, section_llm: Any, extraction_llm: Any):
        self.splitter = AppraisalSectionSplitter(section_llm)
        self.extractor = AppraisalContentExtractor(extraction_llm)

    def invoke(self, source_path: PathInput, output_root: PathInput) -> AppraisalWorkflowResult:
        source_path = Path(source_path)
        output_root = Path(output_root)
        split = self.splitter.split(source_path, output_root)
        data = self.extractor.extract(split["land_building_pdf"], split["machinery_pdf"])
        json_path = output_root / f"{source_path.stem}.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(data.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return AppraisalWorkflowResult(
            source_path=str(source_path),
            split_dir=str(split["output_dir"]),
            land_building_pdf=str(split["land_building_pdf"]),
            machinery_pdf=str(split["machinery_pdf"]),
            json_path=str(json_path),
            section_plan=split["section_plan"],
            data=data,
        )

    async def async_invoke(self, source_path: PathInput, output_root: PathInput) -> AppraisalWorkflowResult:
        source_path = Path(source_path)
        output_root = Path(output_root)
        split = self.splitter.split(source_path, output_root)
        data = await self.extractor.async_extract(split["land_building_pdf"], split["machinery_pdf"])
        json_path = output_root / f"{source_path.stem}.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(data.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return AppraisalWorkflowResult(
            source_path=str(source_path),
            split_dir=str(split["output_dir"]),
            land_building_pdf=str(split["land_building_pdf"]),
            machinery_pdf=str(split["machinery_pdf"]),
            json_path=str(json_path),
            section_plan=split["section_plan"],
            data=data,
        )


def load_appraisal_json(path: PathInput) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
