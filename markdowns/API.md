### 서버 실행

- `ipconfig getifaddr en0` : wifi에서 내 IP검색
  - 192.168.2.29

프로젝트 루트에서:

conda activate langchain
python -m web_service.server

또는:

uvicorn web_service.server:app --host 0.0.0.0 --port 8000

접속:

http://127.0.0.1:8000/

http://192.168.2.52:8000

개발자 화면:

http://127.0.0.1:8000/developer

### 주요 API

GET  /api/health
POST /api/jobs
GET  /api/jobs/{job_id}
GET  /api/jobs/{job_id}/download
POST /api/jobs/{job_id}/rag/build
POST /api/jobs/{job_id}/chat
GET  /api/dashboard
GET  /api/developer/dashboard