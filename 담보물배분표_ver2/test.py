from mortgage_list.parser import MortgageParser
from appraisal.parser import AppraisalParser
from registry.pdf_llm_parser import RawRegistryParser
from registry.parse_registry_info import RegistryParser

import os
import time
from typing import Any
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
load_dotenv("../.env")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

gpt_mini = ChatOpenAI(
        model="gpt-5.6-luna",
        api_key=OPENAI_API_KEY,
        temperature=0,
        reasoning_effort="none"
    )

gpt_sol = ChatOpenAI(
        model="gpt-5.6-terra",
        api_key=OPENAI_API_KEY,
        temperature=0,
        reasoning_effort="none"
    )

# print("# =================================================== #")
# print("                 등기부등본 분석")
# registry_path = Path("datas/부동산등기부등본.pdf")
# output_path = Path("output/registry_luna_ver")
# raw_registry_parser = RawRegistryParser(llm=gpt_mini)
# registry_parser = RegistryParser(llm=gpt_sol)

# print()
# print("전체 원본 파일에 대한 슬라이싱")
# start_time = time.perf_counter()
# split_results = raw_registry_parser.split_registry_sections(
#     source_path=registry_path,
#     output_path=output_path)
# end_time = time.perf_counter()
# print(f"실행 시간: {end_time - start_time:.3f}초")
# print()

# results: list[dict[str, Any]] = []
# for split in split_results:
#     registry_dir = output_path / split["registry"]

#     start_time = time.perf_counter()
#     title_result = registry_parser.extract_title(source_path=split["title_path"])
#     end_time = time.perf_counter()
#     print(f"실행 시간: {end_time - start_time:.3f}초")

#     start_time = time.perf_counter()
#     eul_result = registry_parser.extract_encumbrance(source_path=split["encumbrance_path"])
#     end_time = time.perf_counter()
#     print(f"실행 시간: {end_time - start_time:.3f}초")

#     title_result.save_json(registry_dir / "title.json")
#     eul_result.save_json(registry_dir / "encumbrance.json")
#     results.append({**split, "title_json": str(registry_dir / "title.json"), "encumbrance_json": str(registry_dir / "encumbrance.json")})
# print("# =================================================== #")



print("# =================================================== #")
print("                 감정평가서 분석")
input_path = Path("datas/감정평가서 휴먼중공업.pdf")
output_root = Path("output/appraisal_luna_ver")
parser = AppraisalParser(section_llm=gpt_mini, extraction_llm=gpt_sol)

start_time = time.perf_counter()
appraisal_result = parser.invoke(source_path=input_path, output_root=output_root)
end_time = time.perf_counter()
print(f"실행 시간: {end_time - start_time:.3f}초")
print("# =================================================== #")


print("# =================================================== #")
print("                 저당목록 분석")
# CURRENT_ROOT = Path("/Users/yujin/Desktop/성현회계법인/담보물배분표/mortgage_list")
# PROJECT_ROOT = Path("/Users/yujin/Desktop/성현회계법인/담보물배분표")
INPUT_PATH = Path( "datas/공장저당법목록.pdf")
OUTPUT_ROOT = Path( "output/mortgage_list_luna_ver")
FINAL_JSON_PATH = OUTPUT_ROOT / "공장저당법목록.json"

start_time = time.perf_counter()
parser = MortgageParser(section_llm=gpt_mini, extraction_llm=gpt_sol)
mortgage_list_result = parser.invoke(INPUT_PATH, OUTPUT_ROOT, json_path=FINAL_JSON_PATH)
end_time = time.perf_counter()
print(f"실행 시간: {end_time - start_time:.3f}초")
print("# =================================================== #")
