import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

sys.path.append("/Users/yujin/Desktop/성현회계법인/담보물배분표")
from appraisal.parser import AppraisalParser

load_dotenv("/Users/yujin/Desktop/성현회계법인/담보물배분표/appraisal/.env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

input_path = Path("/Users/yujin/Desktop/성현회계법인/담보물배분표/datas/감정평가서 휴먼중공업.pdf")
output_root = Path("/Users/yujin/Desktop/성현회계법인/담보물배분표/output/appraisal")

section_llm = ChatOpenAI(
    model="gpt-5.4-mini",
    api_key=OPENAI_API_KEY,
    temperature=0,
    use_responses_api=True,
)

extraction_llm = ChatOpenAI(
    model="gpt-5.6-sol",
    api_key=OPENAI_API_KEY,
    temperature=0,
    use_responses_api=True,
    max_retries=3,
)

parser = AppraisalParser(section_llm=section_llm, extraction_llm=extraction_llm)
result = parser.invoke(source_path=input_path, output_root=output_root)

print(f"split_dir={result.split_dir}")
print(f"land_building_pdf={result.land_building_pdf}")
print(f"machinery_pdf={result.machinery_pdf}")
print(f"json_path={result.json_path}")
print(f"land_groups={len(result.data.land)}")
print(f"building_groups={len(result.data.building)}")
print(f"machinery_items={len(result.data.machinery)}")
