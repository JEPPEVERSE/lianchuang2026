"""Additive Level 3 migrations; SQLite remains the authority."""
import json
from datetime import datetime, timezone
from .markdown import digest

SCHEMA = '''
CREATE TABLE IF NOT EXISTS openie_cache (cache_key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS vector_cache (cache_key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hippo_indexes (id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
 fingerprint TEXT NOT NULL, encoder TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hippo_active (singleton INTEGER PRIMARY KEY CHECK(singleton=1), index_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS retrieval_cache (cache_key TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learners (id TEXT PRIMARY KEY, version INTEGER NOT NULL, model TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, learner_id TEXT NOT NULL REFERENCES learners(id),
 version INTEGER NOT NULL, closed INTEGER NOT NULL, state TEXT NOT NULL, plan TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS learning_records (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 created_at TEXT NOT NULL, payload TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS learning_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT,
 session_id TEXT NOT NULL, created_at TEXT NOT NULL, role TEXT NOT NULL, reason TEXT NOT NULL,
 input_json TEXT NOT NULL, output_json TEXT NOT NULL);
'''


def initialize(store):
    store.connection.executescript(SCHEMA)
    store.connection.commit()


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def resource_chunks(store):
    return store.rows("SELECT c.*,d.path,d.content_hash,d.kind FROM chunks c JOIN documents d "
                      "ON c.document_id=d.id WHERE d.kind='resource' ORDER BY c.id")


def fingerprint(store):
    return digest(dumps(store.rows("SELECT id,content_hash FROM documents WHERE kind='resource' ORDER BY id")))


def cache_get(store, table, key):
    if table not in ('openie_cache', 'vector_cache', 'retrieval_cache'):
        raise ValueError('invalid cache')
    rows = store.rows(f'SELECT data FROM {table} WHERE cache_key=?', (key,))
    return json.loads(rows[0]['data']) if rows else None


def cache_put(store, table, key, value):
    if table not in ('openie_cache', 'vector_cache', 'retrieval_cache'):
        raise ValueError('invalid cache')
    with store.transaction():
        store.connection.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?)', (key, dumps(value)))


def active_index(store, require_current=True):
    rows = store.rows('SELECT i.* FROM hippo_indexes i JOIN hippo_active a ON i.id=a.index_id')
    if not rows:
        raise ValueError('尚未构建 HippoRAG 索引，请在资料页点击构建索引')
    row = rows[0]
    if require_current and row['fingerprint'] != fingerprint(store):
        raise ValueError('资料已变化，HippoRAG 索引过期，请重新构建索引')
    return {**row, 'payload': json.loads(row['payload'])}


def index_status(store):
    initialize(store)
    chunks = resource_chunks(store)
    try:
        row = active_index(store, False)
        payload = row['payload']
        return {'status': 'ready' if row['fingerprint'] == fingerprint(store) else 'stale',
                'id': row['id'], 'created_at': row['created_at'], 'encoder': row['encoder'],
                'chunks': len(chunks), 'indexed_chunks': len(payload['chunks']),
                'triples': len(payload['triples']), 'entities': len(payload['entities']),
                'edges': len(payload['edges'])}
    except ValueError:
        return {'status': 'missing', 'chunks': len(chunks)}
