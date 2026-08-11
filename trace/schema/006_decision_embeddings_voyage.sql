-- 006: decision embeddings for Voyage (Gap #3 B3). voyage-3.5 outputs 1024 dims, so the
-- decision_embeddings vector column (created at 1536 in 003) moves to 1024. Safe on an
-- empty table (the RAG layer is greenfield). Adds the cosine ivfflat index used by
-- retrieval (B4). See docs/decision_memory_design.md.

ALTER TABLE decision_embeddings ALTER COLUMN embedding TYPE vector(1024);

CREATE INDEX IF NOT EXISTS decision_embeddings_ivf
    ON decision_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
