# Builds the frontend, then packages it with the FastAPI backend into a
# single image that serves both from one port. Host is not chosen yet
# (see project notes) — this just makes "docker run" the same regardless of
# which host ends up running it.

FROM node:22-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

COPY --from=frontend-build /app/frontend/dist ./frontend/dist

ENV PPT2COURSE_FRONTEND_DIST=/app/frontend/dist \
    PPT2COURSE_DATA_ROOT=/data/jobs
VOLUME ["/data/jobs"]

EXPOSE 8000
CMD ["uvicorn", "ppt2course.server:app", "--host", "0.0.0.0", "--port", "8000"]
