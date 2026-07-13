FROM python:3.14.5-slim
WORKDIR /app
RUN pip install --no-cache-dir uv==0.7.19
COPY pyproject.toml ./
RUN uv sync --no-dev
COPY backend ./backend
ENV PYTHONPATH=/app/backend
CMD ["uv", "run", "uvicorn", "ecc.main:app", "--host", "0.0.0.0", "--port", "8000"]
