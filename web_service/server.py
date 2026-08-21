"""Web service for 담보물배분표_ver2.

This server keeps the protected Python framework API surface intact, accepts the
three required PDFs, runs the existing framework functions against uploaded job
files, exposes user/developer dashboards, and delegates report/chat RAG to the
standalone ``rag_chat`` package.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sqlite3
import sys
import threading
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import fitz
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import BaseModel, ConfigDict, Field

from rag_chat import DEFAULT_THREAD_ID, RagChatManager

ROOT_DIR = Path(__file__).resolve().parents[1]
FRAMEWORK_ROOT = ROOT_DIR / "담보물배분표_ver2"
ENV_PATH = FRAMEWORK_ROOT / ".env"
RUNTIME_ROOT = Path(__file__).resolve().parent / "runtime" / "jobs"
DATABASE_PATH = Path(os.getenv("LOCAL_DATABASE_PATH", str(Path(__file__).resolve().parent / "runtime" / "app.db")))
STATIC_ROOT = Path(__file__).resolve().parent / "static"
KST = timezone(timedelta(hours=9))

MINI_MODEL_NAME = "gpt-5.6-luna"                        # "gpt-5.6-luna"
SOL_MODEL_NAME = "gpt-5.6-sol"                          # "gpt-5.6-sol"
FORMAT_MODEL_NAME = "gpt-5.6-terra"                     # "gpt-5.4"
EMBEDDING_MODEL_NAME = "text-embedding-3-small"
TEMPERATURE = 0
OPENAI_MAX_RETRIES = 3
OPENAI_REQUEST_TIMEOUT_SECONDS = 300

if str(FRAMEWORK_ROOT) not in sys.path:
    sys.path.insert(0, str(FRAMEWORK_ROOT))

from appraisal.parser import AppraisalParser  # noqa: E402
from cost_tracking import CostTracker  # noqa: E402
from excel import write_collateral_workbook  # noqa: E402
from mortgage_list.parser import MortgageParser  # noqa: E402
from pdf_validater.validater import is_appraisal, is_mortgaga_list, is_registry, write_validation_report  # noqa: E402
from pipeline import (  # noqa: E402
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
from registry.parse_registry_info import RegistryParser  # noqa: E402
from registry.pdf_llm_parser import RawRegistryParser  # noqa: E402


class UploadedPdf(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    filename: str = Field(min_length=1)
    content_base64: str = Field(min_length=1, alias="contentBase64")


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    registry: UploadedPdf
    appraisal: UploadedPdf
    mortgage_list: UploadedPdf = Field(alias="mortgageList")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


JobStatus = Literal["queued", "running", "completed", "failed"]
StepStatus = Literal["pending", "running", "completed", "failed"]


@dataclass
class StepState:
    id: str
    label: str
    client_label: str
    status: StepStatus = "pending"
    detail: str = "대기 중"
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class JobState:
    job_id: str
    status: JobStatus = "queued"
    current_step: str = "queued"
    created_at: str = field(default_factory=lambda: iso_now())
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    progress: int = 0
    input_files: dict[str, str] = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)
    steps: list[StepState] = field(default_factory=list)
    cost_summary: dict[str, Any] | None = None
    rag_cost_summary: dict[str, Any] | None = None
    rag_ready: bool = False
    rag_chunk_count: int = 0
    initial_report: str | None = None
    chat_history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RagSession:
    manager: RagChatManager
    cost_tracker: CostTracker


STEP_DEFINITIONS = [
    ("upload", "업로드 파일 저장", "문서 업로드 확인"),
    ("validation", "입력 PDF 형식 검사", "문서 형식 확인"),
    ("parse", "3종 PDF 병렬 문서 인식", "문서 내용 인식"),
    ("manifest", "실행 manifest 생성", "분석 기준 확정"),
    ("merge", "등기부 담보설정 병합", "등기 정보 병합"),
    ("realEstate", "부동산 가치평가 대조", "부동산 평가 대조"),
    ("machineryBase", "기계기구 담보 그룹 구성", "기계기구 기준 구성"),
    ("machineryMatch", "기계기구 저당목록 대조", "기계기구 목록 대조"),
    ("workbookPlan", "Excel 작성 계획 검증", "엑셀 작성 계획"),
    ("format", "Excel 서식 계획 생성", "엑셀 서식 최적화"),
    ("excel", "최종 Excel 생성", "최종 파일 생성"),
    ("report", "결과 리포트 작성", "결과 리포트 작성"),
    ("cost", "비용/토큰 내역 저장", "작업 내역 정리"),
]

JOBS: dict[str, JobState] = {}
JOBS_LOCK = threading.Lock()
RAG_SESSIONS: dict[str, RagSession] = {}
RAG_LOCK = threading.Lock()

app = FastAPI(title="담보물배분표 클라이언트 웹서비스", version="3.0.0")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def display_time(iso_value: str | None) -> str | None:
    if iso_value is None:
        return None
    return datetime.fromisoformat(iso_value).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")


def make_steps() -> list[StepState]:
    return [StepState(id=step_id, label=label, client_label=client_label) for step_id, label, client_label in STEP_DEFINITIONS]


def get_api_key() -> str:
    load_dotenv(ENV_PATH)
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("서비스 설정에 API 키가 없습니다.")
    return api_key


def make_llms(cost_tracker: CostTracker | None = None) -> tuple[ChatOpenAI, ChatOpenAI, ChatOpenAI, ChatOpenAI, ChatOpenAI]:
    api_key = get_api_key()
    callbacks = [cost_tracker] if cost_tracker is not None else None
    max_retries = int(os.getenv("OPENAI_MAX_RETRIES", str(OPENAI_MAX_RETRIES)))
    timeout = float(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", str(OPENAI_REQUEST_TIMEOUT_SECONDS)))
    common = {
        "api_key": api_key,
        "temperature": TEMPERATURE,
        "callbacks": callbacks,
        "max_retries": max_retries,
        "timeout": timeout,
    }
    gpt_validation = ChatOpenAI(model=MINI_MODEL_NAME, reasoning_effort="none", **common)
    gpt_mini = ChatOpenAI(model=MINI_MODEL_NAME, reasoning_effort="none", **common)
    gpt_sol = ChatOpenAI(model=SOL_MODEL_NAME, **common)
    gpt_tool = ChatOpenAI(model=MINI_MODEL_NAME, reasoning_effort="none", **common)
    gpt_terra = ChatOpenAI(model=FORMAT_MODEL_NAME, reasoning_effort="none", **common)
    return gpt_validation, gpt_mini, gpt_sol, gpt_tool, gpt_terra


def make_chat_llm(cost_tracker: CostTracker | None = None) -> ChatOpenAI:
    return ChatOpenAI(
        model=MINI_MODEL_NAME,
        api_key=get_api_key(),
        temperature=0,
        callbacks=[cost_tracker] if cost_tracker is not None else None,
        max_retries=int(os.getenv("OPENAI_MAX_RETRIES", str(OPENAI_MAX_RETRIES))),
        timeout=float(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", str(OPENAI_REQUEST_TIMEOUT_SECONDS))),
    )


def make_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=EMBEDDING_MODEL_NAME,
        api_key=get_api_key(),
        max_retries=int(os.getenv("OPENAI_MAX_RETRIES", str(OPENAI_MAX_RETRIES))),
        timeout=float(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", str(OPENAI_REQUEST_TIMEOUT_SECONDS))),
    )


def configure_pdf_renderer() -> None:
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def windows_safe_text(value: str, *, default: str = "file") -> str:
    normalized = unicodedata.normalize("NFC", str(value or default))
    without_controls = re.sub(r"[\x00-\x1f]", "_", normalized)
    safe = re.sub(r'[<>:"/\\|?*]+', "_", without_controls).strip(" ._")
    safe = re.sub(r"_+", "_", safe) or default
    if safe.upper() in WINDOWS_RESERVED_NAMES:
        safe = f"_{safe}"
    return safe[:180]


def sanitize_filename(filename: str, default: str) -> str:
    normalized = unicodedata.normalize("NFC", str(filename or default))
    name = windows_safe_text(normalized, default=default)
    stem = windows_safe_text(Path(name).stem or Path(default).stem, default=Path(default).stem)
    suffix = Path(name).suffix.lower() or Path(default).suffix.lower() or ".pdf"
    suffix = "." + re.sub(r"[^0-9a-z]+", "", suffix.lstrip("."))[:12] if suffix else ".pdf"
    return stem + suffix


def decode_pdf(payload: UploadedPdf) -> bytes:
    raw = base64.b64decode(payload.content_base64, validate=True)
    if not raw.startswith(b"%PDF"):
        raise ValueError(f"{payload.filename} 파일은 PDF 형식이 아닙니다.")
    return raw


def paths_for_job(job_id: str) -> dict[str, str]:
    root = RUNTIME_ROOT / job_id
    return {
        "jobRoot": str(root),
        "uploadDir": str(root / "uploads"),
        "intermediateDir": str(root / "framework_output"),
        "pipelineArtifactsDir": str(root / "pipeline_artifacts"),
        "ragDir": str(root / "rag"),
        "finalOutputPath": str(root / "final" / "담보물배분표.xlsx"),
    }


def start_step(job_id: str, step_id: str, detail: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.status = "running"
        job.current_step = step_id
        if job.started_at is None:
            job.started_at = iso_now()
        for step in job.steps:
            if step.id == step_id:
                step.status = "running"
                step.detail = detail
                step.started_at = iso_now()
                step.finished_at = None
                break
        completed = sum(step.status == "completed" for step in job.steps)
        job.progress = int(completed / len(job.steps) * 100)
        _write_job_snapshot(job)


def finish_step(job_id: str, step_id: str, detail: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        for step in job.steps:
            if step.id == step_id:
                step.status = "completed"
                step.detail = detail
                step.finished_at = iso_now()
                break
        completed = sum(step.status == "completed" for step in job.steps)
        job.progress = int(completed / len(job.steps) * 100)
        _write_job_snapshot(job)


def fail_job(job_id: str, step_id: str, error: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.status = "failed"
        job.error = error
        job.finished_at = iso_now()
        for step in job.steps:
            if step.id == step_id:
                step.status = "failed"
                step.detail = "처리 중 오류가 발생했습니다. 담당자 확인이 필요합니다."
                step.finished_at = iso_now()
                break
        _write_job_snapshot(job)


def elapsed_seconds(job: JobState) -> float | None:
    if job.started_at is None:
        return None
    start = datetime.fromisoformat(job.started_at)
    end = datetime.fromisoformat(job.finished_at) if job.finished_at else datetime.now(timezone.utc)
    return round((end - start).total_seconds(), 3)


def compact_cost_summary(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not summary:
        return None
    totals = summary.get("totals") or {}
    return {
        "status": summary.get("status"),
        "estimatedTotalCostUsd": summary.get("estimated_total_cost_usd"),
        "llmCallCount": summary.get("llm_call_count"),
        "llmErrorCount": summary.get("llm_error_count"),
        "inputTokens": totals.get("input_tokens"),
        "outputTokens": totals.get("output_tokens"),
        "totalTokens": totals.get("total_tokens"),
        "elapsedSeconds": summary.get("elapsed_seconds"),
        "byStage": summary.get("by_stage") or {},
        "byModel": summary.get("by_model") or {},
    }


def serialize_job(job: JobState, *, include_cost: bool = True) -> dict[str, Any]:
    payload = {
        "jobId": job.job_id,
        "status": job.status,
        "progress": 100 if job.status == "completed" else job.progress,
        "currentStep": job.current_step,
        "createdAt": job.created_at,
        "requestTime": display_time(job.created_at),
        "startedAt": job.started_at,
        "finishedAt": job.finished_at,
        "elapsedSeconds": elapsed_seconds(job),
        "error": job.error,
        "inputFiles": job.input_files,
        "paths": job.paths,
        "ragReady": job.rag_ready,
        "ragChunkCount": job.rag_chunk_count,
        "initialReport": job.initial_report,
        "initialReportPath": job.paths.get("initialReportPath"),
        "chatHistory": job.chat_history,
        "downloadUrl": f"/api/jobs/{job.job_id}/download" if job.status == "completed" else None,
        "steps": [
            {
                "id": step.id,
                "label": step.client_label,
                "status": step.status,
                "detail": step.detail,
                "startedAt": step.started_at,
                "finishedAt": step.finished_at,
            }
            for step in job.steps
        ],
    }
    if include_cost:
        payload["costSummary"] = compact_cost_summary(job.cost_summary)
        payload["ragCostSummary"] = job.rag_cost_summary
    return payload


def _write_job_snapshot(job: JobState) -> None:
    """Persist one complete job snapshot in the local SQLite database."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    snapshot = json.dumps(
        {
            "job_id": job.job_id,
            "status": job.status,
            "current_step": job.current_step,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "error": job.error,
            "progress": job.progress,
            "input_files": job.input_files,
            "paths": job.paths,
            "steps": [step.__dict__ for step in job.steps],
            "cost_summary": job.cost_summary,
            "rag_cost_summary": job.rag_cost_summary,
            "rag_ready": job.rag_ready,
            "rag_chunk_count": job.rag_chunk_count,
            "initial_report": job.initial_report,
            "chat_history": job.chat_history,
        },
        ensure_ascii=False,
    )
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO jobs (job_id, created_at, updated_at, payload)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                created_at = excluded.created_at,
                updated_at = CURRENT_TIMESTAMP,
                payload = excluded.payload
            """,
            (job.job_id, job.created_at, snapshot),
        )


def persist_job(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            _write_job_snapshot(job)


def initialize_local_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        connection.execute("PRAGMA journal_mode=WAL")


def restore_jobs_from_database() -> None:
    initialize_local_database()
    with sqlite3.connect(DATABASE_PATH) as connection:
        rows = connection.execute("SELECT payload FROM jobs ORDER BY created_at").fetchall()
    for (raw_payload,) in rows:
        payload = json.loads(raw_payload)
        steps = [StepState(**step) for step in payload.get("steps", [])]
        job = JobState(
            job_id=payload["job_id"],
            status=payload.get("status", "queued"),
            current_step=payload.get("current_step", "queued"),
            created_at=payload.get("created_at", iso_now()),
            started_at=payload.get("started_at"),
            finished_at=payload.get("finished_at"),
            error=payload.get("error"),
            progress=payload.get("progress", 0),
            input_files=payload.get("input_files", {}),
            paths=payload.get("paths", {}),
            steps=steps,
            cost_summary=payload.get("cost_summary"),
            rag_cost_summary=payload.get("rag_cost_summary"),
            rag_ready=payload.get("rag_ready", False),
            rag_chunk_count=payload.get("rag_chunk_count", 0),
            initial_report=payload.get("initial_report"),
            chat_history=payload.get("chat_history", []),
        )
        if job.status in {"queued", "running"}:
            job.status = "failed"
            job.error = "서버 재시작으로 중단된 작업입니다. 저장된 입력 파일로 다시 실행해 주세요."
            job.finished_at = iso_now()
        JOBS[job.job_id] = job
        if job.status == "failed" and job.error and "서버 재시작" in job.error:
            _write_job_snapshot(job)


restore_jobs_from_database()


def save_uploads(job_id: str, request: CreateJobRequest) -> dict[str, Path]:
    upload_dir = Path(paths_for_job(job_id)["uploadDir"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    mapping = {
        "registry": (request.registry, "등기부등본.pdf"),
        "appraisal": (request.appraisal, "감정평가서.pdf"),
        "mortgage_list": (request.mortgage_list, "저당목록.pdf"),
    }
    saved: dict[str, Path] = {}
    for key, (payload, default_name) in mapping.items():
        destination = upload_dir / sanitize_filename(payload.filename, default_name)
        destination.write_bytes(decode_pdf(payload))
        saved[key] = destination
    with JOBS_LOCK:
        JOBS[job_id].input_files = {key: path.name for key, path in saved.items()}
        _write_job_snapshot(JOBS[job_id])
    return saved


async def validate_inputs_async(
    *,
    uploaded: dict[str, Path],
    gpt_validation: ChatOpenAI,
    cost_tracker: CostTracker,
    validation_report_path: Path,
) -> dict[str, Any]:
    async def validate_one(stage_name: str, result_key: str, validator: Any, path: Path) -> tuple[str, Any]:
        def run() -> Any:
            with cost_tracker.stage(stage_name):
                return validator(path, gpt_validation)

        return result_key, await asyncio.to_thread(run)

    pairs = await asyncio.gather(
        validate_one("00_input_validation.registry", "registry", is_registry, uploaded["registry"]),
        validate_one("00_input_validation.appraisal", "appraisal", is_appraisal, uploaded["appraisal"]),
        validate_one("00_input_validation.mortgage_list", "mortgage_list", is_mortgaga_list, uploaded["mortgage_list"]),
        return_exceptions=True,
    )
    failures = [item for item in pairs if isinstance(item, Exception)]
    if failures:
        write_json(
            validation_report_path,
            {
                "status": "failed",
                "llm_required": True,
                "validation_mode": "document_specific_low_cost_parallel",
                "errors": [f"{type(error).__name__}: {error}" for error in failures],
                "finished_at_utc": iso_now(),
            },
        )
        raise ValueError("; ".join(f"{type(error).__name__}: {error}" for error in failures))
    results = dict(pairs)
    write_validation_report(results, validation_report_path)
    return results


async def run_registry_workflow(
    *,
    registry_pdf_path: Path,
    output_root: Path,
    gpt_mini: ChatOpenAI,
    gpt_sol: ChatOpenAI,
    cost_tracker: CostTracker,
) -> list[dict[str, Any]]:
    raw_registry_parser = RawRegistryParser(llm=gpt_mini)
    registry_parser = RegistryParser(llm=gpt_sol)
    with cost_tracker.stage("01_registry.split_sections"):
        split_results = await raw_registry_parser.async_split_registry_sections(registry_pdf_path, output_root)

    async def extract_one(split: dict[str, Any]) -> dict[str, Any]:
        registry_dir = output_root / split["registry"]
        registry_name = split.get("registry", "unknown")
        with cost_tracker.stage(f"01_registry.extract.{registry_name}"):
            title_result, encumbrance_result = await asyncio.gather(
                registry_parser.async_extract_title(source_path=split["title_path"]),
                registry_parser.async_extract_encumbrance(source_path=split["encumbrance_path"]),
            )
        title_json = title_result.save_json(registry_dir / "title.json")
        encumbrance_json = encumbrance_result.save_json(registry_dir / "encumbrance.json")
        return {**split, "title_json": str(title_json), "encumbrance_json": str(encumbrance_json)}

    return list(await asyncio.gather(*(extract_one(split) for split in split_results)))


async def run_appraisal_workflow(
    *,
    appraisal_pdf_path: Path,
    output_root: Path,
    gpt_mini: ChatOpenAI,
    gpt_sol: ChatOpenAI,
    cost_tracker: CostTracker,
) -> Path:
    parser = AppraisalParser(section_llm=gpt_mini, extraction_llm=gpt_sol)
    with cost_tracker.stage("01_appraisal.workflow"):
        result = await parser.async_invoke(appraisal_pdf_path, output_root)
    return Path(result.json_path)


async def run_mortgage_workflow(
    *,
    mortgage_pdf_path: Path,
    output_root: Path,
    json_path: Path,
    gpt_mini: ChatOpenAI,
    gpt_sol: ChatOpenAI,
    cost_tracker: CostTracker,
) -> Path:
    parser = MortgageParser(section_llm=gpt_mini, extraction_llm=gpt_sol)
    with cost_tracker.stage("01_mortgage_list.workflow"):
        await parser.async_invoke(mortgage_pdf_path, output_root, json_path=json_path)
    return json_path


async def run_framework_job_async(job_id: str, request: CreateJobRequest) -> None:
    active_step = "upload"
    configure_pdf_renderer()
    job_paths = paths_for_job(job_id)
    output_root = Path(job_paths["intermediateDir"])
    pipeline_output_root = Path(job_paths["pipelineArtifactsDir"])
    final_output_path = Path(job_paths["finalOutputPath"])
    ledger_dir = Path(job_paths["jobRoot"]) / "cost_tracking_data"
    pricing_path = FRAMEWORK_ROOT / "cost_tracking" / "pricing.json"
    run_session_id = f"{job_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    cost_tracker = CostTracker(
        run_id=run_session_id,
        artifact_dir=pipeline_output_root / "99_cost_tracking" / run_session_id,
        ledger_dir=ledger_dir,
        pricing_path=pricing_path,
    )
    run_status = "failed"
    run_error: str | None = None
    try:
        start_step(job_id, "upload", "업로드 문서 3종을 안전하게 저장하고 있습니다.")
        uploaded = save_uploads(job_id, request)
        finish_step(job_id, "upload", "문서 3종 업로드가 완료되었습니다.")

        active_step = "validation"
        start_step(job_id, active_step, "세 PDF 문서 형식을 동시에 확인하고 있습니다.")
        gpt_validation, gpt_mini, gpt_sol, gpt_tool, gpt_terra = make_llms(cost_tracker)
        # validation_report_path = pipeline_output_root / "00_input_validation" / "pdf_validation_report.json"
        # await validate_inputs_async(
        #     uploaded=uploaded,
        #     gpt_validation=gpt_validation,
        #     cost_tracker=cost_tracker,
        #     validation_report_path=validation_report_path,
        # )
        # finish_step(job_id, active_step, "문서 형식 확인이 완료되었습니다.")

        active_step = "parse"
        start_step(job_id, active_step, "등기부등본·감정평가서·저당목록을 동시에 인식하고 있습니다.")
        mortgage_json_path = output_root / "mortgage_list" / f"{uploaded['mortgage_list'].stem}.json"
        registry_results, appraisal_json_path, mortgage_json_path = await asyncio.gather(
            run_registry_workflow(
                registry_pdf_path=uploaded["registry"],
                output_root=output_root / "registry",
                gpt_mini=gpt_mini,
                gpt_sol=gpt_sol,
                cost_tracker=cost_tracker,
            ),
            run_appraisal_workflow(
                appraisal_pdf_path=uploaded["appraisal"],
                output_root=output_root / "appraisal",
                gpt_mini=gpt_mini,
                gpt_sol=gpt_mini,
                cost_tracker=cost_tracker,
            ),
            run_mortgage_workflow(
                mortgage_pdf_path=uploaded["mortgage_list"],
                output_root=output_root / "mortgage_list",
                json_path=mortgage_json_path,
                gpt_mini=gpt_mini,
                gpt_sol=gpt_sol,
                cost_tracker=cost_tracker,
            ),
        )
        finish_step(job_id, active_step, f"등기부 {len(registry_results)}건과 감정평가/저당목록 분석 결과를 생성했습니다.")

        appraisal_stem = windows_safe_text(uploaded["appraisal"].stem, default=job_id)
        run_id = windows_safe_text(appraisal_stem, default=job_id)
        borrower_name = re.sub(r"^\d+\.\s*", "", appraisal_stem).replace("감정평가서_", "").replace("감정평가서 ", "").strip()

        active_step = "manifest"
        start_step(job_id, active_step, "분석에 사용할 파일 연결 정보를 확정하고 있습니다.")
        manifest = build_manifest(
            run_id=run_id,
            borrower_name=borrower_name,
            registry_pairs=registry_results,
            appraisal_json_path=appraisal_json_path,
            mortgage_json_path=mortgage_json_path,
            output_path=final_output_path,
        )
        manifest_path = write_json(pipeline_output_root / "run_manifest.json", manifest)
        finish_step(job_id, active_step, "작업 기준 정보가 확정되었습니다.")

        active_step = "merge"
        start_step(job_id, active_step, "등기부의 담보설정 현황을 병합하고 있습니다.")
        stage1 = stage1_merge_registry(manifest, pipeline_output_root / "01_registry_merge")
        finish_step(job_id, active_step, "등기부 병합 결과가 생성되었습니다.")

        active_step = "realEstate"
        start_step(job_id, active_step, "부동산 정보와 감정평가액을 대조하고 있습니다.")
        with cost_tracker.stage("02_pipeline.real_estate"):
            stage2 = stage2_real_estate(
                llm=gpt_mini,
                manifest=manifest,
                merged_title_path=stage1["merged_title"],
                merged_encumbrance_path=stage1["merged_encumbrance"],
                stage_dir=pipeline_output_root / "02_real_estate",
            )
        finish_step(job_id, active_step, "부동산 가치평가 대조가 완료되었습니다.")

        active_step = "machineryBase"
        start_step(job_id, active_step, "저당목록 번호를 기준으로 기계기구 담보 그룹을 구성하고 있습니다.")
        stage3 = stage3_machinery_base(
            manifest=manifest,
            merged_title_path=stage1["merged_title"],
            merged_encumbrance_path=stage1["merged_encumbrance"],
            stage_dir=pipeline_output_root / "03_machinery_base",
        )
        finish_step(job_id, active_step, "기계기구 담보 기준이 구성되었습니다.")

        active_step = "machineryMatch"
        start_step(job_id, active_step, "기계기구 평가 행과 저당목록 항목을 대조하고 있습니다.")
        with cost_tracker.stage("02_pipeline.machinery_match"):
            stage4 = await async_stage4_machinery_match(
                llm=gpt_mini,
                manifest=manifest,
                machinery_base_path=stage3["machinery_distribution_base"],
                stage_dir=pipeline_output_root / "04_machinery_match",
            )
        finish_step(job_id, active_step, "기계기구 대조표 계획이 완성되었습니다.")

        active_step = "workbookPlan"
        start_step(job_id, active_step, "엑셀 작성 계획과 검산 결과를 확인하고 있습니다.")
        excel_stage = build_workbook_plan(
            manifest=manifest,
            real_estate_plan_path=stage2["real_estate_table_plan"],
            machinery_plan_path=stage4["machinery_table_plan"],
            output_dir=pipeline_output_root / "05_excel",
        )
        workbook_plan = load_json(excel_stage["workbook_plan"])
        finish_step(job_id, active_step, "엑셀 작성 계획 검증이 완료되었습니다.")

        active_step = "format"
        start_step(job_id, active_step, "최종 문서의 표시 품질을 정리하고 있습니다.")
        with cost_tracker.stage("03_excel.format_plan"):
            format_stage = build_excel_format_plan(
                llm=gpt_terra,
                workbook_plan_path=excel_stage["workbook_plan"],
                output_dir=pipeline_output_root / "06_excel_format",
            )
        format_plan = load_json(format_stage["excel_format_plan"])
        finish_step(job_id, active_step, "엑셀 표시 계획이 준비되었습니다.")

        active_step = "excel"
        start_step(job_id, active_step, "최종 Excel 파일을 생성하고 있습니다.")
        output_path = write_collateral_workbook(workbook_plan, final_output_path, format_plan=format_plan)
        finish_step(job_id, active_step, "최종 Excel 파일이 생성되었습니다.")

        active_step = "report"
        start_step(job_id, active_step, "최종 workbook_plan을 바탕으로 유저용 결과 리포트를 작성하고 있습니다.")
        create_initial_report_for_job(job_id)
        finish_step(job_id, active_step, "유저용 결과 리포트가 작성되었습니다.")

        run_status = "success"
        active_step = "cost"
        start_step(job_id, active_step, "작업 시간과 사용량 내역을 정리하고 있습니다.")
        run_error = None
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {exc}"
        fail_job(job_id, active_step, run_error)
        raise
    finally:
        cost_summary_path = cost_tracker.finalize(status=run_status, error=run_error)
        cost_summary = load_json(cost_summary_path)
        with JOBS_LOCK:
            job = JOBS[job_id]
            job.cost_summary = cost_summary
            job.paths.update({"costSummaryPath": str(cost_summary_path), "costLedgerDir": str(ledger_dir)})
            _write_job_snapshot(job)
        if run_status == "success":
            finish_step(job_id, "cost", "작업 내역 정리가 완료되었습니다.")
            with JOBS_LOCK:
                job = JOBS[job_id]
                job.status = "completed"
                job.current_step = "completed"
                job.progress = 100
                job.finished_at = iso_now()
                job.paths.update(
                    {
                        "manifestPath": str(manifest_path),
                        "workbookPlanPath": str(excel_stage["workbook_plan"]),
                        "excelFormatPlanPath": str(format_stage["excel_format_plan"]),
                        "finalOutputPath": str(output_path),
                    }
                )
                _write_job_snapshot(job)


def run_framework_job(job_id: str, request: CreateJobRequest) -> None:
    try:
        asyncio.run(run_framework_job_async(job_id, request))
    except Exception:
        return


def rag_cost_tracker_for(job: JobState) -> CostTracker:
    return CostTracker(
        run_id=f"{job.job_id}_rag",
        artifact_dir=Path(job.paths["ragDir"]) / "cost_tracking",
        ledger_dir=Path(job.paths["jobRoot"]) / "cost_tracking_data",
        pricing_path=FRAMEWORK_ROOT / "cost_tracking" / "pricing.json",
    )


def rag_session_for_job(job_id: str, *, require_completed: bool = True) -> RagSession:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        if require_completed and job.status != "completed":
            raise HTTPException(status_code=409, detail="작업 완료 후 채팅을 시작할 수 있습니다.")
    with RAG_LOCK:
        session = RAG_SESSIONS.get(job_id)
        if session is not None:
            return session
        tracker = rag_cost_tracker_for(job)
        manager = RagChatManager(
            pipeline_artifacts_dir=Path(job.paths["pipelineArtifactsDir"]),
            framework_output_dir=Path(job.paths["intermediateDir"]),
            rag_dir=Path(job.paths["ragDir"]),
            chat_model=make_chat_llm(tracker),
            embeddings=make_embeddings(),
            cost_tracker=tracker,
            thread_id=DEFAULT_THREAD_ID,
        )
        session = RagSession(manager=manager, cost_tracker=tracker)
        RAG_SESSIONS[job_id] = session
        return session


def refresh_rag_cost(job_id: str, session: RagSession, *, status: str = "success", error: str | None = None) -> dict[str, Any]:
    cost_summary_path = session.cost_tracker.finalize(status=status, error=error)
    llm_summary = compact_cost_summary(load_json(cost_summary_path)) or {}
    embedding_summary = session.manager.usage_summary()
    embedding_tokens = int(embedding_summary.get("embeddingTokens") or 0)
    embedding_cost = float(embedding_summary.get("embeddingEstimatedCostUsd") or 0)
    rag_summary = {
        "status": status,
        "threadId": DEFAULT_THREAD_ID,
        "llm": llm_summary,
        "embedding": embedding_summary,
        "totalTokens": int(llm_summary.get("totalTokens") or 0) + embedding_tokens,
        "estimatedTotalCostUsd": float(llm_summary.get("estimatedTotalCostUsd") or 0) + embedding_cost,
        "costSummaryPath": str(cost_summary_path),
        "usageSummaryPath": str(session.manager.usage_path),
    }
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.rag_cost_summary = rag_summary
        job.paths.update({"ragCostSummaryPath": str(cost_summary_path), "ragUsageSummaryPath": str(session.manager.usage_path)})
        _write_job_snapshot(job)
    return rag_summary


def create_initial_report_for_job(job_id: str) -> dict[str, Any]:
    session = rag_session_for_job(job_id, require_completed=False)
    report_result = session.manager.load_initial_report() if session.manager.report_path.exists() else session.manager.initial_report()
    rag_cost = refresh_rag_cost(job_id, session)
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.initial_report = report_result["report"]
        job.paths["initialReportPath"] = report_result["reportPath"]
        if not any(item.get("type") == "initial_report" for item in job.chat_history):
            job.chat_history.append({"role": "assistant", "message": report_result["report"], "createdAt": iso_now(), "type": "initial_report"})
        _write_job_snapshot(job)
    return {**report_result, "ragCostSummary": rag_cost}


def build_rag_for_job(job_id: str) -> dict[str, Any]:
    session = rag_session_for_job(job_id)
    build_result = session.manager.build_index()
    report_result = create_initial_report_for_job(job_id)
    rag_cost = report_result["ragCostSummary"]
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.rag_ready = True
        job.rag_chunk_count = int(build_result["chunkCount"])
        job.paths["ragIndexPath"] = build_result["ragIndexPath"]
        _write_job_snapshot(job)
    return {"jobId": job_id, **build_result, "report": report_result["report"], "reportPath": report_result["reportPath"], "ragCostSummary": rag_cost}


def chat_with_job(job_id: str, message: str) -> dict[str, Any]:
    session = rag_session_for_job(job_id)
    answer = session.manager.ask(message)
    rag_cost = refresh_rag_cost(job_id, session)
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.chat_history.append({"role": "user", "message": message, "createdAt": iso_now()})
        job.chat_history.append({"role": "assistant", "message": answer["answer"], "sources": answer["sources"], "createdAt": iso_now()})
        _write_job_snapshot(job)
    return {**answer, "ragCostSummary": rag_cost}


def user_dashboard_payload() -> dict[str, Any]:
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    total_jobs = len(jobs)
    completed = sum(job.status == "completed" for job in jobs)
    failed = sum(job.status == "failed" for job in jobs)
    return {
        "totalJobs": total_jobs,
        "completedJobs": completed,
        "failedJobs": failed,
        "runningJobs": total_jobs - completed - failed,
        "jobs": [serialize_job(job, include_cost=False) for job in sorted(jobs, key=lambda j: j.created_at, reverse=True)],
    }


def developer_dashboard_payload() -> dict[str, Any]:
    with JOBS_LOCK:
        jobs = list(JOBS.values())
    total_cost = 0.0
    total_tokens = 0
    total_calls = 0
    for job in jobs:
        framework = compact_cost_summary(job.cost_summary) or {}
        rag = job.rag_cost_summary or {}
        total_cost += float(framework.get("estimatedTotalCostUsd") or 0) + float(rag.get("estimatedTotalCostUsd") or 0)
        total_tokens += int(framework.get("totalTokens") or 0) + int(rag.get("totalTokens") or 0)
        total_calls += int(framework.get("llmCallCount") or 0) + int((rag.get("llm") or {}).get("llmCallCount") or 0)
    return {
        "totalJobs": len(jobs),
        "completedJobs": sum(job.status == "completed" for job in jobs),
        "failedJobs": sum(job.status == "failed" for job in jobs),
        "runningJobs": sum(job.status in {"queued", "running"} for job in jobs),
        "estimatedTotalCostUsd": total_cost,
        "totalTokens": total_tokens,
        "totalLlmCalls": total_calls,
        "jobs": [serialize_job(job, include_cost=True) for job in sorted(jobs, key=lambda j: j.created_at, reverse=True)],
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "frameworkRoot": str(FRAMEWORK_ROOT),
        "runtimeRoot": str(RUNTIME_ROOT),
        "databasePath": str(DATABASE_PATH),
        "chatModel": MINI_MODEL_NAME,
        "embeddingModel": EMBEDDING_MODEL_NAME,
        "ragThreadId": DEFAULT_THREAD_ID,
    }


@app.post("/api/jobs")
def create_job(request: CreateJobRequest) -> dict[str, Any]:
    job_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    job = JobState(
        job_id=job_id,
        paths=paths_for_job(job_id),
        steps=make_steps(),
        input_files={
            "registry": request.registry.filename,
            "appraisal": request.appraisal.filename,
            "mortgage_list": request.mortgage_list.filename,
        },
    )
    with JOBS_LOCK:
        JOBS[job_id] = job
        _write_job_snapshot(job)
    threading.Thread(target=run_framework_job, args=(job_id, request), daemon=True).start()
    return {
        "jobId": job_id,
        "requestTime": display_time(job.created_at),
        "inputFiles": job.input_files,
        "statusUrl": f"/api/jobs/{job_id}",
        "downloadUrl": f"/api/jobs/{job_id}/download",
        "chatStartUrl": f"/api/jobs/{job_id}/rag/build",
        "chatUrl": f"/api/jobs/{job_id}/chat",
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        return serialize_job(job, include_cost=False)


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
        if job.status != "completed":
            raise HTTPException(status_code=409, detail="아직 완료되지 않은 작업입니다.")
        final_path = Path(job.paths["finalOutputPath"])
    if not final_path.exists():
        raise HTTPException(status_code=404, detail="최종 Excel 파일을 찾을 수 없습니다.")
    return FileResponse(
        final_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="담보물배분표.xlsx",
    )


@app.post("/api/jobs/{job_id}/rag/build")
def build_rag(job_id: str) -> dict[str, Any]:
    return build_rag_for_job(job_id)


@app.post("/api/jobs/{job_id}/chat")
def chat(job_id: str, request: ChatRequest) -> dict[str, Any]:
    return chat_with_job(job_id, request.message)


@app.get("/api/dashboard")
def user_dashboard() -> dict[str, Any]:
    return user_dashboard_payload()


@app.get("/api/developer/dashboard")
def developer_dashboard() -> dict[str, Any]:
    return developer_dashboard_payload()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html")


@app.get("/developer")
def developer_index() -> FileResponse:
    return FileResponse(STATIC_ROOT / "developer.html")


app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_service.server:app", host="127.0.0.1", port=8000, reload=False)
