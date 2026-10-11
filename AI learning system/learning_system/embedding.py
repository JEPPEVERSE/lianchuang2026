"""Real local BGE embeddings, token windows and persistent cache (no hash vectors)."""
from __future__ import annotations
import os
import threading
from .markdown import digest
from .level3_store import cache_get, cache_put, dumps

_MODEL_LOCK = threading.Lock()
_MODELS = {}


class LocalEncoder:
    def __init__(self):
        self.name = os.getenv('EMBED_MODEL', 'BAAI/bge-small-zh-v1.5')
        self.revision = os.getenv('EMBED_REVISION', '7999e1d3359715c523056ef9478215996d62a620')
        self.instruction = '为这个句子生成表示以用于检索相关文章：'
        self.identity = dumps({'model': self.name, 'revision': self.revision,
                               'windows': 'tokens-overlap32-v1', 'instruction': self.instruction})
        self._model = None

    def model(self):
        with _MODEL_LOCK:
            if self._model is None:
                from sentence_transformers import SentenceTransformer
                key = (self.name, self.revision)
                if key not in _MODELS:
                    kwargs = {'revision': self.revision, 'device': os.getenv('EMBED_DEVICE', 'cpu')}
                    try:
                        _MODELS[key] = SentenceTransformer(self.name, local_files_only=True, **kwargs)
                    except OSError:
                        _MODELS[key] = SentenceTransformer(self.name, **kwargs)
                self._model = _MODELS[key]
        return self._model

    def encode_windows(self, text, query=False):
        """Cover every source token; citations always use the untouched SQLite text."""
        import numpy as np
        model = self.model()
        tokenizer = model.tokenizer
        prefix = self.instruction if query else ''
        # Subtract special tokens and query prefix, leave room for decode/re-encode drift.
        budget = model.max_seq_length - tokenizer.num_special_tokens_to_add(False)
        budget -= len(tokenizer.encode(prefix, add_special_tokens=False)) + 16
        if budget <= 32:
            raise ValueError('embedding 模型上下文窗口过小')
        ids = tokenizer.encode(text, add_special_tokens=False)
        if not ids:
            raise ValueError('无法编码空文本')
        windows = []
        step = budget - 32
        for start in range(0, len(ids), step):
            candidate = prefix + tokenizer.decode(ids[start:start + budget], skip_special_tokens=True)
            # Do not silently truncate even when tokenizer decoding changes token boundaries.
            if len(tokenizer.encode(candidate)) > model.max_seq_length:
                raise ValueError('embedding 窗口超过模型限制，请调整 token 预算')
            windows.append(candidate)
            if start + budget >= len(ids):
                break
        with _MODEL_LOCK:
            vectors = model.encode(windows, normalize_embeddings=True, batch_size=32,
                                   show_progress_bar=False, convert_to_numpy=True)
        vectors = np.asarray(vectors, dtype=np.float32)
        if not np.isfinite(vectors).all():
            raise ValueError('embedding 出现非法数值')
        return vectors.tolist()

    def cached(self, store, text, query=False):
        key = digest(self.identity + str(query) + text)
        value = cache_get(store, 'vector_cache', key)
        if value is None:
            value = self.encode_windows(text, query)
            cache_put(store, 'vector_cache', key, value)
        return value
