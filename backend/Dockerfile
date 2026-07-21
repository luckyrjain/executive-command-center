FROM python:3.14.6-alpine
WORKDIR /app
# The python:3.14.6-alpine tag already resolves to the newest published
# alpine build, but Alpine ships OS-package security fixes via its apk
# repository ahead of new base-image tags/rebuilds -- pull those in
# explicitly (mirrors frontend/Dockerfile's production stage).
RUN apk update && apk upgrade --no-cache
RUN addgroup -S ecc && adduser -S -G ecc -h /app ecc && chown ecc:ecc /app
RUN pip install --no-cache-dir uv==0.7.19
COPY --chown=ecc:ecc pyproject.toml uv.lock ./
# Run as the non-root user before `uv sync` (not just the later `COPY
# --chown`), so /app/.venv is created ecc-owned in the first place rather
# than needing a separate chown pass afterward -- avoids an image where
# .venv is root-owned but everything else under /app is ecc-owned. HOME is
# set explicitly rather than relying on `adduser -h`/passwd-lookup fallback
# for it, since Docker's USER directive alone does not update $HOME for
# subsequent RUN steps -- without this, uv's cache/venv resolution could
# still try (and fail without permission) to write under the inherited
# root HOME instead of /app.
ENV HOME=/app
USER ecc
RUN uv sync --frozen --no-dev
COPY --chown=ecc:ecc backend ./backend
ENV PYTHONPATH=/app/backend
CMD ["/app/.venv/bin/uvicorn", "ecc.main:app", "--host", "0.0.0.0", "--port", "8000"]