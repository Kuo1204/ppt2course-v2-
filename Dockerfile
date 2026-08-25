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

# Pre-fetch the ffmpeg/ffprobe binaries at build time (rather than lazily on
# a user's first request) so they're baked into this image layer, and so a
# broken download path fails the build immediately with a clear error
# instead of failing silently in production later. static-ffmpeg's own API
# only returns the downloaded binaries' paths -- symlink them onto PATH so
# every existing `subprocess.run(["ffmpeg", ...])`/`["ffprobe", ...])` call
# in this codebase keeps working unchanged.
RUN python -c "import os; from static_ffmpeg.run import get_or_fetch_platform_executables_else_raise; ffmpeg_path, ffprobe_path = get_or_fetch_platform_executables_else_raise(); os.symlink(ffmpeg_path, '/usr/local/bin/ffmpeg'); os.symlink(ffprobe_path, '/usr/local/bin/ffprobe')" \
    && ffmpeg -version && ffprobe -version

COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# PPT2COURSE_DATA_ROOT points at /opt/appdata -- the platform's persistent
# storage mount (declared via deploy_config.yml's persistent_storage: true)
# -- so uploaded decks and rendered videos survive a redeploy instead of
# being reset to whatever was last committed to the repo.
ENV PPT2COURSE_FRONTEND_DIST=/app/frontend/dist \
    PPT2COURSE_DATA_ROOT=/opt/appdata/jobs

EXPOSE 8000
CMD ["uvicorn", "ppt2course.server:app", "--host", "0.0.0.0", "--port", "8000"]
