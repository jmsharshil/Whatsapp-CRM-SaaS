import json
import numpy as np
from pathlib import Path
from django.conf import settings

_embedder = None
_faiss    = None
_index    = None      # ← NEW: cache in memory
_metadata = None      # ← NEW: cache in memory


def _get_embedder():
    global _embedder
    if _embedder is None:
        import time
        print("\n[TIMING] Loading embedding model...")
        t = time.time()
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        elapsed = time.time() - t
        print(f"[TIMING] Model loaded in {elapsed:.2f}s\n")
    return _embedder


def _get_faiss():
    global _faiss
    if _faiss is None:
        import faiss
        _faiss = faiss
    return _faiss


def _get_index_and_meta():
    """Load FAISS index + metadata once, cache in memory forever."""
    global _index, _metadata
    if _index is None or _metadata is None:
        faiss     = _get_faiss()
        index_dir = Path(settings.FAISS_INDEX_DIR)
        index_path = index_dir / "index.faiss"
        meta_path  = index_dir / "metadata.json"

        if not index_path.exists():
            return None, []

        _index = faiss.read_index(str(index_path))
        with open(meta_path) as f:
            _metadata = json.load(f)

    return _index, _metadata


def invalidate_index_cache():
    """Call this after build_index() so next request reloads fresh index."""
    global _index, _metadata
    _index    = None
    _metadata = None


# ── Chunk text ────────────────────────────────────────────────────────────────
def chunk_text(text: str, chunk_size=500, overlap=50) -> list:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i:i + chunk_size]))
        i += chunk_size - overlap
    return chunks


# ── Build FAISS index ─────────────────────────────────────────────────────────
def build_index(documents: list):
    faiss     = _get_faiss()
    embedder  = _get_embedder()
    index_dir = Path(settings.FAISS_INDEX_DIR)
    index_dir.mkdir(parents=True, exist_ok=True)

    all_chunks, metadata = [], []
    for doc in documents:
        for chunk in chunk_text(doc["content"]):
            all_chunks.append(chunk)
            metadata.append({
                "doc_id":   doc["doc_id"],
                "doc_name": doc["name"],
                "text":     chunk
            })

    if not all_chunks:
        return

    embeddings = embedder.encode(
        all_chunks, convert_to_numpy=True, show_progress_bar=False
    ).astype("float32")
    faiss.normalize_L2(embeddings)

    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)

    faiss.write_index(index, str(index_dir / "index.faiss"))
    with open(index_dir / "metadata.json", "w") as f:
        json.dump(metadata, f)

    invalidate_index_cache()  # ← flush cache so next call reloads fresh


# ── Semantic search ───────────────────────────────────────────────────────────
def semantic_search(query: str, top_k=5) -> list:
    import time
    t_start = time.time()
    
    faiss         = _get_faiss()
    embedder      = _get_embedder()
    index, metadata = _get_index_and_meta()  # ← from memory, not disk

    if index is None:
        print("[TIMING] No FAISS index found")
        return []

    t_embed = time.time()
    q_emb = embedder.encode(
        [query], convert_to_numpy=True, show_progress_bar=False
    ).astype("float32")
    embed_time = time.time()-t_embed
    print(f"[TIMING]   - Query embedding: {embed_time:.3f}s")
    
    faiss.normalize_L2(q_emb)

    scores, indices = index.search(q_emb, top_k)
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        item = metadata[idx].copy()
        item["score"] = float(score)
        results.append(item)
    
    total_time = time.time()-t_start
    print(f"[TIMING] Total semantic_search: {total_time:.3f}s")
    return results


# ── Keyword search ────────────────────────────────────────────────────────────
def keyword_search(documents: list, query: str, max_chars=6000) -> str:
    q_words = [w for w in query.lower().split() if len(w) > 3]
    if not q_words:
        return ""

    scored = sorted(
        documents,
        key=lambda d: sum(d["content"].lower().count(w) for w in q_words),
        reverse=True
    )
    context, total = "", 0
    for doc in scored:
        if total >= max_chars:
            break
        chunk = doc["content"][:6000]
        context += f"\n\n=== {doc['name']} ===\n{chunk}"
        total += len(chunk)
    return context


# ── Combined retrieval ────────────────────────────────────────────────────────
def get_context(query: str, documents: list) -> str:
    import time
    
    t_start = time.time()
    print(f"\n[TIMING] START get_context for query: '{query[:50]}...'")
    
    # Cache results to avoid recomputing embeddings for same query  
    try:
        from django.core.cache import cache
        import hashlib
        cache_key = f"ctx_{hashlib.md5(query.encode()).hexdigest()}"
        cached = cache.get(cache_key)
        if cached:  # Only use cache if it has content
            elapsed = time.time()-t_start
            print(f"[TIMING] Context from cache: {elapsed:.3f}s\n")
            return cached
    except Exception as e:
        print(f"[TIMING] Cache miss (error: {e})")
    
    t_sem = time.time()
    sem_results = semantic_search(query, top_k=5)
    t_sem_done = time.time()
    sem_elapsed = t_sem_done-t_sem
    print(f"[TIMING] Semantic search: {sem_elapsed:.3f}s ({len(sem_results)} results)")
    
    sem_text = ""
    if sem_results:
        seen = set()
        for r in sem_results:
            key = r["text"][:80]
            if key not in seen:
                sem_text += f"\n[{r['doc_name']}]\n{r['text']}\n"
                seen.add(key)

    t_kw = time.time()
    kw_text = keyword_search(documents, query)
    t_kw_done = time.time()
    kw_elapsed = t_kw_done-t_kw
    print(f"[TIMING] Keyword search: {kw_elapsed:.3f}s")

    combined = ""
    if sem_text:
        combined += "## Semantic Matches\n" + sem_text
    if kw_text:
        combined += "\n## Keyword Matches\n" + kw_text
    
    result = combined.strip()
    
    # Try to cache the result
    try:
        cache.set(cache_key, result, timeout=600)
    except:
        pass
    
    total_elapsed = time.time()-t_start
    print(f"[TIMING] Total get_context: {total_elapsed:.3f}s\n")
    return result