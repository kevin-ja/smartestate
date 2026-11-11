# Tests de Spark en un contenedor, sin instalar Java ni Spark en la Mac (new_arch.md §3.1, "Dos puertas").
# Mismo Python y Spark que el cluster (DBR 18 LTS: Python 3.12.3, Spark 4.1.0).
#
# Uso (desde la raíz del repo):
#   docker build -f tests/Dockerfile -t smartestate-tests .
#   docker run --rm smartestate-tests

FROM python:3.12.3-slim-bookworm

# Spark 4.1 necesita Java 17. pyspark lo encuentra en el PATH.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-17-jre-headless \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dependencias primero: esta capa queda en caché y solo se rehace si cambian las versiones.
# numpy/pandas/pyarrow/scikit-learn/joblib/boto3: mismas versiones que requirements.txt.
# boto3 trae botocore y urllib3, que importa src/inference/online_store.py.
RUN pip install --no-cache-dir \
    pyspark==4.1.0 \
    pytest==9.1.1 \
    numpy==2.5.3 \
    pandas==3.0.5 \
    pyarrow==25.0.1 \
    scikit-learn==1.9.1 \
    joblib==1.6.0 \
    boto3==1.43.124

# Solo el código que se evalúa.
WORKDIR /app
COPY src/ src/
COPY tests/ tests/

CMD ["python", "-m", "pytest", "tests", "-v", "-p", "no:cacheprovider"]
