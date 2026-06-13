"""Knowledge-base search tools backed by Redis (RediSearch).

kb_search_bm25: full-text BM25 search (OR-semantics keyword query).
kb_search_vector: HNSW vector search over gemini-embedding-001 embeddings.
kb_search_hybrid: Reciprocal Rank Fusion of BM25 + vector (use this by default).

All tools accept an optional `category` parameter to scope results to a
specific product line (e.g. "checking_accounts", "credit_cards").

Replies are parsed via execute_command so both the classic array reply and
the Redis 8 map-style reply work regardless of redis-py version."""

import os
import re
import struct

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
KB_INDEX = "kb_idx"
DOC_PREFIX = "doc:"
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 768

_client = redis.Redis.from_url(REDIS_URL, decode_responses=False)
_genai_client = None

# Known top-level categories (longer prefixes first to avoid prefix conflicts).
KNOWN_CATEGORIES = [
    "business_checking_accounts",
    "business_savings_accounts",
    "business_credit_cards",
    "buy_now_pay_later",
    "checking_accounts",
    "savings_accounts",
    "credit_cards",
    "bank_accounts",
]


def extract_category(doc_id: str) -> str:
    """Return top-level category for a doc_id, e.g. 'checking_accounts'."""
    name = doc_id.removeprefix("doc_")
    for cat in KNOWN_CATEGORIES:
        if name.startswith(cat):
            return cat
    return "general"


def _get_genai_client():
    """Reused genai client (one connection pool, not a new one per search)."""
    global _genai_client
    if _genai_client is None:
        from google import genai

        _genai_client = genai.Client()
    return _genai_client


def _embed(texts: list[str]) -> list[list[float]]:
    """Embed texts with gemini-embedding-001 via google-genai."""
    from google.genai import types

    # Reduced-dim output is unnormalized; the index uses COSINE, so that's fine.
    result = _get_genai_client().models.embed_content(
        model=EMBEDDING_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
    )
    return [e.values for e in result.embeddings]


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _parse_search_reply(reply) -> list[dict]:
    """Normalize an FT.SEARCH reply (array or map shape) to result dicts."""
    if isinstance(reply, dict):
        results = reply.get(b"results", reply.get("results")) or []
        out = []
        for row in results:
            attrs = row.get(b"extra_attributes", row.get("extra_attributes")) or {}
            doc = {"doc_id": _decode(row.get(b"id", row.get("id", "")))}
            doc.update({_decode(k): _decode(v) for k, v in attrs.items()})
            out.append(doc)
        return out
    out = []
    for i in range(1, len(reply) - 1, 2):
        doc = {"doc_id": _decode(reply[i])}
        fields = reply[i + 1]
        for j in range(0, len(fields) - 1, 2):
            doc[_decode(fields[j])] = _decode(fields[j + 1])
        out.append(doc)
    return out


def _strip_score(docs: list[dict]) -> list[dict]:
    for doc in docs:
        doc.pop("score", None)
    return docs


def _category_filter(category: str | None) -> str:
    """Build a RediSearch TAG filter clause, or return empty string."""
    if not category:
        return ""
    # Escape RediSearch TAG special characters
    safe = re.sub(r"([{}\[\]\\|&!()@\"'])", r"\\\1", category)
    return f"@category:{{{safe}}} "


def kb_search_bm25(query: str, top_k: int = 12, category: str | None = None) -> list[dict]:
    """Full-text (BM25) keyword search over the Rho-Bank knowledge base.

    Args:
        query: Keywords or a short phrase. OR-joined so extra keywords help.
        top_k: Number of documents to return (default 12).
        category: Optional product-line filter. Values: "checking_accounts",
            "savings_accounts", "credit_cards", "business_checking_accounts",
            "business_savings_accounts", "business_credit_cards",
            "buy_now_pay_later", "bank_accounts". Filters to that line only.

    Returns:
        Matching documents with doc_id, title, and full content.
    """
    terms = re.findall(r"\w+", query.lower())
    if not terms:
        return []
    or_query = "|".join(dict.fromkeys(terms))
    filt = _category_filter(category)
    full_query = f"{filt}({or_query})" if filt else or_query
    reply = _client.execute_command(
        "FT.SEARCH", KB_INDEX, full_query,
        "LIMIT", "0", str(top_k),
        "RETURN", "2", "title", "content",
    )
    return _parse_search_reply(reply)


def kb_search_vector(query: str, top_k: int = 12, category: str | None = None) -> list[dict]:
    """Semantic (vector) search over the Rho-Bank knowledge base.

    Better than kb_search_bm25 when the query is a natural-language question
    rather than exact keywords. Use kb_search_hybrid to get both at once.

    Args:
        query: A natural-language question or description.
        top_k: Number of documents to return (default 12).
        category: Optional product-line filter (same values as kb_search_bm25).

    Returns:
        Matching documents with doc_id, title, and full content; or an error
        entry telling you to fall back to kb_search_bm25.
    """
    try:
        vector = struct.pack(f"{EMBEDDING_DIM}f", *_embed([query])[0])
        filt = _category_filter(category)
        knn_expr = (
            f"({filt})=>[KNN {top_k} @embedding $vec AS score]"
            if filt
            else f"*=>[KNN {top_k} @embedding $vec AS score]"
        )
        reply = _client.execute_command(
            "FT.SEARCH", KB_INDEX, knn_expr,
            "PARAMS", "2", "vec", vector,
            "SORTBY", "score",
            "LIMIT", "0", str(top_k),
            "RETURN", "3", "title", "content", "score",
            "DIALECT", "2",
        )
        return _strip_score(_parse_search_reply(reply))
    except Exception as e:
        return [
            {
                "error": f"Vector search unavailable ({type(e).__name__}). "
                "Use kb_search_bm25 with keywords instead."
            }
        ]


def kb_search_hybrid(query: str, top_k: int = 12, category: str | None = None) -> list[dict]:
    """Hybrid search: Reciprocal Rank Fusion (RRF) of BM25 + vector results.

    USE THIS BY DEFAULT. It combines keyword matching with semantic
    understanding, giving higher recall than either search alone. Especially
    important when comparing multiple account/card options — run one hybrid
    search per product you're comparing so you don't miss fee details.

    Args:
        query: A natural-language question or keywords about the product.
        top_k: Number of fused results to return (default 12).
        category: Strongly recommended — filters to a product line first,
            then fuses within that category. Values: "checking_accounts",
            "savings_accounts", "credit_cards", "business_checking_accounts",
            "business_savings_accounts", "business_credit_cards",
            "buy_now_pay_later", "bank_accounts".

    Returns:
        Best-matching documents ranked by fused BM25 + vector relevance.

    Examples (comparison task workflow — search each candidate separately):
        kb_search_hybrid("ATM fee foreign withdrawal", category="checking_accounts")
        kb_search_hybrid("eligibility age restriction", category="checking_accounts")
        kb_search_hybrid("cash back rewards rate", category="credit_cards")
    """
    k_rrf = 60  # standard RRF constant
    pool = top_k * 3  # fetch more from each ranker for better fusion coverage

    bm25_docs = kb_search_bm25(query, top_k=pool, category=category)
    vector_docs = kb_search_vector(query, top_k=pool, category=category)

    rrf_scores: dict[str, float] = {}
    doc_store: dict[str, dict] = {}

    for rank, doc in enumerate(bm25_docs):
        did = doc.get("doc_id", "")
        if did:
            rrf_scores[did] = rrf_scores.get(did, 0.0) + 1.0 / (k_rrf + rank + 1)
            doc_store[did] = doc

    for rank, doc in enumerate(vector_docs):
        if "error" in doc:
            continue
        did = doc.get("doc_id", "")
        if did:
            rrf_scores[did] = rrf_scores.get(did, 0.0) + 1.0 / (k_rrf + rank + 1)
            doc_store.setdefault(did, doc)

    ranked = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
    return [doc_store[did] for did, _ in ranked if did in doc_store]
