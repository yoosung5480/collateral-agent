# LINTER
단순 실행성공이 아니라 요구조건과 규약을 맞췄는지 파악해야한다.

## 목표사항
1. 요구사항 "의미론적" 성공
2. "human readable" 코드를 

## 그 외 주의사항
- OPENAI_API_KEY 사용, llm 호출없이 smoke test는 실전에서 아무런 쓸모없는 시스템 실패이다. 반드시 llm을 쓰는데선, llm이 사용돼야한다.
- 최신 LangChain문법을 통한 코드 간소화
  - LangChain, ChatOpenAI, Pydantic 등을 통해서 간소화된 llm 호출및 사용 코드여야한다.
- 지나친 try-except 금지
- 지나친 함수 래핑 및 구현금지. 필요한 라이브러리나 프레임워크 활용을 최우선.