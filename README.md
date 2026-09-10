# AEGIS

AEGIS is an autonomous multi-agent disaster response system. Its agents predict hazard spread, allocate resources, negotiate between competing priorities, and brief a human operator — all while the process is visible live.

Built on LangGraph for orchestration, with Groq or a local Ollama runtime as the LLM provider, a FastAPI backend, and a Next.js frontend. No paid APIs are required to run it: everything works with a free-tier Groq key or a fully local Ollama model.

## Monorepo layout

```
/backend    FastAPI application
/frontend   Next.js (TypeScript) application
```

## Running

- Backend: `cd backend && pip install -r requirements.txt && uvicorn main:app --reload`
- Frontend: `cd frontend && npm install && npm run dev`