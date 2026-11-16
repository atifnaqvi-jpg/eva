FROM python:3.12.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/models

WORKDIR /srv

COPY worker/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
    -r requirements.txt

# The worker imports the API's ingestion/retrieval modules directly so there is
# exactly one implementation of chunking and vector storage.
COPY api/app ./app
# Copy the package itself, not the directory: `COPY worker ./worker` would nest
# it at /srv/worker/worker and `worker.settings` would not import.
COPY worker/worker ./worker

CMD ["arq", "worker.settings.WorkerSettings"]
