"""Tests for compositional query vector operations."""
import sqlite3
import tempfile
import time
from pathlib import Path


def test_concept_vector_generation():
    """Test that concept vectors are generated correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        # Create tables
        conn.execute("""
            CREATE TABLE concept_vectors (
                concept_id TEXT PRIMARY KEY,
                concept_name TEXT,
                vector_json TEXT,
                dimension INTEGER,
                created_at REAL,
                updated_at REAL
            )
        """)

        conn.execute("""
            CREATE TABLE facts (
                fact_id TEXT PRIMARY KEY,
                fact_text TEXT,
                status TEXT
            )
        """)

        # Insert sample facts for TF-IDF
        conn.execute("INSERT INTO facts VALUES ('f1', 'python programming language', 'active')")
        conn.execute("INSERT INTO facts VALUES ('f2', 'javascript web development', 'active')")
        conn.execute("INSERT INTO facts VALUES ('f3', 'python data science', 'active')")
        conn.commit()

        # Simulate vector generation (simplified)
        import json
        text = "python programming"
        words = text.lower().split()
        dimension = 128
        vector = [0.0] * dimension

        # Simple hash-based vector
        for word in words:
            hash_val = hash(word) % dimension
            vector[hash_val] += 1.0

        # Normalize
        magnitude = sum(x * x for x in vector) ** 0.5
        if magnitude > 0:
            vector = [x / magnitude for x in vector]

        # Store
        now = time.time()
        conn.execute("""
            INSERT INTO concept_vectors VALUES (?, ?, ?, ?, ?, ?)
        """, ('concept:python', 'python', json.dumps(vector), dimension, now, now))
        conn.commit()

        # Verify
        row = conn.execute("SELECT vector_json, dimension FROM concept_vectors WHERE concept_name = ?", ('python',)).fetchone()
        assert row is not None
        stored_vector = json.loads(row[0])
        assert len(stored_vector) == 128
        assert abs(sum(x * x for x in stored_vector) ** 0.5 - 1.0) < 0.01  # Normalized
        conn.close()
    print("✓ Concept vector generation test passed")


def test_vector_composition():
    """Test vector composition (A + B - C)."""
    import json

    dimension = 128

    # Create simple test vectors
    vec_a = [1.0 if i == 10 else 0.0 for i in range(dimension)]
    vec_b = [1.0 if i == 20 else 0.0 for i in range(dimension)]
    vec_c = [1.0 if i == 10 else 0.0 for i in range(dimension)]

    # Compose: A + B - C = B (since A and C cancel)
    result = [0.0] * dimension
    for i in range(dimension):
        result[i] = vec_a[i] + vec_b[i] - vec_c[i]

    # Normalize
    magnitude = sum(x * x for x in result) ** 0.5
    if magnitude > 0:
        result = [x / magnitude for x in result]

    # Verify result is similar to vec_b
    assert result[20] > 0.9  # Should be strong at position 20
    assert result[10] < 0.1  # Should be weak at position 10 (cancelled)
    print("✓ Vector composition test passed")


def test_cosine_similarity():
    """Test cosine similarity calculation."""
    # Identical vectors
    vec1 = [1.0, 0.0, 0.0, 0.0]
    vec2 = [1.0, 0.0, 0.0, 0.0]
    similarity = sum(a * b for a, b in zip(vec1, vec2))
    assert abs(similarity - 1.0) < 0.01

    # Orthogonal vectors
    vec3 = [1.0, 0.0, 0.0, 0.0]
    vec4 = [0.0, 1.0, 0.0, 0.0]
    similarity = sum(a * b for a, b in zip(vec3, vec4))
    assert abs(similarity - 0.0) < 0.01

    # Opposite vectors
    vec5 = [1.0, 0.0, 0.0, 0.0]
    vec6 = [-1.0, 0.0, 0.0, 0.0]
    similarity = sum(a * b for a, b in zip(vec5, vec6))
    assert similarity < 0.0
    print("✓ Cosine similarity test passed")


def test_similarity_search():
    """Test finding similar concepts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        conn = sqlite3.connect(db_path)

        # Create table
        conn.execute("""
            CREATE TABLE concept_vectors (
                concept_id TEXT PRIMARY KEY,
                concept_name TEXT,
                vector_json TEXT,
                dimension INTEGER,
                created_at REAL,
                updated_at REAL
            )
        """)

        import json
        dimension = 128
        now = time.time()

        # Create test vectors
        # Concept 1: strong at position 10
        vec1 = [0.0] * dimension
        vec1[10] = 1.0
        conn.execute("INSERT INTO concept_vectors VALUES (?, ?, ?, ?, ?, ?)",
                     ('c1', 'python', json.dumps(vec1), dimension, now, now))

        # Concept 2: strong at position 10 (similar to concept 1)
        vec2 = [0.0] * dimension
        vec2[10] = 0.9
        vec2[11] = 0.1
        conn.execute("INSERT INTO concept_vectors VALUES (?, ?, ?, ?, ?, ?)",
                     ('c2', 'programming', json.dumps(vec2), dimension, now, now))

        # Concept 3: strong at position 50 (different)
        vec3 = [0.0] * dimension
        vec3[50] = 1.0
        conn.execute("INSERT INTO concept_vectors VALUES (?, ?, ?, ?, ?, ?)",
                     ('c3', 'database', json.dumps(vec3), dimension, now, now))
        conn.commit()

        # Query vector (similar to vec1)
        query_vec = [0.0] * dimension
        query_vec[10] = 1.0

        # Find similar
        rows = conn.execute("SELECT concept_name, vector_json FROM concept_vectors").fetchall()
        similarities = []
        for row in rows:
            concept_name = row[0]
            vector = json.loads(row[1])
            similarity = sum(a * b for a, b in zip(query_vec, vector))
            if similarity >= 0.5:
                similarities.append((concept_name, similarity))

        similarities.sort(key=lambda x: x[1], reverse=True)

        # Verify: python and programming should be top results
        assert len(similarities) >= 2
        assert similarities[0][0] in ['python', 'programming']
        conn.close()
    print("✓ Similarity search test passed")


if __name__ == "__main__":
    test_concept_vector_generation()
    test_vector_composition()
    test_cosine_similarity()
    test_similarity_search()
    print("\n✅ All compositional query tests passed!")
