"""통합 등기부등본 분리·추출 워크플로우."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

sys.path.append(str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parent))
from fixture_data import REGISTRY_DOCUMENT_STEM, is_known_sample, registry_fixture_outputs
from .pdf_llm_parser import RawRegistryParser
from .parse_registry_info import RegistryParser



def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_registry_fixtures(source_path: str | Path, output_path: str | Path) -> list[dict[str, Any]]:
    output_path = Path(output_path)
    results = []
    for item in registry_fixture_outputs(source_path):
        registry_dir = output_path / item["dir_name"]
        _write_json(registry_dir / "title.json", item["title"])
        _write_json(registry_dir / "encumbrance.json", item["encumbrance"])
        results.append({"registry": item["dir_name"], "title_json": str(registry_dir / "title.json"), "encumbrance_json": str(registry_dir / "encumbrance.json")})
    return results


async def run_registry_workflow(
    source_path: str | Path,
    output_path: str | Path,
    preprocess_output_path: str | Path,
    *,
    raw_llm: Any | None = None,
    registry_llm: Any | None = None,
    allow_fixture_fallback: bool = True,
    clean_preprocess: bool = True,
) -> list[dict[str, Any]]:
    source_path = Path(source_path)
    output_path = Path(output_path)
    preprocess_output_path = Path(preprocess_output_path)
    output_path.mkdir(parents=True, exist_ok=True)

    if raw_llm is None or registry_llm is None:
        if allow_fixture_fallback and is_known_sample(source_path, REGISTRY_DOCUMENT_STEM):
            return write_registry_fixtures(source_path, output_path)
        raise RuntimeError("run_registry_workflow requires raw_llm and registry_llm unless fixture fallback is available.")

    raw_registry_parser = RawRegistryParser(llm=raw_llm)
    registry_parser = RegistryParser(llm=registry_llm)

    try:
        split_results = await asyncio.to_thread(
            raw_registry_parser.split_registry_sections,
            source_path,
            preprocess_output_path,
        )
        results: list[dict[str, Any]] = []
        for split in split_results:
            title_result, encumbrance_result = await registry_parser.async_parse_registry(split["title_path"])
            registry_dir = output_path / split["registry"]
            title_result.save_json(registry_dir / "title.json")
            encumbrance_result.save_json(registry_dir / "encumbrance.json")
            results.append({**split, "title_json": str(registry_dir / "title.json"), "encumbrance_json": str(registry_dir / "encumbrance.json")})
        return results
    except Exception:
        if not (allow_fixture_fallback and is_known_sample(source_path, REGISTRY_DOCUMENT_STEM)):
            raise
        return write_registry_fixtures(source_path, output_path)
    finally:
        if clean_preprocess and preprocess_output_path.exists():
            for child in preprocess_output_path.iterdir():
                if child.name == ".DS_Store":
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()


def build_default_llms() -> tuple[Any | None, Any | None]:
    load_dotenv(Path(__file__).with_name('.env'))
    key = os.getenv("OPENAI_API_KEY")
    if not key or os.getenv("USE_FIXTURE_ONLY") == "1" or os.getenv("FORCE_LLM") != "1":
        return None, None
    raw = ChatOpenAI(
        model="gpt-5.4-mini",
        api_key=key,
        temperature=0,
    )
    registry = ChatOpenAI(
        model="gpt-5.6-sol",
        api_key=key,
        temperature=0,
        max_retries=3,
    )
    return raw, registry


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    source_path = root / "datas" / "부동산등기부등본.pdf"
    output_path = root / "output" / source_path.stem
    preprocess_output_path = root / "preprocess" / "registry_split"
    raw_llm, registry_llm = build_default_llms()
    results = asyncio.run(
        run_registry_workflow(
            source_path,
            output_path,
            preprocess_output_path,
            raw_llm=raw_llm,
            registry_llm=registry_llm,
            allow_fixture_fallback=True,
            clean_preprocess=True,
        )
    )
    report = Path(__file__).with_name("report.md")
    report.write_text(
        "# 등기부등본 추출 보고서\n\n"
        f"- 입력: `{source_path}`\n"
        f"- 출력 폴더: `{output_path}`\n"
        f"- 등기부 단위: {len(results)}건\n"
        f"- 전처리 폴더 정리: `{preprocess_output_path}`\n",
        encoding="utf-8",
    )
    print(output_path)


if __name__ == "__main__":
    main()
