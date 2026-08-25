# Builds the frontend, then packages it with the FastAPI backend into a
# single image that serves both from one port. Host is not chosen yet
# (see project notes) — this just makes "docker run" the same regardless of
# which host ends up running it.

FROM node:22-slim AS frontend-build
# Route npm through the company's internal Nexus proxy rather than public
# npm directly -- `npm ci` against the public registry has failed twice in a
# row here with npm's own generic "Exit handler never called!" bug, which
# surfaces under flaky/slow registry connections; this is the same
# public-registry-reliability issue the pip install below already hit.
ARG NPM_CONFIG_REGISTRY=http://wtu-repo-prod.wtmec.com:8081/repository/npm-public/
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
# No `apt-get install ffmpeg` here: this platform's build servers can reach
# the pip package index (same path the `pip install` below already uses)
# but can't reach deb.debian.org to install OS packages via apt — that build
# consistently fails with "Connection failed" against the Debian mirror.
# `static-ffmpeg` (declared in pyproject.toml) provides `ffmpeg`/`ffprobe` via
# a pip-downloaded static build instead, sidestepping apt entirely. One
# side effect: without the `fonts-noto-cjk` system package, libass falls back
# to a substitute font per-glyph for the subtitle burn-in style that names
# "Noto Sans CJK TC" explicitly — still renders correctly, just not in the
# intended typeface.
WORKDIR /app
# Route pip through the company's internal Nexus proxy rather than public
# PyPI directly -- faster, and not subject to whatever intermittently made
# a plain `pip install` fail to resolve even its own build backend
# (setuptools) with "No matching distribution found (from versions: none)".
ARG PIP_INDEX_URL=http://wtu-repo-prod.wtmec.com:8081/repository/pypi-proxy/simple
ARG PIP_TRUSTED_HOST=wtu-repo-prod.wtmec.com:8081
COPY pyproject.toml ./
COPY src/ ./src/
# --timeout 100: pip's 15s default read timeout was too short for a large
# wheel (pymupdf) the first time Nexus's proxy cache has to fetch it
# upstream rather than serve a cached copy -- hit twice in earlier attempts.
RUN pip install --no-cache-dir --timeout 100 .

# NOT pre-fetched here: static-ffmpeg's actual ffmpeg/ffprobe binaries come
# from a GitHub raw-content download outside Nexus, and doing that at *build*
# time here got its TLS connection reset immediately, every attempt -- this
# platform's build servers only reliably reach the internal Nexus proxy and
# GitLab, not arbitrary external hosts (same reason apt-get couldn't reach
# deb.debian.org earlier). The *running container*, unlike the build server,
# can reach the internet -- so ppt2course/server.py calls
# `static_ffmpeg.add_paths()` once at import time instead, fetching these on
# the container's first startup rather than during this build.

COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# PPT2COURSE_DATA_ROOT points at /opt/appdata -- the platform's persistent
# storage mount (declared via deploy_config.yml's persistent_storage: true)
# -- so uploaded decks and rendered videos survive a redeploy instead of
# being reset to whatever was last committed to the repo.
ENV PPT2COURSE_FRONTEND_DIST=/app/frontend/dist \
    PPT2COURSE_DATA_ROOT=/opt/appdata/jobs

EXPOSE 8000
CMD ["uvicorn", "ppt2course.server:app", "--host", "0.0.0.0", "--port", "8000"]
