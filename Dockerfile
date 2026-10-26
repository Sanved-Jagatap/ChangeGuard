FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY benchmarks ./benchmarks
RUN pip install -e ".[api,postgres,ui]"
EXPOSE 8000 8501
# default: API. docker-compose overrides the command for the UI.
CMD ["sh", "-c", "changeguard init-db && uvicorn changeguard.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
