"""BM25 over exact passages, alias anchors and one-hop graph expansion."""

from collections import Counter
import math
import re
import uuid

from .store import normalized


STOP = {"为什么", "是什么", "怎么", "如何", "是否", "可以", "一个", "这个", "说明", "证明", "求", "的", "是", "与", "和"}


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        # Single characters matter for mathematical terms such as 秩、核、像.
        tokens.extend(char for char in run if char in "秩核像基逆解")
    return [token for token in tokens if token not in STOP]


def retrieve(store, question: str, top_k=8, max_chars=12000):
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        raise ValueError("问题必须是 1–4000 字符的非空文本")
    if type(top_k) is not int or not 1 <= top_k <= 50 or max_chars < 100:
        raise ValueError("检索数量或上下文预算不合法")
    # Question statements/solutions are not authoritative explanations.
    chunks = store.rows("SELECT c.*,d.path,d.kind,d.content_hash FROM chunks c "
                        "JOIN documents d ON c.document_id=d.id WHERE d.kind='resource' ORDER BY c.id")
    frequencies = [Counter(tokenize(chunk["text"])) for chunk in chunks]
    lengths = [sum(tf.values()) for tf in frequencies]
    avg = sum(lengths) / max(len(lengths), 1) or 1
    df = Counter(term for tf in frequencies for term in tf)
    query = set(tokenize(question))
    aliases = store.rows("SELECT * FROM aliases ORDER BY normalized")
    query_name = normalized(question)
    seeds = {alias["point_id"] for alias in aliases
             if alias["normalized"] and alias["normalized"] in query_name}
    query_points = set(seeds)
    links = store.rows("SELECT * FROM point_chunks")
    chunk_points = {}
    for link in links:
        chunk_points.setdefault(link["chunk_id"], set()).add(link["point_id"])
    candidates = {}
    for chunk, tf, length in zip(chunks, frequencies, lengths):
        score = 0.0
        for term in query:
            count = tf[term]
            if count:
                idf = math.log(1 + (len(chunks) - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length / avg))
        overlap = len(query & set(tokenize(chunk["title"])))
        score += overlap * 0.5
        anchored = bool(chunk_points.get(chunk["id"], set()) & seeds)
        if anchored:
            score += 2.0
        if score > 0:
            reasons = (["alias_match"] if anchored else []) + (["lexical"] if tf.keys() & query else [])
            candidates[chunk["id"]] = {**chunk, "score": round(score, 6), "scores":{"bm25":round(score,6),"dense":None,"ppr":None,"rrf":None}, "retrieval_reason": reasons}
    ranked = sorted(candidates.values(), key=lambda c: (-c["score"], c["id"]))
    # Seed expansion using actual query matches, not every weak lexical candidate.
    for chunk in ranked[:3]:
        seeds.update(chunk_points.get(chunk["id"], set()))
    expanded = set(seeds)
    for edge in store.rows("SELECT DISTINCT before_id,after_id FROM prerequisites"):
        if edge["before_id"] in seeds:
            expanded.add(edge["after_id"])
        if edge["after_id"] in seeds:
            expanded.add(edge["before_id"])
    expansions = []
    for chunk in chunks:
        if chunk["id"] not in candidates and chunk_points.get(chunk["id"], set()) & expanded:
            expansions.append({**chunk, "score": 0.0, "scores":{"bm25":0.0,"dense":None,"ppr":None,"rrf":None}, "retrieval_reason": ["graph_expansion"]})
    selected, used = [], 0
    # Reserve up to two slots for graph evidence if direct matches fill the list.
    direct_limit = max(1, top_k - min(2, len(expansions)))
    ordered = ranked[:direct_limit] + expansions + ranked[direct_limit:]
    for chunk in ordered:
        if used + len(chunk["text"]) > max_chars:
            continue
        selected.append(chunk)
        used += len(chunk["text"])
        if len(selected) == top_k:
            break
    return {"schema_version":2,"query_point_ids":sorted(query_points),
            "evidence_point_ids":sorted({p for c in selected for p in chunk_points.get(c["id"],set())}),
            "id": "retrieval_" + uuid.uuid4().hex, "query": question,
            "point_ids": sorted(seeds), "expanded_point_ids": sorted(expanded - seeds),
            "chunks": selected, "context_chars": used, "max_chars": max_chars,
            "method": "lexical_bm25_alias_one_hop"}
