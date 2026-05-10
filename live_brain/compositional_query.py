"""
Compositional Queries - Feature 6
Algebraic composition for memory search (A + B - C).
"""
import json
import logging
import sqlite3
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    logger.warning("[compositional_query] numpy not available, using simple vectors")


class CompositionEngine:
    """Manages compositional queries with vector operations."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.dimension = 128

    def get_or_create_vector(self, concept: str) -> List[float]:
        """Get concept vector or create if doesn't exist."""
        # Check if vector exists
        row = self.conn.execute(
            "SELECT vector_json FROM concept_vectors WHERE concept_name = ?",
            (concept.lower(),)
        ).fetchone()

        if row:
            return json.loads(row[0])

        # Create new vector from concept text
        vector = self.build_concept_vector(concept)
        now = time.time()
        concept_id = f"concept:{concept.lower()}"

        self.conn.execute(
            """INSERT OR REPLACE INTO concept_vectors
               (concept_id, concept_name, vector_json, dimension, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (concept_id, concept.lower(), json.dumps(vector), self.dimension, now, now)
        )
        self.conn.commit()

        return vector

    def compose(
        self,
        add_concepts: List[str],
        subtract_concepts: Optional[List[str]] = None
    ) -> List[float]:
        """
        Compose vectors: (A + B + C) - (D + E)

        Example: compose(['ffmpeg', 'video'], ['audio'])
        Returns vector representing "video processing without audio"
        """
        subtract_concepts = subtract_concepts or []

        # Get vectors for all concepts
        add_vectors = [self.get_or_create_vector(c) for c in add_concepts]
        subtract_vectors = [self.get_or_create_vector(c) for c in subtract_concepts]

        # Compose: sum of add vectors minus sum of subtract vectors
        result = [0.0] * self.dimension

        for vec in add_vectors:
            for i in range(self.dimension):
                result[i] += vec[i]

        for vec in subtract_vectors:
            for i in range(self.dimension):
                result[i] -= vec[i]

        # Normalize
        magnitude = sum(x * x for x in result) ** 0.5
        if magnitude > 0:
            result = [x / magnitude for x in result]

        return result

    def find_similar(
        self,
        query_vector: List[float],
        top_k: int = 5,
        threshold: float = 0.5
    ) -> List[Dict]:
        """Find concepts/memories similar to query vector."""
        # Get all concept vectors
        rows = self.conn.execute(
            "SELECT concept_name, vector_json FROM concept_vectors"
        ).fetchall()

        similarities = []
        for row in rows:
            concept_name = row[0]
            vector = json.loads(row[1])

            # Cosine similarity
            similarity = self._cosine_similarity(query_vector, vector)

            if similarity >= threshold:
                similarities.append({
                    'concept': concept_name,
                    'similarity': similarity
                })

        # Sort by similarity
        similarities.sort(key=lambda x: x['similarity'], reverse=True)
        return similarities[:top_k]

    def build_concept_vector(self, text: str) -> List[float]:
        """
        Build vector from text using simple term frequency.
        In production, could use embeddings.
        """
        # Simple term frequency vector
        words = text.lower().split()
        vector = [0.0] * self.dimension

        for i, word in enumerate(words):
            # Hash word to dimension index
            hash_val = hash(word) % self.dimension
            vector[hash_val] += 1.0

        # Normalize
        magnitude = sum(x * x for x in vector) ** 0.5
        if magnitude > 0:
            vector = [x / magnitude for x in vector]

        return vector

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        return max(0.0, min(1.0, dot_product))
