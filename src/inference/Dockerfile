# API de inferencia (new_arch.md §4). Sin credenciales: en EC2 las entrega el instance profile.
#
# Uso (desde la raíz del repo):
#   docker build --platform linux/amd64 -f src/inference/Dockerfile -t smartestate-inference .
#   docker run --rm -p 8000:8000 \
#     -e RUN_ID=<run_id> -e RUNS_ROOT=s3://<lake>/runs -e AWS_REGION=us-east-1 smartestate-inference

# Mismo Python que el cluster que construye el GeoIndex: joblib lo carga con esta imagen.
FROM python:3.12.3-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dependencias primero: esta capa queda en caché y solo se rehace si cambian las versiones.
COPY src/inference/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Solo el código que importa la API. GeoIndex debe estar en la misma ruta en que se guardó.
WORKDIR /app
COPY src/__init__.py src/__init__.py
COPY src/common/__init__.py src/common/geo_index.py src/common/
COPY src/ml_pipeline/__init__.py src/ml_pipeline/materialization.py src/ml_pipeline/
COPY src/inference/__init__.py src/inference/api.py src/inference/artifacts.py src/inference/online_store.py src/inference/recommender.py src/inference/

RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8000

# start-period: cubre la carga de artefactos desde S3 al arrancar.
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

# 2 processes: the t3.small has 2 vCPU and Python uses one per process (docs/performance_report.md).
CMD ["uvicorn", "src.inference.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
