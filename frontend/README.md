# PPT2Course AI — frontend

React + Vite single-page app for the PPT2Course AI web layer. Talks to the
FastAPI backend in `../src/ppt2course/server.py` over `VITE_API_BASE_URL`
(see `.env.example`, defaults to `http://localhost:8000`).

## Run locally

Backend (from the repo root, with the project venv active):

```
uvicorn ppt2course.server:app --host 0.0.0.0 --port 8000
```

Frontend:

```
npm install
npm run dev
```

`npm run build` produces a static `dist/` you can serve from any static host
or from FastAPI itself.
