import logging
import math

from app.config import get_settings
from app.services.google_auth import build_genai_client
from app.services.text import jaccard_similarity

settings = get_settings()
logger = logging.getLogger(__name__)


class HeadingSimilarity:
    """Semantic heading similarity with a deterministic fallback.

    Vertex/API-key embedding calls are used only when AI is enabled. Each unique
    heading is embedded once per worker process. If embedding auth/model/network
    fails, the existing lexical Jaccard score remains available.
    """

    def __init__(self):
        self.client = None
        self.vector_cache: dict[str, list[float] | None] = {}
        if settings.ai_mode.strip().lower() in {"vertex", "api_key"}:
            try:
                self.client = build_genai_client()
            except Exception as exc:
                logger.warning("Embedding client unavailable; using lexical fallback: %s", exc)
                self.client = None

    def _vector(self, text: str) -> list[float] | None:
        if text in self.vector_cache:
            return self.vector_cache[text]
        if not self.client:
            self.vector_cache[text] = None
            return None
        try:
            from google.genai import types

            # Gemini Embedding 2 uses custom task instructions in the content;
            # older gemini-embedding-001 supports task_type in EmbedContentConfig.
            if settings.embedding_model == "gemini-embedding-2":
                response = self.client.models.embed_content(
                    model=settings.embedding_model,
                    contents=f"task: semantic similarity | text: {text}",
                    config=types.EmbedContentConfig(
                        output_dimensionality=settings.embedding_dimension,
                    ),
                )
            else:
                response = self.client.models.embed_content(
                    model=settings.embedding_model,
                    contents=text,
                    config=types.EmbedContentConfig(
                        task_type="SEMANTIC_SIMILARITY",
                        output_dimensionality=settings.embedding_dimension,
                    ),
                )
            embeddings = response.embeddings or []
            vec = embeddings[0].values if embeddings else None
            self.vector_cache[text] = vec
            return vec
        except Exception as exc:
            logger.warning("Embedding failed for one heading; lexical fallback used: %s", exc)
            self.vector_cache[text] = None
            return None

    def similarity(self, a: str, b: str) -> float:
        token_score = jaccard_similarity(a, b)
        if token_score >= 0.8:
            return token_score
        va, vb = self._vector(a), self._vector(b)
        if not va or not vb:
            return token_score
        dot = sum(x * y for x, y in zip(va, vb))
        na = math.sqrt(sum(x * x for x in va))
        nb = math.sqrt(sum(y * y for y in vb))
        return dot / (na * nb) if na and nb else token_score
