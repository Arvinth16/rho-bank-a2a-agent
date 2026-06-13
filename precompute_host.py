"""Standalone host-side embedding precomputation (works on Python 3.9+).

Run from the build root:
    GOOGLE_GENAI_USE_VERTEXAI=true \
    GOOGLE_CLOUD_PROJECT=a2a-hackathon-499312 \
    GOOGLE_CLOUD_LOCATION=global \
    python3 precompute_host.py
"""

import base64
import json
import os
import struct
import sys
from pathlib import Path

KB_DIR = Path(__file__).parent / "kb" / "documents"
OUT_PATH = Path(__file__).parent / "kb" / "embeddings.json"
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 768
BATCH = 25


def load_docs():
    docs = []
    for p in sorted(KB_DIR.glob("*.json")):
        with open(p) as f:
            docs.append(json.load(f))
    return docs


def embed_batch(client, texts):
    from google.genai import types
    result = client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
    )
    return [e.values for e in result.embeddings]


def main():
    from google import genai
    client = genai.Client()

    docs = load_docs()
    print(f"[precompute] {len(docs)} documents to embed", file=sys.stderr)

    cache = {}
    if OUT_PATH.exists():
        with open(OUT_PATH) as f:
            cache = json.load(f)
        print(f"[precompute] {len(cache)} already cached, skipping those", file=sys.stderr)

    todo = [d for d in docs if d["id"] not in cache]
    print(f"[precompute] {len(todo)} to embed fresh", file=sys.stderr)

    for start in range(0, len(todo), BATCH):
        batch = todo[start: start + BATCH]
        texts = [f"{d['title']}\n{d['content']}" for d in batch]
        vectors = embed_batch(client, texts)
        for doc, vec in zip(batch, vectors):
            cache[doc["id"]] = base64.b64encode(
                struct.pack(f"{EMBEDDING_DIM}f", *vec)
            ).decode()
        done = min(start + BATCH, len(todo))
        print(f"[precompute] {done}/{len(todo)} embedded", file=sys.stderr)

    OUT_PATH.write_text(json.dumps(cache))
    print(f"[precompute] wrote {len(cache)} embeddings to {OUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
