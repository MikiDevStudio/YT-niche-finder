"""Local, free multilingual embeddings for semantic niche search (no external API)."""
import numpy as np
from functools import lru_cache
from fastembed import TextEmbedding

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


@lru_cache(maxsize=1)
def _model():
    return TextEmbedding(model_name=MODEL_NAME)


def embed(texts):
    """texts: str or list[str] -> list of float32 np.ndarray (already normalized)."""
    single = isinstance(texts, str)
    if single:
        texts = [texts]
    vecs = list(_model().embed(texts))
    vecs = [np.asarray(v, dtype=np.float32) for v in vecs]
    return vecs[0] if single else vecs


def to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def cosine_matrix(queries, vecs) -> np.ndarray:
    """Every query against every vector at once: shape (len(queries), len(vecs)).

    check_ideas asks twenty phrases about a few thousand videos, which is a
    few tens of thousands of cosines -- one matrix product instead of a Python
    loop turns that from seconds into milliseconds. A zero-length vector keeps
    the 0.0 that `cosine` gives it, rather than a NaN spreading through the row.
    """
    if not len(queries) or not len(vecs):
        return np.zeros((len(queries), len(vecs)), dtype=np.float32)

    def _unit(rows):
        m = np.vstack([np.asarray(v, dtype=np.float32) for v in rows])
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.where(norms == 0, 1.0, norms)

    return _unit(queries) @ _unit(vecs).T
