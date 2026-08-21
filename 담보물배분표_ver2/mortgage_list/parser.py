"""공장저당법목록 PDF 2단계 LLM 분리·추출 프레임워크."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageDraw
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator, model_validator
from pypdf import PdfReader, PdfWriter

PathInput = str | Path


MORTGAGE_LIST_SPLIT_SYSTEM_PROMPT = """
너는 대한민국 공장 및 광업재단 저당법 제6조 목록 PDF의 문서 구조를 판정하는 회계법인 분석가다.

목표:
원본 PDF에서 각 `공장 및 광업재단 저당법 제6조 목록` 묶음의 물리 PDF 페이지 시작과 끝을 찾아 반환한다.

문서 구조:
- 앞부분에 부동산 등기사항전부증명서가 있을 수 있다. 이 부분은 목록 묶음이 아니므로 제외한다.
- 이후 `공장 및 광업재단 저당법 제6조 목록` 부분에서 서로 다른 목록번호 묶음이 반복된다.
- 하나의 묶음은 보통 `목록 변경내역표` → `목록 구분용 간지` → `제6조 목록 표지` → `기계기구목록` → 관련 부속 설명/확인 내용으로 이어진다.
- `목록번호`, `제 YYYY-N호`, 표지의 목록번호, 기계기구목록 상단의 목록번호를 교차 확인한다.

페이지 판정 규칙:
- 반환 페이지 번호는 문서에 인쇄된 번호가 아니라 PDF 뷰어 기준 물리 페이지 번호다. 첨부 이미지의 빨간 `PDF PAGE` 라벨을 기준으로 한다.
- 각 묶음의 start_page는 해당 목록번호의 변경내역표가 있으면 그 페이지, 없으면 그 목록번호가 처음 등장하는 간지/표지 페이지다.
- end_page는 다음 목록번호 묶음이 시작하기 직전 페이지다.
- 하나의 PDF 페이지에 이전 묶음 끝과 다음 묶음 시작이 함께 있으면 다음 묶음의 시작 페이지로도 포함한다. 이 경우 page range가 겹칠 수 있음을 허용한다.
- 목록번호가 같은 변경내역표, 간지, 표지, 기계기구목록은 반드시 같은 묶음으로 묶는다.
- PDF의 첫 물리 페이지 번호는 1이다.

제한:
- 기계기구 세부 항목은 추출하지 않는다. 페이지 범위와 목록번호만 반환한다.
- 존재하지 않는 목록번호를 만들지 않는다.
- 목록번호 문자열은 원문 의미를 보존하되 `공장 및 광업재단 저당법 제6조 목록 제YYYY-N호` 형태로 정규화한다.
- 반드시 지정된 출력 스키마만 따른다.
""".strip()


MORTGAGE_LIST_EXTRACT_SYSTEM_PROMPT = """
너는 대한민국 공장 및 광업재단 저당법 제6조 목록을 검토하는 회계법인 분석가다.
첨부된 단일 목록번호 묶음 PDF에서 해당 목록번호의 기계기구목록 본문만 구조화하라.

목표:
- 최상위 mortgage_list_no는 `공장 및 광업재단 저당법 제6조 목록 제YYYY-N호` 형식의 목록번호다.
- 해당 목록번호 아래 기계기구목록 본문에 있는 품목 행을 machinery_items 배열로 반환한다.
- 변경내역, 말소/삭제 이력, 접수 이력, 상태값은 별도 필드로 만들지 않는다.

추출 규칙:
- 목록 표지와 기계기구목록 본문을 읽어 목록번호와 품목 행을 확인한다.
- 목록 변경내역표는 목록번호 묶음 확인에만 참고하고, 변경/삭제/말소 내역을 구조화하지 않는다.
- 명확한 기계기구목록 본문 품목 행이 아닌 변경내역표 행, 접수/말소/삭제 설명, 안내문은 machinery_items에 넣지 않는다.
- 명칭, 규격, 제작자, 제작(취득)일자, 수량, 단위, 비고는 원문 열에 근거해 추출한다.
- 명칭/규격/제작자가 여러 줄이면 같은 행의 내용으로 합친다.
- list_serial_no는 저당목록 문서의 일련번호를 보존한다.
- appraisal_serial_no는 감정평가서 일련번호와 연결 단서가 문서에 명확할 때만 기록한다. 단서가 없으면 null이다.
- quantity는 숫자만이면 숫자로 반환하고, `1식`처럼 단위가 결합되어 있으면 quantity=1, unit="식"처럼 분리한다.
- 제작(취득)일자는 원문 형식의 연월/일자를 최대한 보존한다. 예: `2012.08`, `2012.8`, `2012.08.20`.
- 문서에 없는 금액, 단가, 평가액은 만들지 않는다.
- 읽을 수 없거나 문서에 없는 값은 추정하지 말고 null로 둔다.
- source_text에는 해당 품목을 판단한 원문 핵심 행을 짧게 보존한다.

제한:
- 다른 목록번호의 품목을 섞지 않는다.
- 합계, 이하 여백, 안내문, 확인 문구는 품목으로 만들지 않는다.
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


class MortgageListSlice(BaseModel):
    mortgage_list_no: str = Field(
        description="공장 및 광업재단 저당법 제6조 목록 제YYYY-N호 형식의 목록번호"
    )
    start_page: int = Field(description="묶음 시작 물리 PDF 페이지 번호(1-based)")
    end_page: int = Field(description="묶음 끝 물리 PDF 페이지 번호(1-based, 포함)")
    evidence: str = Field(default="", description="경계를 판정한 핵심 근거")

    @field_validator("start_page", "end_page")
    @classmethod
    def positive_page(cls, page: int) -> int:
        if page < 1:
            raise ValueError("PDF 페이지 번호는 1 이상이어야 합니다.")
        return page

    @model_validator(mode="after")
    def validate_range(self):
        if self.start_page > self.end_page:
            raise ValueError("목록 묶음 시작 페이지가 끝 페이지보다 큽니다.")
        return self


class MortgageListSplitPlan(JsonModel):
    source_document: str = Field(default="", description="원본 PDF 경로")
    slices: list[MortgageListSlice] = Field(
        min_length=1,
        description="목록번호별 PDF 분할 계획. 원본 등장 순서대로 반환한다.",
    )

    @model_validator(mode="after")
    def validate_slices(self):
        numbers = [item.mortgage_list_no for item in self.slices]
        if len(numbers) != len(set(numbers)):
            raise ValueError("목록번호가 중복되었습니다.")
        starts = [item.start_page for item in self.slices]
        if starts != sorted(starts):
            raise ValueError("목록 묶음은 원본 등장 순서대로 오름차순이어야 합니다.")
        return self


class MortgageMachineryItem(BaseModel):
    list_serial_no: str | None = Field(default=None, description="저당목록 문서의 일련번호")
    appraisal_serial_no: str | None = Field(default=None, description="명확한 연결 단서가 있을 때만 기록하는 감정평가서 일련번호")
    name: str | None = Field(default=None, description="기계기구 명칭")
    specification: str | None = Field(default=None, description="규격, 형식, 용량 등")
    manufacturer: str | None = Field(default=None, description="제작자")
    manufactured_date: str | None = Field(default=None, description="제작 또는 취득 일자")
    quantity: float | int | None = Field(default=None, description="수량 숫자")
    unit: str | None = Field(default=None, description="수량 단위")
    remarks: str | None = Field(default=None, description="비고")
    source_text: str | None = Field(default=None, description="근거 원문 핵심 행")


class MortgageListExtractionSummary(BaseModel):
    machinery_item_count: int = Field(default=0, description="추출된 기계기구 품목 수")
    warning_count: int = Field(default=0, description="warning 수")
    source_pages: list[int] = Field(default_factory=list, description="원본 PDF 기준 [시작, 끝] 물리 페이지 번호")


class MortgageListGroup(JsonModel):
    mortgage_list_no: str = Field(description="공장 및 광업재단 저당법 제6조 목록번호")
    machinery_items: list[MortgageMachineryItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_pdf: str | None = Field(default=None, description="이 목록번호 추출에 사용한 분할 PDF 경로")
    source_pages: list[int] = Field(default_factory=list, description="원본 PDF 기준 [시작, 끝] 물리 페이지 번호")
    extraction_summary: MortgageListExtractionSummary = Field(default_factory=MortgageListExtractionSummary, description="중간산출물 검토용 간단 보고")


class MortgageAssetsReport(JsonModel):
    schema_version: int = 1
    source_document: str
    mortgage_lists: list[MortgageListGroup] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict, description="목록 전체 추출 결과 간단 보고")
    warnings: list[str] = Field(default_factory=list, description="목록 전체 수준 검토 필요 사항")


class MortgageWorkflowResult(BaseModel):
    source_path: str
    split_dir: str
    split_plan_path: str
    json_path: str
    split_pdfs: list[str]
    data: MortgageAssetsReport


_NUMBER_TOKEN = re.compile(r"(\d{4})\s*-\s*(\d+)")
_SAFE_NAME = re.compile(r"[^0-9A-Za-z가-힣_.-]+")


def normalize_mortgage_list_no(value: str) -> str:
    match = _NUMBER_TOKEN.search(value)
    if not match:
        return value.strip()
    return f"공장 및 광업재단 저당법 제6조 목록 제{match.group(1)}-{match.group(2)}호"


def safe_file_stem(value: str) -> str:
    normalized = normalize_mortgage_list_no(value)
    token = _NUMBER_TOKEN.search(normalized)
    if token:
        return f"목록_{token.group(1)}-{token.group(2)}"
    return _SAFE_NAME.sub("_", normalized).strip("_")


def encode_pdf(path: PathInput) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("utf-8")


def build_pdf_message(path: PathInput, instruction: str, *, detail: str = "high") -> HumanMessage:
    path = Path(path)
    return HumanMessage(
        content=[
            {"type": "text", "text": instruction},
            {
                "type": "file",
                "base64": encode_pdf(path),
                "filename": path.name,
                "mime_type": "application/pdf",
                "detail": detail,
            },
        ]
    )


def render_numbered_contact_sheets(
    path: PathInput,
    *,
    pages_per_sheet: int = 4,
    zoom: float = 0.9,
) -> list[str]:
    document = fitz.open(str(path))
    rendered: list[tuple[int, Image.Image]] = []
    matrix = fitz.Matrix(zoom, zoom)
    for page_no, page in enumerate(document, start=1):
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
        columns = 2
        rows = 2
        sheet = Image.new("RGB", (max_width * columns, (max_height + label_height) * rows), "white")
        draw = ImageDraw.Draw(sheet)
        for index, (page_no, image) in enumerate(chunk):
            x = (index % columns) * max_width
            y = (index // columns) * (max_height + label_height)
            draw.text((x + 10, y + 8), f"PDF PAGE {page_no}", fill=(220, 0, 0))
            sheet.paste(image, (x, y + label_height))

        import io

        buffer = io.BytesIO()
        sheet.save(buffer, format="PNG")
        sheets.append(base64.b64encode(buffer.getvalue()).decode("utf-8"))
    return sheets


def build_split_plan_message(path: PathInput, instruction: str) -> HumanMessage:
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
        {
            "type": "text",
            "text": "아래 이미지는 원본 PDF 전체 물리 페이지의 contact sheet다. 빨간 PDF PAGE 라벨을 start_page/end_page로 사용하라.",
        },
    ]
    for index, image_base64 in enumerate(render_numbered_contact_sheets(path), start=1):
        content.append({"type": "text", "text": f"contact sheet {index}"})
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


def close_split_gaps(plan: MortgageListSplitPlan, total_pages: int) -> MortgageListSplitPlan:
    closed_slices: list[MortgageListSlice] = []
    for index, item in enumerate(plan.slices):
        expected_end = plan.slices[index + 1].start_page - 1 if index + 1 < len(plan.slices) else total_pages
        end_page = max(item.end_page, expected_end)
        evidence = item.evidence
        if end_page != item.end_page:
            evidence = (evidence + " ").strip() + f"다음 목록번호 시작 전까지 같은 묶음으로 보정: p{item.end_page}→p{end_page}."
        closed_slices.append(
            MortgageListSlice(
                mortgage_list_no=item.mortgage_list_no,
                start_page=item.start_page,
                end_page=end_page,
                evidence=evidence,
            )
        )
    return MortgageListSplitPlan(source_document=plan.source_document, slices=closed_slices)


def validate_split_plan(plan: MortgageListSplitPlan, total_pages: int) -> None:
    invalid = [item for item in plan.slices if item.end_page > total_pages]
    if invalid:
        labels = [f"{item.mortgage_list_no}: {item.start_page}-{item.end_page}" for item in invalid]
        raise ValueError(f"목록 묶음 페이지가 PDF 범위를 초과했습니다: {labels} / 전체 {total_pages}페이지")


class MortgageListSplitter:
    def __init__(self, llm: Any):
        self.llm = llm
        self.split_plan_parser = llm.with_structured_output(MortgageListSplitPlan, method="json_schema")

    def find_split_plan(self, source_path: PathInput) -> MortgageListSplitPlan:
        source_path = Path(source_path)
        message = build_split_plan_message(
            source_path,
            "첨부 공장저당법목록 PDF에서 목록번호별 묶음의 시작/끝 물리 PDF 페이지 번호와 목록번호만 반환하라.",
        )
        plan = self.split_plan_parser.invoke([SystemMessage(content=MORTGAGE_LIST_SPLIT_SYSTEM_PROMPT), message])
        plan.source_document = str(source_path)
        plan.slices = [
            MortgageListSlice(
                mortgage_list_no=normalize_mortgage_list_no(item.mortgage_list_no),
                start_page=item.start_page,
                end_page=item.end_page,
                evidence=item.evidence,
            )
            for item in plan.slices
        ]
        return MortgageListSplitPlan.model_validate(plan.model_dump())

    def split(self, source_path: PathInput, output_root: PathInput) -> dict[str, Any]:
        source_path = Path(source_path)
        output_dir = Path(output_root) / source_path.stem
        output_dir.mkdir(parents=True, exist_ok=True)
        for child in output_dir.iterdir():
            if child.is_file() and child.suffix.lower() in {".pdf", ".json"}:
                child.unlink()

        reader = PdfReader(str(source_path))
        plan = close_split_gaps(self.find_split_plan(source_path), len(reader.pages))
        validate_split_plan(plan, len(reader.pages))

        split_pdfs: list[Path] = []
        for index, item in enumerate(plan.slices, start=1):
            path = output_dir / f"{index:02d}_{safe_file_stem(item.mortgage_list_no)}_p{item.start_page}-{item.end_page}.pdf"
            split_pdfs.append(write_page_range(reader, item.start_page, item.end_page, path))

        split_plan_path = plan.save_json(output_dir / "split_plan.json")
        return {
            "output_dir": output_dir,
            "split_plan": plan,
            "split_plan_path": split_plan_path,
            "split_pdfs": split_pdfs,
        }


class MortgageListContentExtractor:
    def __init__(self, llm: Any):
        self.llm = llm
        self.extractor = llm.with_structured_output(MortgageListGroup, method="json_schema")

    def extract_group(self, source_path: PathInput, expected_list_no: str) -> MortgageListGroup:
        source_path = Path(source_path)
        message = build_pdf_message(
            source_path,
            f"첨부 단일 PDF 묶음에서 `{expected_list_no}`의 현재 유효 기계기구목록만 추출하라. PDF 원본을 detail=high로 확인하라.",
            detail="high",
        )
        result = self.extractor.invoke([SystemMessage(content=MORTGAGE_LIST_EXTRACT_SYSTEM_PROMPT), message])
        result.mortgage_list_no = normalize_mortgage_list_no(result.mortgage_list_no or expected_list_no)
        if result.mortgage_list_no != expected_list_no:
            result.warnings.append(f"분할 계획 목록번호({expected_list_no})와 추출 목록번호({result.mortgage_list_no})가 다릅니다.")
        return result

    async def async_extract_group(self, source_path: PathInput, expected_list_no: str) -> MortgageListGroup:
        source_path = Path(source_path)
        message = build_pdf_message(
            source_path,
            f"첨부 단일 PDF 묶음에서 `{expected_list_no}`의 현재 유효 기계기구목록만 추출하라. PDF 원본을 detail=high로 확인하라.",
            detail="high",
        )
        result = await self.extractor.ainvoke([SystemMessage(content=MORTGAGE_LIST_EXTRACT_SYSTEM_PROMPT), message])
        result.mortgage_list_no = normalize_mortgage_list_no(result.mortgage_list_no or expected_list_no)
        if result.mortgage_list_no != expected_list_no:
            result.warnings.append(f"분할 계획 목록번호({expected_list_no})와 추출 목록번호({result.mortgage_list_no})가 다릅니다.")
        return result

    async def async_extract_all(
        self,
        split_pdfs: list[Path],
        split_plan: MortgageListSplitPlan,
    ) -> list[MortgageListGroup]:
        tasks = [
            self.async_extract_group(pdf_path, item.mortgage_list_no)
            for pdf_path, item in zip(split_pdfs, split_plan.slices, strict=True)
        ]
        return await asyncio.gather(*tasks)


def build_assets_summary(groups: list[MortgageListGroup]) -> dict[str, Any]:
    item_count_by_list = {
        group.mortgage_list_no: len(group.machinery_items)
        for group in groups
    }
    warning_count_by_list = {
        group.mortgage_list_no: len(group.warnings)
        for group in groups
        if group.warnings
    }
    return {
        "mortgage_list_count": len(groups),
        "machinery_item_count": sum(item_count_by_list.values()),
        "item_count_by_list": item_count_by_list,
        "warning_count_by_list": warning_count_by_list,
    }


class MortgageParser:
    def __init__(self, llm: Any | None = None, *, section_llm: Any | None = None, extraction_llm: Any | None = None):
        section_llm = section_llm or llm
        extraction_llm = extraction_llm or llm
        if section_llm is None or extraction_llm is None:
            raise RuntimeError("MortgageParser는 gpt-5.4-mini 분할 LLM과 gpt-5.6-sol 추출 LLM이 필요합니다.")
        self.splitter = MortgageListSplitter(section_llm)
        self.extractor = MortgageListContentExtractor(extraction_llm)
        self.last_result: MortgageWorkflowResult | None = None

    async def async_invoke(self, source_path: PathInput, output_root: PathInput, *, json_path: PathInput | None = None) -> MortgageAssetsReport:
        source_path = Path(source_path)
        output_root = Path(output_root)
        split = self.splitter.split(source_path, output_root)
        groups = await self.extractor.async_extract_all(split["split_pdfs"], split["split_plan"])
        for group, pdf_path, slice_plan in zip(groups, split["split_pdfs"], split["split_plan"].slices, strict=True):
            group.source_pdf = str(pdf_path)
            group.source_pages = [slice_plan.start_page, slice_plan.end_page]
            group.extraction_summary = MortgageListExtractionSummary(
                machinery_item_count=len(group.machinery_items),
                warning_count=len(group.warnings),
                source_pages=group.source_pages,
            )
        data = MortgageAssetsReport(
            source_document=str(source_path),
            mortgage_lists=groups,
            summary=build_assets_summary(groups),
            warnings=[warning for group in groups for warning in group.warnings],
        )

        final_json_path = Path(json_path) if json_path else output_root / f"{source_path.stem}.json"
        final_json_path.parent.mkdir(parents=True, exist_ok=True)
        final_json_path.write_text(json.dumps(data.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")

        compatibility_path = split["output_dir"] / "mortgage_assets.json"
        if compatibility_path != final_json_path:
            compatibility_path.write_text(json.dumps(data.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")

        self.last_result = MortgageWorkflowResult(
            source_path=str(source_path),
            split_dir=str(split["output_dir"]),
            split_plan_path=str(split["split_plan_path"]),
            json_path=str(final_json_path),
            split_pdfs=[str(path) for path in split["split_pdfs"]],
            data=data,
        )
        return data

    def invoke(self, source_path: PathInput, output_root: PathInput, *, json_path: PathInput | None = None) -> MortgageAssetsReport:
        return asyncio.run(self.async_invoke(source_path, output_root, json_path=json_path))


async def run_mortgage_list_workflow(
    source_path: PathInput,
    output_root: PathInput,
    *,
    section_llm: Any,
    extraction_llm: Any,
    json_path: PathInput | None = None,
) -> MortgageWorkflowResult:
    parser = MortgageParser(section_llm=section_llm, extraction_llm=extraction_llm)
    await parser.async_invoke(source_path, output_root, json_path=json_path)
    if parser.last_result is None:
        raise RuntimeError("저당목록 워크플로우 결과가 생성되지 않았습니다.")
    return parser.last_result


def load_mortgage_assets_json(path: PathInput) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
