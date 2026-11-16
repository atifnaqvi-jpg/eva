"""Embedding service: one BGE-M3 instance, shared by ingestion and query.

Why a separate process instead of embedding inline:

* BGE-M3 is ~3.2 GiB resident (measured, PLAN 8.2). Giving it to the API and the
  worker separately would mean two copies — 6.4 GiB — for one model.
* Query embedding must use the *same* model as ingestion, or the vectors live in
  different spaces and ranking silently breaks. Pointing both at one service makes
  that impossible to get wrong.

The interface is deliberately tiny: ``POST /embed {"texts": [...]}`` returns
``{"model": ..., "dimension": ..., "vectors": [[...]]}``. Callers verify the model
key, so a misconfigured client fails loudly instead of comparing incomparable
vectors.
"""

import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "BAAI/bge-m3")
MODEL_KEY = os.environ.get("EMBEDDING_MODEL_KEY", "bge-m3")
BATCH_SIZE = int(os.environ.get("EMBEDDING_BATCH_SIZE", "8"))
MAX_BATCH = int(os.environ.get("EMBEDDING_MAX_BATCH", "64"))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Load eagerly: the first client request should not pay the ~30 s load.
    _load()
    yield


app = FastAPI(title="EVA embeddings", version="0.1.0", lifespan=lifespan)

_model = None
# Encoding is CPU-bound; a lock keeps concurrent requests from thrashing cores and
# inflating memory beyond the container budget.
_lock = threading.Lock()


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_BATCH)


class EmbedResponse(BaseModel):
    model: str
    dimension: int
    vectors: list[list[float]]


def _load():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        logger.info("loading %s", MODEL_NAME)
        _model = SentenceTransformer(MODEL_NAME, device="cpu")
        logger.info("loaded %s (%d dims)", MODEL_NAME, _model.get_sentence_embedding_dimension())
    return _model


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "model": MODEL_KEY, "loaded": _model is not None}


@app.post("/embed", response_model=EmbedResponse)
def embed(request: EmbedRequest) -> EmbedResponse:
    if any(not t.strip() for t in request.texts):
        raise HTTPException(status_code=422, detail="texts must not be blank")
    model = _load()
    with _lock:
        vectors = model.encode(
            request.texts,
            batch_size=BATCH_SIZE,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
    return EmbedResponse(
        model=MODEL_KEY,
        dimension=int(model.get_sentence_embedding_dimension()),
        vectors=[v.tolist() for v in vectors],
    )
