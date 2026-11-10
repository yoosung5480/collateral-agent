"""Document-specific low-cost LLM PDF format validators.

Public validators are intentionally separate:
- is_registry(path, llm)
- is_appraisal(path, llm)
- is_mortgaga_list(path, llm)

There is no non-LLM fallback.  Each public validator calls the supplied LLM once
for exactly one uploaded PDF and fails closed when the API call or validation
fails.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

PathInput = str | Path
DocumentType = Literal["registry", "mortgage_list", "appraisal"]
DetectedType = Literal["registry", "mortgage_list", "appraisal", "other", "unknown"]


class PdfValidationResult(BaseModel):
    """Structured validation result returned by a document-specific checker."""

    expected_type: DocumentType = Field(description="검증 대상 문서 유형")
    detected_type: DetectedType = Field(description="PDF에서 판정한 실제 문서 유형")
    is_valid: bool = Field(description="해당 문서별 형식 검사에 통과하면 true")
    confidence: Literal["high", "medium", "low"] = Field(description="판정 신뢰도")
    evidence: list[str] = Field(default_factory=list, description="원문에서 확인한 형식 단서")
    blocking_reason: str = Field(default="", description="is_valid=false인 경우 실행 중단 사유")

    @field_validator("evidence")
    @classmethod
    def require_evidence(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("PDF 형식 검증에는 원문 기반 evidence가 필요합니다.")
        return value


REGISTRY_VALIDATION_PROMPT = """
너는 '부동산 등기사항전부증명서/등기부등본' 형식만 검사하는 저비용 게이트다.

목표:
첨부 PDF가 담보물배분표 입력용 등기부등본인지 확인한다.

통과 단서:
- '등기사항전부증명서', '등기부등본', '말소사항 포함', '제출용' 같은 제목 또는 발급 문구
- '[표제부]', '[갑구]', '[을구]'
- '소유권 이외의 권리에 관한 사항', '근저당권', '채권최고액', '접수', '권리자 및 기타사항'
- 토지/건물의 표시 또는 부동산 고유번호/소재지번

실패 단서:
- 감정평가서 중심 문서
- 공장 및 광업재단 저당법 제6조 목록 중심 문서
- 계약서, 일반 보고서, 이미지 모음 등 등기부 형식이 아닌 문서

규칙:
- 파일명만 보지 말고 PDF 원문 단서만 근거로 한다.
- 상세 추출, 금액 계산, 페이지 분할은 하지 않는다.
- 등기부 일부가 선명하게 확인되고 본 분석에 투입 가능한 형식이면 is_valid=true.
- 확신이 낮으면 is_valid=false.
""".strip()


APPRAISAL_VALIDATION_PROMPT = """
너는 '감정평가서' 형식만 검사하는 저비용 게이트다.

목표:
첨부 PDF가 담보물배분표 입력용 감정평가서인지 확인한다.

통과 단서:
- 표지 또는 본문에 '감정평가서'
- '감정평가 요약', '감정평가액', '평가목적', '기준시점', '감정평가법인/감정평가사'
- '토지·건물 감정평가명세표' 또는 이에 준하는 토지/건물 명세표
- '기계기구 감정평가명세표' 또는 기계기구 평가 품목 표
- 소재지, 지번, 구조, 면적, 단가, 금액 등 평가 명세 컬럼

실패 단서:
- 등기사항전부증명서가 주 문서
- 공장저당법 제6조 목록이 주 문서
- 평가액/명세표 없이 일반 소개서 또는 계약서인 문서

규칙:
- 파일명만 보지 말고 PDF 원문 단서만 근거로 한다.
- 자산/금액을 추출하지 않는다. 형식 통과 여부만 판단한다.
- 담보물배분표 작업에 필요한 감정평가서 형식이면 is_valid=true.
- 확신이 낮으면 is_valid=false.
""".strip()


MORTGAGE_LIST_VALIDATION_PROMPT = """
너는 '공장 및 광업재단 저당법 제6조 목록' 형식만 검사하는 저비용 게이트다.

목표:
첨부 PDF가 담보물배분표 입력용 공장저당법목록 문서인지 확인한다.

통과 단서:
- '공장 및 광업재단 저당법 제6조 목록'
- '목록번호', '제YYYY-N호' 형식의 목록 번호
- '목록 변경내역표', '목록 구분용 간지', '기계기구목록'
- 기계기구 항목 표의 '명칭', '규격', '제작자', '제작(취득)일자', '수량', '비고'
- 앞부분에 등기부 일부가 섞여 있어도 제6조 목록 묶음이 명확하면 통과

실패 단서:
- 순수 등기사항전부증명서만 있고 제6조 목록이 없음
- 감정평가서가 주 문서
- 기계기구 목록번호/변경내역표가 없는 일반 문서

규칙:
- 파일명만 보지 말고 PDF 원문 단서만 근거로 한다.
- 기계기구 품목을 추출하지 않는다. 형식 통과 여부만 판단한다.
- 목록 묶음이 하나 이상 확인되면 is_valid=true.
- 확신이 낮으면 is_valid=false.
""".strip()


def _ensure_pdf(path: PathInput) -> Path:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"입력 PDF가 없습니다: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"입력 파일은 PDF여야 합니다: {path}")
    return path


def _encode_pdf(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def _single_pdf_message(path: Path, instruction: str) -> HumanMessage:
    return HumanMessage(
        content=[
            {"type": "text", "text": instruction},
            {
                "type": "file",
                "base64": _encode_pdf(path),
                "filename": path.name,
                "mime_type": "application/pdf",
                "detail": "low",
            },
        ]
    )


def _run_document_checker(
    *,
    path: PathInput,
    llm: Any,
    expected_type: DocumentType,
    system_prompt: str,
    instruction: str,
) -> PdfValidationResult:
    """Shared transport only; document judgment lives in per-document prompts.

    This helper is not a public generic classifier.  It only prevents duplicating
    LangChain invocation boilerplate while each public API keeps a dedicated
    prompt and a single-PDF call boundary.
    """

    pdf_path = _ensure_pdf(path)
    parser = llm.with_structured_output(PdfValidationResult, method="function_calling")
    result = parser.invoke([SystemMessage(content=system_prompt), _single_pdf_message(pdf_path, instruction)])
    result.expected_type = expected_type
    if result.is_valid:
        result.detected_type = expected_type
    if not result.is_valid or result.confidence == "low":
        reason = result.blocking_reason or f"{pdf_path.name}: expected={expected_type}, detected={result.detected_type}, confidence={result.confidence}"
        raise ValueError(f"PDF 입력 검증 실패: {reason}; evidence={result.evidence}")
    return result


def is_registry(registry_path: PathInput, llm: Any) -> PdfValidationResult:
    """Validate exactly one uploaded registry/deungibu PDF through a registry-only prompt."""

    return _run_document_checker(
        path=registry_path,
        llm=llm,
        expected_type="registry",
        system_prompt=REGISTRY_VALIDATION_PROMPT,
        instruction="첨부 PDF 1개가 등기부등본 형식인지 저비용으로 검사하라. 다른 문서 유형까지 분류하려 하지 말고 등기부 형식 통과 여부만 판단하라.",
    )


def is_appraisal(appraisal_path: PathInput, llm: Any) -> PdfValidationResult:
    """Validate exactly one uploaded appraisal PDF through an appraisal-only prompt."""

    return _run_document_checker(
        path=appraisal_path,
        llm=llm,
        expected_type="appraisal",
        system_prompt=APPRAISAL_VALIDATION_PROMPT,
        instruction="첨부 PDF 1개가 감정평가서 형식인지 저비용으로 검사하라. 다른 문서 유형까지 분류하려 하지 말고 감정평가서 형식 통과 여부만 판단하라.",
    )


def is_mortgaga_list(mortgaga_list_path: PathInput, llm: Any) -> PdfValidationResult:
    """Validate exactly one uploaded factory mortgage-list PDF.

    The misspelled function name is preserved because 시스템안정성.md specified
    this public API spelling.
    """

    return _run_document_checker(
        path=mortgaga_list_path,
        llm=llm,
        expected_type="mortgage_list",
        system_prompt=MORTGAGE_LIST_VALIDATION_PROMPT,
        instruction="첨부 PDF 1개가 공장 및 광업재단 저당법 제6조 목록 형식인지 저비용으로 검사하라. 다른 문서 유형까지 분류하려 하지 말고 저당목록 형식 통과 여부만 판단하라.",
    )


def is_mortgage_list(mortgage_list_path: PathInput, llm: Any) -> PdfValidationResult:
    """Correctly spelled alias for future callers."""

    return is_mortgaga_list(mortgage_list_path, llm)


def write_validation_report(results: dict[str, PdfValidationResult], output_path: PathInput) -> dict[str, Any]:
    report = {
        "status": "ok",
        "llm_required": True,
        "validation_mode": "document_specific_low_cost",
        "results": {name: result.model_dump() for name, result in results.items()},
    }
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def validate_required_inputs(
    *,
    registry_path: PathInput,
    appraisal_path: PathInput,
    mortgage_list_path: PathInput,
    llm: Any,
    output_path: PathInput | None = None,
) -> dict[str, Any]:
    """Batch-script convenience wrapper.

    Upload/API callers should call the single-document functions directly.  This
    wrapper exists only for `python main.py`, where all three hard-coded inputs
    are required for one batch run.
    """

    results = {
        "registry": is_registry(registry_path, llm),
        "appraisal": is_appraisal(appraisal_path, llm),
        "mortgage_list": is_mortgaga_list(mortgage_list_path, llm),
    }
    if output_path:
        return write_validation_report(results, output_path)
    return {
        "status": "ok",
        "llm_required": True,
        "validation_mode": "document_specific_low_cost",
        "results": {name: result.model_dump() for name, result in results.items()},
    }
