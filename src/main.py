"""담보물배분표 엑셀 자동 반환기.

README 요구에 맞춰 모델명과 입출력 경로를 코드 최상단 전역 상수로 고정한다.
실행: python main.py
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import fitz
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from appraisal.parser import AppraisalParser
from cost_tracking import CostTracker
from excel import write_collateral_workbook
from mortgage_list.parser import MortgageParser
from pdf_validater.validater import is_appraisal, is_mortgaga_list, is_registry, write_validation_report
from pipeline import (
    async_stage4_machinery_match,
    build_excel_format_plan,
    build_manifest,
    build_workbook_plan,
    load_json,
    stage1_merge_registry,
    stage2_real_estate,
    stage3_machinery_base,
    write_json,
)
from registry.parse_registry_info import RegistryParser
from registry.pdf_llm_parser import RawRegistryParser

# README/test.py 기반 실행 설정. 경로 상수는 실행 파일 위치를 기준으로 고정해
# 백업/복사본 디렉터리에서도 같은 실행 API(`python main.py`)를 유지한다.
PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
REGISTRY_PDF_PATH = PROJECT_ROOT / "datas" / "건물_동구 초량동 1149-3 제4호.pdf"
APPRAISAL_PDF_PATH = PROJECT_ROOT / "datas" / "1. 감정평가서_문화충전소_동구 초량동 1149-3.pdf"
MORTGAGE_LIST_PDF_PATH = PROJECT_ROOT / "datas" / "부산 동구 초량동 1149-3(목록포함).pdf"


REGISTRY_OUTPUT_ROOT = PROJECT_ROOT / "output" / "registry"
APPRAISAL_OUTPUT_ROOT = PROJECT_ROOT / "output" / "appraisal"
MORTGAGE_LIST_OUTPUT_ROOT = PROJECT_ROOT / "output" / "mortgage_list"
MORTGAGE_LIST_JSON_PATH = MORTGAGE_LIST_OUTPUT_ROOT / f"{MORTGAGE_LIST_PDF_PATH.stem}.json"
RUN_ID = re.sub(r"[^0-9A-Za-z가-힣]+", "_", APPRAISAL_PDF_PATH.stem).strip("_")
BORROWER_NAME = re.sub(r"^\d+\.\s*", "", APPRAISAL_PDF_PATH.stem).replace("감정평가서_", "").replace("감정평가서 ", "").strip()
FINAL_OUTPUT_DIR = PROJECT_ROOT / APPRAISAL_PDF_PATH.stem
FINAL_OUTPUT_PATH = FINAL_OUTPUT_DIR / "담보물배분표.xlsx"
PIPELINE_OUTPUT_ROOT = FINAL_OUTPUT_DIR / "pipeline_artifacts"
INPUT_VALIDATION_REPORT_PATH = PIPELINE_OUTPUT_ROOT / "00_input_validation" / "pdf_validation_report.json"
COST_TRACKING_LEDGER_DIR = PROJECT_ROOT / "cost_tracking_data"
COST_TRACKING_PRICING_PATH = PROJECT_ROOT / "cost_tracking" / "pricing.json"
MINI_MODEL_NAME = "gpt-5.4-mini"
SOL_MODEL_NAME = "gpt-5.6-sol"
FORMAT_MODEL_NAME = "gpt-5.4"
VALIDATION_MODEL_NAME = MINI_MODEL_NAME
TEMPERATURE = 0
OPENAI_MAX_RETRIES = 6
OPENAI_REQUEST_TIMEOUT_SECONDS = 300


def configure_pdf_renderer() -> None:
    """Keep malformed-but-readable PDFs from flooding stderr during rendering.

    Some client PDFs render correctly but contain broken ExtGState references.
    PyMuPDF reports those as MuPDF syntax errors for every page.  The parser only
    needs rendered page images, so we keep rendering enabled and suppress the
    non-fatal diagnostic stream globally.
    """

    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)


@contextmanager
def _null_stage() -> Iterator[None]:
    yield


def make_llms(cost_tracker: CostTracker | None = None) -> tuple[ChatOpenAI, ChatOpenAI, ChatOpenAI, ChatOpenAI, ChatOpenAI]:
    load_dotenv(ENV_PATH)
    api_key = os.getenv("OPENAI_API_KEY")
    validation_model_name = os.getenv("PDF_VALIDATION_MODEL_NAME", VALIDATION_MODEL_NAME)
    callbacks = [cost_tracker] if cost_tracker is not None else None
    max_retries = int(os.getenv("OPENAI_MAX_RETRIES", str(OPENAI_MAX_RETRIES)))
    request_timeout = float(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", str(OPENAI_REQUEST_TIMEOUT_SECONDS)))
    llm_kwargs = {
        "api_key": api_key,
        "temperature": TEMPERATURE,
        "callbacks": callbacks,
        "max_retries": max_retries,
        "timeout": request_timeout,
    }
    gpt_validation = ChatOpenAI(model=validation_model_name, **llm_kwargs)
    gpt_mini = ChatOpenAI(model=MINI_MODEL_NAME, **llm_kwargs)
    gpt_sol = ChatOpenAI(model=SOL_MODEL_NAME, **llm_kwargs)
    gpt_tool = ChatOpenAI(model=SOL_MODEL_NAME, reasoning_effort="none", **llm_kwargs)
    gpt_format = ChatOpenAI(model=FORMAT_MODEL_NAME, **llm_kwargs)
    return gpt_validation, gpt_mini, gpt_sol, gpt_tool, gpt_format


async def run_registry_workflow(
    gpt_mini: ChatOpenAI,
    gpt_sol: ChatOpenAI,
    cost_tracker: CostTracker | None = None,
) -> list[dict[str, Any]]:
    stage = cost_tracker.stage if cost_tracker is not None else None
    raw_registry_parser = RawRegistryParser(llm=gpt_mini)
    registry_parser = RegistryParser(llm=gpt_sol)
    with stage("01_registry.split_sections") if stage else _null_stage():
        split_results = await raw_registry_parser.async_split_registry_sections(REGISTRY_PDF_PATH, REGISTRY_OUTPUT_ROOT)
    registry_results: list[dict[str, Any]] = []

    async def extract_and_save(split: dict[str, Any]) -> dict[str, Any]:
        registry_dir = REGISTRY_OUTPUT_ROOT / split["registry"]
        registry_name = split.get("registry", "unknown")
        with stage(f"01_registry.extract.{registry_name}") if stage else _null_stage():
            title_result, encumbrance_result = await asyncio.gather(
                registry_parser.async_extract_title(source_path=split["title_path"]),
                registry_parser.async_extract_encumbrance(source_path=split["encumbrance_path"]),
            )
        title_json = title_result.save_json(registry_dir / "title.json")
        encumbrance_json = encumbrance_result.save_json(registry_dir / "encumbrance.json")
        return {**split, "title_json": str(title_json), "encumbrance_json": str(encumbrance_json)}

    registry_results.extend(await asyncio.gather(*(extract_and_save(split) for split in split_results)))
    return registry_results


async def run_appraisal_workflow(
    gpt_mini: ChatOpenAI,
    gpt_sol: ChatOpenAI,
    cost_tracker: CostTracker | None = None,
) -> Path:
    parser = AppraisalParser(section_llm=gpt_mini, extraction_llm=gpt_sol)
    with cost_tracker.stage("01_appraisal.workflow") if cost_tracker is not None else _null_stage():
        result = await parser.async_invoke(APPRAISAL_PDF_PATH, APPRAISAL_OUTPUT_ROOT)
    return Path(result.json_path)


async def run_mortgage_workflow(
    gpt_mini: ChatOpenAI,
    gpt_sol: ChatOpenAI,
    cost_tracker: CostTracker | None = None,
) -> Path:
    parser = MortgageParser(section_llm=gpt_mini, extraction_llm=gpt_sol)
    with cost_tracker.stage("01_mortgage_list.workflow") if cost_tracker is not None else _null_stage():
        await parser.async_invoke(MORTGAGE_LIST_PDF_PATH, MORTGAGE_LIST_OUTPUT_ROOT, json_path=MORTGAGE_LIST_JSON_PATH)
    return MORTGAGE_LIST_JSON_PATH


async def main_async() -> None:
    started = time.perf_counter()
    run_session_id = f"{RUN_ID}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    cost_tracker = CostTracker(
        run_id=run_session_id,
        artifact_dir=PIPELINE_OUTPUT_ROOT / "99_cost_tracking" / run_session_id,
        ledger_dir=COST_TRACKING_LEDGER_DIR,
        pricing_path=COST_TRACKING_PRICING_PATH,
    )
    run_status = "failed"
    run_error: str | None = None

    print("# =================================================== #")
    print("담보물배분표 엑셀 자동 반환기 시작")
    try:
        configure_pdf_renderer()
        gpt_validation, gpt_mini, gpt_sol, gpt_tool, gpt_format = make_llms(cost_tracker)

        print("0. 입력 PDF 문서별 저비용 LLM 형식 검증")
        with cost_tracker.stage("00_input_validation.registry"):
            registry_validation = is_registry(REGISTRY_PDF_PATH, gpt_validation)
        with cost_tracker.stage("00_input_validation.appraisal"):
            appraisal_validation = is_appraisal(APPRAISAL_PDF_PATH, gpt_validation)
        with cost_tracker.stage("00_input_validation.mortgage_list"):
            mortgage_validation = is_mortgaga_list(MORTGAGE_LIST_PDF_PATH, gpt_validation)
        validation_results = {
            "registry": registry_validation,
            "appraisal": appraisal_validation,
            "mortgage_list": mortgage_validation,
        }
        validation_report = write_validation_report(
            validation_results,
            INPUT_VALIDATION_REPORT_PATH,
        )
        print(f"   입력 검증 완료: {validation_report['status']} ({INPUT_VALIDATION_REPORT_PATH})")

        print("1. 등기부등본/감정평가서/공장저당법목록 PDF LLM 병렬 분석")
        registry_task = run_registry_workflow(gpt_mini, gpt_sol, cost_tracker)
        appraisal_task = run_appraisal_workflow(gpt_mini, gpt_sol, cost_tracker)
        mortgage_task = run_mortgage_workflow(gpt_mini, gpt_sol, cost_tracker)
        registry_results, appraisal_json_path, mortgage_json_path = await asyncio.gather(
            registry_task,
            appraisal_task,
            mortgage_task,
        )
        print(f"   등기부 JSON {len(registry_results)}건 생성")
        print(f"   감정평가 JSON 생성: {appraisal_json_path}")
        print(f"   저당목록 JSON 생성: {mortgage_json_path}")

        print("2. 명시 manifest 및 단계별 산출물 생성")
        manifest = build_manifest(
            run_id=RUN_ID,
            borrower_name=BORROWER_NAME,
            registry_pairs=registry_results,
            appraisal_json_path=appraisal_json_path,
            mortgage_json_path=mortgage_json_path,
            output_path=FINAL_OUTPUT_PATH,
        )
        manifest_path = write_json(PIPELINE_OUTPUT_ROOT / "run_manifest.json", manifest)
        stage1 = stage1_merge_registry(manifest, PIPELINE_OUTPUT_ROOT / "01_registry_merge")
        with cost_tracker.stage("02_pipeline.real_estate"):
            stage2 = stage2_real_estate(
                llm=gpt_tool,
                manifest=manifest,
                merged_title_path=stage1["merged_title"],
                merged_encumbrance_path=stage1["merged_encumbrance"],
                stage_dir=PIPELINE_OUTPUT_ROOT / "02_real_estate",
            )
        stage3 = stage3_machinery_base(
            manifest=manifest,
            merged_title_path=stage1["merged_title"],
            merged_encumbrance_path=stage1["merged_encumbrance"],
            stage_dir=PIPELINE_OUTPUT_ROOT / "03_machinery_base",
        )
        with cost_tracker.stage("02_pipeline.machinery_match"):
            stage4 = await async_stage4_machinery_match(
                llm=gpt_tool,
                manifest=manifest,
                machinery_base_path=stage3["machinery_distribution_base"],
                stage_dir=PIPELINE_OUTPUT_ROOT / "04_machinery_match",
            )
        excel_stage = build_workbook_plan(
            manifest=manifest,
            real_estate_plan_path=stage2["real_estate_table_plan"],
            machinery_plan_path=stage4["machinery_table_plan"],
            output_dir=PIPELINE_OUTPUT_ROOT / "05_excel",
        )

        print("3. gpt-5.4 내부 서식 규칙 기반 Excel 개선 노드 실행 및 Excel 작성")
        with cost_tracker.stage("03_excel.format_plan"):
            format_stage = build_excel_format_plan(
                llm=gpt_format,
                workbook_plan_path=excel_stage["workbook_plan"],
                output_dir=PIPELINE_OUTPUT_ROOT / "06_excel_format",
            )
        workbook_plan = load_json(excel_stage["workbook_plan"])
        format_plan = load_json(format_stage["excel_format_plan"])
        output_path = write_collateral_workbook(workbook_plan, FINAL_OUTPUT_PATH, format_plan=format_plan)
        elapsed = time.perf_counter() - started
        run_status = "success"
        print(f"완료: {output_path}")
        print(f"manifest: {manifest_path}")
        print(f"excel_format_plan: {format_stage['excel_format_plan']}")
        print(f"실행 시간: {elapsed:.3f}초")
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        cost_summary_path = cost_tracker.finalize(status=run_status, error=run_error)
        cost_summary = load_json(cost_summary_path)
        print(f"cost_usd: {float(cost_summary.get('estimated_total_cost_usd') or 0.0):.6f}")
        print(f"elapsed_seconds: {float(cost_summary.get('elapsed_seconds') or 0.0):.3f}")
        print(f"cost_summary: {cost_summary_path}")
        print(f"cost_ledger: {COST_TRACKING_LEDGER_DIR / 'run_summaries.jsonl'}")
        print("# =================================================== #")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
