FROM python:3.14.6-alpine
WORKDIR /app
# The python:3.14.6-alpine tag already resolves to the newest published
# alpine build, but Alpine ships OS-package security fixes via its apk
# repository ahead of new base-image tags/rebuilds -- pull those in
# explicitly (mirrors frontend/Dockerfile's production stage).
RUN apk update && apk upgrade --no-cache
RUN addgroup -S ecc && adduser -S -G ecc -h /app ecc
RUN pip install --no-cache-dir uv==0.7.19
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY --chown=ecc:ecc backend ./backend
ENV PYTHONPATH=/app/backend
USER ecc
CMD ["/app/.venv/bin/uvicorn", "ecc.main:app", "--host", "0.0.0.0", "--port", "8000"]