FROM python:3.12.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
    -r requirements.txt

# The worker imports the API's ingestion/retrieval modules directly so there is
# exactly one implementation of chunking and vector storage.
COPY api/app ./app
COPY worker ./worker

CMD ["arq", "worker.settings.WorkerSettings"]
