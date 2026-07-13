FROM python:3.14.6-slim
WORKDIR /app
RUN groupadd --system ecc && useradd --system --gid ecc --home-dir /app ecc
RUN pip install --no-cache-dir uv==0.7.19
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY --chown=ecc:ecc backend ./backend
ENV PYTHONPATH=/app/backend
USER ecc
CMD ["uv", "run", "uvicorn", "ecc.main:app", "--host", "0.0.0.0", "--port", "8000"]