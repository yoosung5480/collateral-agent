"""Per-run LLM usage and cost ledger.

The tracker intentionally observes actual LangChain/OpenAI response metadata rather
than estimating tokens from prompts.  It writes both a per-run artifact and an
append-only local ledger so `python main.py` executions accumulate automatically.
"""

from __future__ import annotations

import contextvars
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

_STAGE: contextvars.ContextVar[str] = contextvars.ContextVar("llm_cost_stage", default="unknown")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalise_model_name(model_name: str | None) -> str | None:
    if not model_name:
        return None
    # OpenAI/LangChain sometimes returns dated names such as
    # "gpt-4o-mini-2024-07-18".  Keep exact first; pricing lookup later also
    # tries prefix/base names.
    return str(model_name)


@dataclass(frozen=True)
class CostBreakdown:
    input_usd: float | None
    cached_input_usd: float | None
    output_usd: float | None
    total_usd: float | None
    total_krw: float | None
    pricing_missing: bool


class PricingCatalog:
    """Loads local per-1M-token prices and converts observed usage to cost."""

    def __init__(self, pricing_path: Path) -> None:
        self.pricing_path = pricing_path
        self.raw = self._load(pricing_path)
        self.usd_krw_rate = _safe_float(os.getenv("USD_KRW_RATE"))
        if self.usd_krw_rate is None:
            self.usd_krw_rate = _safe_float(self.raw.get("usd_krw_rate"))
        self.models: dict[str, dict[str, Any]] = dict(self.raw.get("models") or {})

    def _load(self, pricing_path: Path) -> dict[str, Any]:
        if not pricing_path.exists():
            return {"models": {}, "usd_krw_rate": None}
        with pricing_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def snapshot(self) -> dict[str, Any]:
        data = deepcopy(self.raw)
        data["effective_usd_krw_rate"] = self.usd_krw_rate
        data["pricing_path"] = str(self.pricing_path)
        return data

    def _candidate_names(self, model_name: str | None) -> list[str]:
        if not model_name:
            return []
        name = str(model_name)
        candidates = [name]
        # Common LangChain/OpenAI return values include provider prefixes or
        # date/version suffixes. Try stable base aliases without changing the
        # stored raw model name.
        if name.startswith("openai:"):
            candidates.append(name.split(":", 1)[1])
        parts = name.split("-")
        if len(parts) > 2 and parts[-1].isdigit():
            candidates.append("-".join(parts[:-1]))
        # Try configured aliases whose key is a prefix of the returned name.
        for configured in self.models:
            if name.startswith(configured) and configured not in candidates:
                candidates.append(configured)
        return candidates

    def price_for(self, model_name: str | None) -> tuple[str | None, dict[str, Any] | None]:
        for candidate in self._candidate_names(model_name):
            if candidate in self.models:
                return candidate, self.models[candidate]
        return None, None

    def calculate(self, model_name: str | None, usage: dict[str, int]) -> tuple[CostBreakdown, str | None]:
        matched_model, prices = self.price_for(model_name)
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cached_input_tokens = usage.get("cached_input_tokens", 0)
        billable_input_tokens = max(input_tokens - cached_input_tokens, 0)

        if not prices:
            return CostBreakdown(None, None, None, None, None, True), matched_model

        input_rate = _safe_float(prices.get("input_per_1m_usd"))
        cached_rate = _safe_float(prices.get("cached_input_per_1m_usd"))
        output_rate = _safe_float(prices.get("output_per_1m_usd"))
        if input_rate is None or output_rate is None:
            return CostBreakdown(None, None, None, None, None, True), matched_model
        if cached_rate is None:
            cached_rate = input_rate

        input_usd = billable_input_tokens * input_rate / 1_000_000
        cached_input_usd = cached_input_tokens * cached_rate / 1_000_000
        output_usd = output_tokens * output_rate / 1_000_000
        total_usd = input_usd + cached_input_usd + output_usd
        total_krw = total_usd * self.usd_krw_rate if self.usd_krw_rate is not None else None
        return (
            CostBreakdown(
                input_usd=input_usd,
                cached_input_usd=cached_input_usd,
                output_usd=output_usd,
                total_usd=total_usd,
                total_krw=total_krw,
                pricing_missing=False,
            ),
            matched_model,
        )


class CostTracker(BaseCallbackHandler):
    """LangChain callback handler that persists actual LLM usage per run."""

    def __init__(self, *, run_id: str, artifact_dir: Path, ledger_dir: Path, pricing_path: Path) -> None:
        super().__init__()
        self.pipeline_run_id = run_id
        self.started_at = _now_iso()
        self._perf_started = time.perf_counter()
        self.artifact_dir = artifact_dir
        self.ledger_dir = ledger_dir
        self.pricing = PricingCatalog(pricing_path)
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._llm_failures: list[dict[str, Any]] = []
        self._active_runs: dict[str, dict[str, Any]] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        token = _STAGE.set(name)
        try:
            yield
        finally:
            _STAGE.reset(token)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        self._remember_start(run_id, parent_run_id=parent_run_id, tags=tags, metadata=metadata)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        self._remember_start(run_id, parent_run_id=parent_run_id, tags=tags, metadata=metadata)

    def _remember_start(
        self,
        run_id: UUID,
        *,
        parent_run_id: UUID | None,
        tags: list[str] | None,
        metadata: dict[str, Any] | None,
    ) -> None:
        now_perf = time.perf_counter()
        started_at = _now_iso()
        with self._lock:
            self._active_runs[str(run_id)] = {
                "started_at_utc": started_at,
                "start_perf": now_perf,
                "stage": _STAGE.get(),
                "parent_run_id": str(parent_run_id) if parent_run_id else None,
                "tags": tags or [],
                "metadata": metadata or {},
            }

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        event = self._build_success_event(response, run_id=run_id, parent_run_id=parent_run_id, tags=tags, kwargs=kwargs)
        with self._lock:
            self._events.append(event)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        ended_perf = time.perf_counter()
        ended_at = _now_iso()
        with self._lock:
            start_info = self._active_runs.pop(str(run_id), None)
        duration_seconds = None
        started_at = None
        stage_name = _STAGE.get()
        if start_info:
            started_at = start_info.get("started_at_utc")
            duration_seconds = round(ended_perf - float(start_info.get("start_perf", ended_perf)), 6)
            stage_name = start_info.get("stage") or stage_name
            tags = tags or start_info.get("tags")
        event = {
            "event_type": "llm_error",
            "pipeline_run_id": self.pipeline_run_id,
            "timestamp_utc": ended_at,
            "started_at_utc": started_at,
            "ended_at_utc": ended_at,
            "duration_seconds": duration_seconds,
            "stage": stage_name,
            "langchain_run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "tags": tags or [],
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
        with self._lock:
            self._llm_failures.append(event)

    def _build_success_event(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None,
        tags: list[str] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        ended_perf = time.perf_counter()
        ended_at = _now_iso()
        with self._lock:
            start_info = self._active_runs.pop(str(run_id), None)
        duration_seconds = None
        started_at = None
        stage_name = _STAGE.get()
        if start_info:
            started_at = start_info.get("started_at_utc")
            duration_seconds = round(ended_perf - float(start_info.get("start_perf", ended_perf)), 6)
            stage_name = start_info.get("stage") or stage_name
            tags = tags or start_info.get("tags")
            parent_run_id = parent_run_id or UUID(start_info["parent_run_id"]) if start_info.get("parent_run_id") else parent_run_id

        model_name, usage, raw_usage = self._extract_usage(response)
        cost, pricing_model = self.pricing.calculate(model_name, usage)
        event = {
            "event_type": "llm_end",
            "pipeline_run_id": self.pipeline_run_id,
            "timestamp_utc": ended_at,
            "started_at_utc": started_at,
            "ended_at_utc": ended_at,
            "duration_seconds": duration_seconds,
            "stage": stage_name,
            "langchain_run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "tags": tags or [],
            "model_name": model_name,
            "pricing_model": pricing_model,
            "usage": usage,
            "raw_usage": raw_usage,
            "estimated_cost_usd": cost.total_usd,
            "cost": {
                "input_usd": cost.input_usd,
                "cached_input_usd": cost.cached_input_usd,
                "output_usd": cost.output_usd,
                "total_usd": cost.total_usd,
                "total_krw": cost.total_krw,
                "pricing_missing": cost.pricing_missing,
            },
        }
        return event

    def _extract_usage(self, response: LLMResult) -> tuple[str | None, dict[str, int], dict[str, Any]]:
        model_name: str | None = None
        raw_usage: dict[str, Any] = {}

        llm_output = response.llm_output or {}
        if isinstance(llm_output, dict):
            model_name = _normalise_model_name(llm_output.get("model_name") or llm_output.get("model"))
            token_usage = llm_output.get("token_usage") or llm_output.get("usage")
            if isinstance(token_usage, dict):
                raw_usage.update(token_usage)

        for generations in response.generations or []:
            for generation in generations:
                message = getattr(generation, "message", None)
                if message is None:
                    continue
                usage_metadata = getattr(message, "usage_metadata", None)
                if isinstance(usage_metadata, dict):
                    raw_usage.update(usage_metadata)
                response_metadata = getattr(message, "response_metadata", None)
                if isinstance(response_metadata, dict):
                    model_name = model_name or _normalise_model_name(
                        response_metadata.get("model_name") or response_metadata.get("model")
                    )
                    token_usage = response_metadata.get("token_usage") or response_metadata.get("usage")
                    if isinstance(token_usage, dict):
                        raw_usage.update(token_usage)
                    if not raw_usage and isinstance(response_metadata.get("usage_metadata"), dict):
                        raw_usage.update(response_metadata["usage_metadata"])

        input_tokens = (
            _safe_int(raw_usage.get("input_tokens"))
            or _safe_int(raw_usage.get("prompt_tokens"))
            or _safe_int(raw_usage.get("prompt_token_count"))
            or 0
        )
        output_tokens = (
            _safe_int(raw_usage.get("output_tokens"))
            or _safe_int(raw_usage.get("completion_tokens"))
            or _safe_int(raw_usage.get("completion_token_count"))
            or 0
        )
        total_tokens = (
            _safe_int(raw_usage.get("total_tokens"))
            or _safe_int(raw_usage.get("total_token_count"))
            or input_tokens + output_tokens
        )

        input_details = raw_usage.get("input_token_details") if isinstance(raw_usage.get("input_token_details"), dict) else {}
        output_details = raw_usage.get("output_token_details") if isinstance(raw_usage.get("output_token_details"), dict) else {}
        prompt_details = raw_usage.get("prompt_tokens_details") if isinstance(raw_usage.get("prompt_tokens_details"), dict) else {}
        completion_details = raw_usage.get("completion_tokens_details") if isinstance(raw_usage.get("completion_tokens_details"), dict) else {}

        cached_input_tokens = (
            _safe_int(raw_usage.get("cached_input_tokens"))
            or _safe_int(input_details.get("cache_read"))
            or _safe_int(input_details.get("cached_tokens"))
            or _safe_int(prompt_details.get("cached_tokens"))
            or 0
        )
        reasoning_tokens = (
            _safe_int(raw_usage.get("reasoning_tokens"))
            or _safe_int(output_details.get("reasoning"))
            or _safe_int(output_details.get("reasoning_tokens"))
            or _safe_int(completion_details.get("reasoning_tokens"))
            or 0
        )

        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_input_tokens": cached_input_tokens,
            "reasoning_tokens": reasoning_tokens,
        }
        return model_name, usage, raw_usage

    def finalize(self, *, status: str, error: str | None = None) -> Path:
        ended_at = _now_iso()
        elapsed_seconds = round(time.perf_counter() - self._perf_started, 6)
        with self._lock:
            events = list(self._events)
            failures = list(self._llm_failures)

        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_dir.mkdir(parents=True, exist_ok=True)

        event_path = self.artifact_dir / "llm_usage_events.jsonl"
        with event_path.open("w", encoding="utf-8") as f:
            for event in events + failures:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

        ledger_event_path = self.ledger_dir / "llm_usage_events.jsonl"
        with ledger_event_path.open("a", encoding="utf-8") as f:
            for event in events + failures:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

        pricing_snapshot_path = self.artifact_dir / "pricing_snapshot.json"
        pricing_snapshot = self.pricing.snapshot()
        with pricing_snapshot_path.open("w", encoding="utf-8") as f:
            json.dump(pricing_snapshot, f, ensure_ascii=False, indent=2, default=str)
        with (self.ledger_dir / f"pricing_snapshot_{self.pipeline_run_id}.json").open("w", encoding="utf-8") as f:
            json.dump(pricing_snapshot, f, ensure_ascii=False, indent=2, default=str)

        summary = self._build_summary(
            events=events,
            failures=failures,
            status=status,
            error=error,
            started_at=self.started_at,
            ended_at=ended_at,
            event_path=event_path,
            ledger_event_path=ledger_event_path,
            pricing_snapshot_path=pricing_snapshot_path,
            elapsed_seconds=elapsed_seconds,
        )
        summary_path = self.artifact_dir / "cost_summary.json"
        with summary_path.open("w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
        with (self.ledger_dir / "run_summaries.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")
        return summary_path

    def _build_summary(
        self,
        *,
        events: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        status: str,
        error: str | None,
        started_at: str,
        ended_at: str,
        event_path: Path,
        ledger_event_path: Path,
        pricing_snapshot_path: Path,
        elapsed_seconds: float,
    ) -> dict[str, Any]:
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "total_usd": 0.0,
            "total_krw": 0.0,
            "llm_duration_seconds": 0.0,
        }
        any_usd = False
        any_krw = False
        pricing_missing = False
        by_stage: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}

        def bucket(mapping: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
            if key not in mapping:
                mapping[key] = {
                    "call_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_usd": 0.0,
                    "total_krw": 0.0,
                    "llm_duration_seconds": 0.0,
                    "pricing_missing": False,
                }
            return mapping[key]

        for event in events:
            usage = event.get("usage") or {}
            cost = event.get("cost") or {}
            for key in ["input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"]:
                totals[key] += int(usage.get(key) or 0)
            usd = _safe_float(cost.get("total_usd"))
            krw = _safe_float(cost.get("total_krw"))
            if usd is not None:
                totals["total_usd"] += usd
                any_usd = True
            if krw is not None:
                totals["total_krw"] += krw
                any_krw = True
            duration = _safe_float(event.get("duration_seconds")) or 0.0
            totals["llm_duration_seconds"] += duration
            if cost.get("pricing_missing"):
                pricing_missing = True

            for mapping, key in ((by_stage, event.get("stage") or "unknown"), (by_model, event.get("model_name") or "unknown")):
                b = bucket(mapping, key)
                b["call_count"] += 1
                for token_key in ["input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens"]:
                    b[token_key] += int(usage.get(token_key) or 0)
                if usd is not None:
                    b["total_usd"] += usd
                if krw is not None:
                    b["total_krw"] += krw
                b["llm_duration_seconds"] += duration
                if cost.get("pricing_missing"):
                    b["pricing_missing"] = True

        if not any_usd:
            totals["total_usd"] = None
        if not any_krw:
            totals["total_krw"] = None
        for mapping in (by_stage, by_model):
            for b in mapping.values():
                if not any_usd:
                    b["total_usd"] = None
                if not any_krw:
                    b["total_krw"] = None

        return {
            "pipeline_run_id": self.pipeline_run_id,
            "status": status,
            "error": error,
            "started_at_utc": started_at,
            "ended_at_utc": ended_at,
            "elapsed_seconds": elapsed_seconds,
            "estimated_total_cost_usd": totals["total_usd"] or 0.0,
            "llm_call_count": len(events),
            "llm_error_count": len(failures),
            "totals": totals,
            "pricing_missing": pricing_missing,
            "by_stage": by_stage,
            "by_model": by_model,
            "artifacts": {
                "events_jsonl": str(event_path),
                "ledger_events_jsonl": str(ledger_event_path),
                "pricing_snapshot_json": str(pricing_snapshot_path),
            },
        }
