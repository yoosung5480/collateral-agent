"""JSON-aware RAG manager for 담보물배분표 automation artifacts.

The package is independent from the web server. It converts run JSON artifacts
into compact, agent-friendly text, stores embeddings on disk, and uses
LangChain/LangGraph chat-history support for session memory.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tiktoken
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langgraph.checkpoint.memory import InMemorySaver

DEFAULT_THREAD_ID = "collateral-report-session"
INDEX_FILENAME = "rag_index.json"
REPORT_FILENAME = "initial_report.md"
USAGE_FILENAME = "rag_usage_summary.json"
EMBEDDING_USD_PER_1M_TOKENS = 0.02


@dataclass(frozen=True)
class RagChunk:
    source: str
    json_path: str
    title: str
    text: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _money(value: Any) -> str:
    return f"{int(value):,}원" if isinstance(value, (int, float)) else "미확정"


def _short(value: Any) -> str:
    return " ".join(str(value or "").split())


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _token_count(texts: list[str], model: str) -> int:
    encoding = tiktoken.encoding_for_model(model)
    return sum(len(encoding.encode(text)) for text in texts)


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def summarize_workbook_plan(workbook_plan: dict[str, Any]) -> str:
    real_estate_lines = []
    for idx, table in enumerate(workbook_plan.get("real_estate_tables") or [], start=1):
        properties = "; ".join(
            f"{p.get('property_type')} {_short(p.get('location'))} {_short(p.get('details'))[:120]}"
            for p in table.get("properties") or []
        )
        matches = "; ".join(
            f"{m.get('property_type')} {m.get('location_lot_number')}={_money(m.get('appraised_value'))}"
            for m in table.get("appraisal_matches") or []
        )
        mortgages = "; ".join(
            f"{m.get('rank')}순위 {m.get('mortgagee')} 채권최고액 {_money(m.get('maximum_claim'))} {m.get('mortgage_list_no') or ''}"
            for m in table.get("mortgages") or []
        )
        real_estate_lines.append(
            f"부동산 표 {idx}: group_id={table.get('group_id')}, 담보권자={table.get('mortgagee')}, "
            f"가치평가액={_money(table.get('total_appraised_value'))}\n"
            f"- 담보물: {properties}\n- 감정평가 매칭: {matches}\n- 담보설정: {mortgages}"
        )

    machinery = workbook_plan.get("machinery_table") or {}
    comparison = workbook_plan.get("machinery_comparison") or {}
    machine_lines = []
    for row in (comparison.get("rows") or [])[:40]:
        membership = ", ".join(f"{k}: {v}" for k, v in (row.get("membership") or {}).items())
        machine_lines.append(
            f"{row.get('appraisal_row_index')}. {row.get('name')} / 제작자={row.get('manufacturer')} / "
            f"제작일={row.get('manufacture_date')} / 수량={row.get('quantity')} / 금액={_money(row.get('amount'))} / 목록포함={membership}"
        )

    return "\n".join(
        [
            f"실행 ID: {workbook_plan.get('run_id')}",
            f"채무자/표시명: {workbook_plan.get('borrower_name')}",
            f"상태: {workbook_plan.get('status')}",
            "",
            "[부동산 담보물 배분표 요약]",
            "\n\n".join(real_estate_lines) or "부동산 표 없음",
            "",
            "[기계기구 담보물 배분표 요약]",
            f"기준 그룹: {machinery.get('source_group_id')}",
            f"수량 합계: {comparison.get('quantity_total')}",
            f"감정평가액 합계: {_money(comparison.get('amount_total'))}",
            f"저당목록 번호: {', '.join(comparison.get('mortgage_list_numbers') or [])}",
            "\n".join(machine_lines) or "기계기구 행 없음",
            "",
            f"검토 필요 항목: {len(workbook_plan.get('review_required') or [])}건",
            f"차단 오류: {len(workbook_plan.get('blocking_errors') or [])}건",
        ]
    )


def report_prompt_text(workbook_summary: str) -> str:
    return (
        "너는 담보물배분표 자동화 결과를 고객에게 설명하는 회계 분석 전문가다. "
        "항상 아래 workbook_plan 핵심 요약을 최우선 근거로 삼아 답한다. "
        "사용자에게는 결과 요약, 산정 근거, 출처/확인 위치, 확인 필요 가능성을 구분해서 설명한다.\n\n"
        "[항상 참조할 workbook_plan 요약]\n"
        f"{workbook_summary}"
    )


def json_artifact_paths(*roots: Path) -> list[Path]:
    seen: set[Path] = set()
    paths: list[Path] = []
    for root in roots:
        if not root or not Path(root).exists():
            continue
        for path in sorted(Path(root).rglob("*.json")):
            resolved = path.resolve()
            if path.is_file() and resolved not in seen:
                seen.add(resolved)
                paths.append(path)
    return paths


def _chunk_text(source: Path, json_path: str, title: str, lines: list[str]) -> RagChunk:
    return RagChunk(source=str(source), json_path=json_path, title=title, text="\n".join(line for line in lines if line))


def chunks_from_workbook_plan(path: Path, plan: dict[str, Any]) -> list[RagChunk]:
    chunks = [_chunk_text(path, "$", "최종 Excel 작성 계획 전체 요약", [summarize_workbook_plan(plan)])]
    for idx, table in enumerate(plan.get("real_estate_tables") or [], start=1):
        chunks.append(
            _chunk_text(
                path,
                f"$.real_estate_tables[{idx - 1}]",
                f"부동산 표 {idx} 상세",
                [
                    f"group_id: {table.get('group_id')}",
                    f"담보권자: {table.get('mortgagee')}",
                    f"가치평가액: {_money(table.get('total_appraised_value'))}",
                    "담보물: " + "; ".join(f"{p.get('property_type')} {p.get('location')}" for p in table.get("properties") or []),
                    "감정평가 매칭: " + "; ".join(f"{m.get('appraisal_group_id')} {m.get('location_lot_number')} {_money(m.get('appraised_value'))}" for m in table.get("appraisal_matches") or []),
                    "근저당권: " + "; ".join(f"{m.get('rank')}순위 {m.get('mortgagee')} {_money(m.get('maximum_claim'))}" for m in table.get("mortgages") or []),
                ],
            )
        )
    for idx, row in enumerate((plan.get("machinery_comparison") or {}).get("rows") or [], start=1):
        chunks.append(
            _chunk_text(
                path,
                f"$.machinery_comparison.rows[{idx - 1}]",
                f"기계기구 대조 행 {idx}",
                [
                    f"일련번호: {row.get('appraisal_row_index')}",
                    f"명칭: {row.get('name')}",
                    f"제작자: {row.get('manufacturer')}",
                    f"제작일자: {row.get('manufacture_date')}",
                    f"수량: {row.get('quantity')}",
                    f"금액: {_money(row.get('amount'))}",
                    "저당목록 포함여부: " + "; ".join(f"{k}={v}" for k, v in (row.get("membership") or {}).items()),
                ],
            )
        )
    return chunks


def compact_json_text(value: Any) -> str:
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if isinstance(item, (str, int, float, bool)) or item is None:
                parts.append(f"{key}: {item}")
            elif isinstance(item, list):
                parts.append(f"{key}: {len(item)}개")
            elif isinstance(item, dict):
                parts.append(f"{key}: {{{', '.join(list(item.keys())[:8])}}}")
        return "\n".join(parts)
    return str(value)


def chunks_from_artifact(path: Path, data: Any) -> list[RagChunk]:
    if path.name == "workbook_plan.json" and isinstance(data, dict):
        return chunks_from_workbook_plan(path, data)
    chunks: list[RagChunk] = []
    if isinstance(data, dict):
        chunks.append(
            _chunk_text(
                path,
                "$",
                f"{path.name} 상태 요약",
                [
                    f"파일: {path.name}",
                    f"상태: {data.get('status')}",
                    f"검토 필요: {len(data.get('review_required') or []) if isinstance(data.get('review_required'), list) else 0}건",
                    f"차단 오류: {len(data.get('blocking_errors') or []) if isinstance(data.get('blocking_errors'), list) else 0}건",
                ],
            )
        )
        for key, value in data.items():
            if isinstance(value, list):
                for idx, item in enumerate(value[:80]):
                    chunks.append(_chunk_text(path, f"$.{key}[{idx}]", f"{path.name} {key} {idx + 1}", [compact_json_text(item)]))
            elif isinstance(value, dict):
                chunks.append(_chunk_text(path, f"$.{key}", f"{path.name} {key}", [compact_json_text(value)]))
    elif isinstance(data, list):
        for idx, item in enumerate(data[:120]):
            chunks.append(_chunk_text(path, f"$[{idx}]", f"{path.name} 항목 {idx + 1}", [compact_json_text(item)]))
    return [chunk for chunk in chunks if len(chunk.text) > 30]


class RagChatManager:
    def __init__(
        self,
        *,
        pipeline_artifacts_dir: Path,
        rag_dir: Path,
        chat_model: ChatOpenAI,
        embeddings: OpenAIEmbeddings,
        framework_output_dir: Path | None = None,
        cost_tracker: Any | None = None,
        thread_id: str = DEFAULT_THREAD_ID,
    ) -> None:
        self.pipeline_artifacts_dir = Path(pipeline_artifacts_dir)
        self.framework_output_dir = Path(framework_output_dir) if framework_output_dir is not None else None
        self.rag_dir = Path(rag_dir)
        self.chat_model = chat_model
        self.embeddings = embeddings
        self.cost_tracker = cost_tracker
        self.thread_id = thread_id
        self.workbook_plan_path = self.pipeline_artifacts_dir / "05_excel" / "workbook_plan.json"
        self.session_checkpointer = InMemorySaver()
        self.workbook_summary = summarize_workbook_plan(_load_json(self.workbook_plan_path))
        self.session_agent = create_agent(
            model=self.chat_model,
            tools=[],
            checkpointer=self.session_checkpointer,
            system_prompt=report_prompt_text(self.workbook_summary),
        )

    @property
    def config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}}

    @property
    def index_path(self) -> Path:
        return self.rag_dir / INDEX_FILENAME

    @property
    def report_path(self) -> Path:
        return self.rag_dir / REPORT_FILENAME

    @property
    def usage_path(self) -> Path:
        return self.rag_dir / USAGE_FILENAME

    def usage_summary(self) -> dict[str, Any]:
        if self.usage_path.exists():
            return _load_json(self.usage_path)
        return {
            "embeddingModel": getattr(self.embeddings, "model", "unknown"),
            "embeddingCallCount": 0,
            "embeddingTokens": 0,
            "embeddingEstimatedCostUsd": 0.0,
            "events": [],
        }

    def _record_embedding_usage(self, *, stage: str, texts: list[str]) -> dict[str, Any]:
        token_count = _token_count(texts, getattr(self.embeddings, "model", "text-embedding-3-small"))
        cost = token_count * EMBEDDING_USD_PER_1M_TOKENS / 1_000_000
        summary = self.usage_summary()
        summary["embeddingCallCount"] = int(summary.get("embeddingCallCount") or 0) + 1
        summary["embeddingTokens"] = int(summary.get("embeddingTokens") or 0) + token_count
        summary["embeddingEstimatedCostUsd"] = float(summary.get("embeddingEstimatedCostUsd") or 0) + cost
        summary["updatedAt"] = _now_iso()
        summary.setdefault("events", []).append(
            {"stage": stage, "textCount": len(texts), "tokens": token_count, "estimatedCostUsd": cost, "createdAt": _now_iso()}
        )
        _write_json(self.usage_path, summary)
        return summary

    def build_chunks(self) -> list[RagChunk]:
        chunks: list[RagChunk] = []
        for path in json_artifact_paths(self.pipeline_artifacts_dir, self.framework_output_dir):
            chunks.extend(chunks_from_artifact(path, _load_json(path)))
        return chunks

    def build_index(self) -> dict[str, Any]:
        chunks = self.build_chunks()
        texts = [chunk.text for chunk in chunks]
        vectors = self.embeddings.embed_documents(texts)
        usage = self._record_embedding_usage(stage="rag.embed_artifacts", texts=texts)
        payload = {
            "embeddingModel": getattr(self.embeddings, "model", "unknown"),
            "createdAt": _now_iso(),
            "chunkCount": len(chunks),
            "workbookSummary": self.workbook_summary,
            "chunks": [
                {"source": chunk.source, "jsonPath": chunk.json_path, "title": chunk.title, "text": chunk.text, "embedding": vector}
                for chunk, vector in zip(chunks, vectors, strict=True)
            ],
        }
        _write_json(self.index_path, payload)
        return {
            "ragReady": True,
            "chunkCount": len(chunks),
            "ragIndexPath": str(self.index_path),
            "embeddingUsage": usage,
        }

    def load_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            self.build_index()
        return _load_json(self.index_path)

    def retrieve(self, question: str, *, k: int = 8) -> list[dict[str, Any]]:
        index = self.load_index()
        query_vector = self.embeddings.embed_query(question)
        self._record_embedding_usage(stage="rag.embed_query", texts=[question])
        ranked = sorted(
            (
                {"score": cosine(query_vector, chunk["embedding"]), **{key: value for key, value in chunk.items() if key != "embedding"}}
                for chunk in index["chunks"]
            ),
            key=lambda item: item["score"],
            reverse=True,
        )
        return ranked[:k]

    def load_initial_report(self) -> dict[str, Any]:
        return {"report": self.report_path.read_text(encoding="utf-8"), "reportPath": str(self.report_path)}

    def initial_report(self) -> dict[str, Any]:
        prompt = (
            "최종 workbook_plan 요약을 바탕으로 고객이 바로 읽을 수 있는 실행 결과 보고서를 작성하라. "
            "다음 형식을 지켜라: 1) 처리 결과 요약 2) 부동산 담보 평가 3) 기계기구 담보 평가 4) 산정 근거 5) 확인 필요 가능성 6) 주요 출처."
        )
        if self.cost_tracker is None:
            result = self.session_agent.invoke({"messages": [HumanMessage(content=prompt)]}, config=self.config)
        else:
            with self.cost_tracker.stage("rag.initial_report"):
                result = self.session_agent.invoke({"messages": [HumanMessage(content=prompt)]}, config=self.config)
        answer = result["messages"][-1].content
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(answer, encoding="utf-8")
        return {"report": answer, "reportPath": str(self.report_path)}

    def ask(self, question: str) -> dict[str, Any]:
        sources = self.retrieve(question)
        context = "\n\n---\n\n".join(
            f"[근거 {idx}] source={item['source']} json_path={item['jsonPath']} score={item['score']:.4f}\n제목: {item['title']}\n{item['text']}"
            for idx, item in enumerate(sources, start=1)
        )
        prompt = (
            f"사용자 질문:\n{question}\n\n"
            "아래는 질문에 맞춰 자동 검색된 중간산출물 근거다. 답변에는 사용한 출처를 파일명과 json_path로 표시하라.\n\n"
            f"{context}"
        )
        if self.cost_tracker is None:
            result = self.session_agent.invoke({"messages": [HumanMessage(content=prompt)]}, config=self.config)
        else:
            with self.cost_tracker.stage("rag.chat_answer"):
                result = self.session_agent.invoke({"messages": [HumanMessage(content=prompt)]}, config=self.config)
        return {
            "answer": result["messages"][-1].content,
            "sources": [{key: value for key, value in item.items() if key != "text"} for item in sources],
        }
