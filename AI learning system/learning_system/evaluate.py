"""Compare retrieval methods. Expected-file coverage is a proxy, not answer accuracy."""
import argparse
import json
from pathlib import Path
import time
from .model import ChatModel, load_env
from .store import Store
from .level3_store import initialize
from .hippo import HippoRetriever


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description='BM25/dense/HippoRAG 检索对比（会调用三元组筛选模型）')
    parser.add_argument('--db', default=str(root / 'runtime/learning.sqlite3'))
    parser.add_argument('--queries', type=Path, default=root/'data/linear_algebra/evaluation_queries.json')
    parser.add_argument('--output', type=Path, default=root/'runtime/retrieval_evaluation.json')
    parser.add_argument('--limit', type=int, default=12)
    parser.add_argument('--offline', action='store_true', help='仅本地BM25与dense，不调用DeepSeek')
    args = parser.parse_args()
    load_env(root / '.env')
    model, retriever = ChatModel(), HippoRetriever()
    results = []
    with Store(args.db) as store:
        initialize(store)
        imported = {Path(d['path']).name for d in store.rows("SELECT path FROM documents WHERE kind='resource'")}
        for query in json.loads(args.queries.read_text())[:args.limit]:
            expected = set(query.get('expected_resource_files', []))
            for mode in (('bm25', 'dense') if args.offline else ('bm25', 'dense', 'hippo')):
                start = time.monotonic(); before = len(model.call_stats)
                output = retriever.retrieve(store, model, query['question'], mode=mode)
                found = {Path(c['path']).name for c in output['chunks']}
                results.append({'id': query['id'], 'question': query['question'], 'mode': mode,
                                'seconds': round(time.monotonic()-start, 3),
                                'model_calls': len(model.call_stats)-before,
                                'expected_file_coverage': len(expected & found)/len(expected) if expected else None,
                                'missing_expected_files_in_database': sorted(expected-imported),
                                'retrieved_files': sorted(found), 'chunk_ids': [c['id'] for c in output['chunks']]})
                print(query['id'], mode, results[-1]['seconds'], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'metric_note':'expected_file_coverage 是章节覆盖代理指标；非人工标注段落Recall，不衡量回答正确性。缺资料时不可比较质量。冷启动与缓存耗时需区分。',
                                     'results':results}, ensure_ascii=False, indent=2))
    print(args.output)

if __name__ == '__main__':
    main()
