"""Local Web UI and asynchronous single-writer jobs. Bind loopback only."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import urlsplit, parse_qs

from .analysis import ingest_file
from .embedding import LocalEncoder
from .hippo import HippoRetriever, build_index
from .learning import (create_session, get_session, records, submit_record, update_student,
                       plan_learning, complete_action, change_goal, close_session)
from .level3_store import initialize, index_status
from .model import ChatModel, load_env
from .qa import ask
from .store import Store

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).with_name('static')


class Application:
    def __init__(self, database, model=None, encoder=None):
        self.database = str(database)
        self.model = model
        self.encoder = encoder
        self.retriever = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='learning-job')
        self.jobs = {}
        self.lock = threading.Lock()
        with Store(self.database) as store:
            initialize(store)

    def chat(self):
        if self.model is None:
            self.model = ChatModel()
        return self.model

    def search(self, store, question, top_k=8, max_chars=12000, mode='hippo'):
        if self.retriever is None:
            self.retriever = HippoRetriever(self.encoder or LocalEncoder())
        return self.retriever.retrieve(store, self.chat(), question, top_k, max_chars, mode)

    def submit(self, command, payload):
        with self.lock:
            if sum(j['status'] in ('queued', 'running') for j in self.jobs.values()) >= 10:
                raise ValueError('任务过多，请等待当前任务完成')
            jid = 'job_' + uuid.uuid4().hex
            self.jobs[jid] = {'id': jid, 'command': command, 'status': 'queued', 'progress': '等待执行',
                              'started_at': time.time(), 'result': None, 'error': None}
            # Bound finished jobs retained in memory; durable results live in SQLite.
            finished = [key for key, j in self.jobs.items() if j['status'] in ('done', 'failed')]
            for key in finished[:-100]:
                self.jobs.pop(key, None)
        self.pool.submit(self.run_job, jid, command, payload)
        return {'job_id': jid}

    def run_job(self, jid, command, payload):
        def progress(message):
            with self.lock:
                self.jobs[jid]['progress'] = message
        with self.lock:
            self.jobs[jid]['status'] = 'running'
        before = len(getattr(self.model, 'call_stats', []))
        try:
            with Store(self.database) as store:
                result = self.dispatch(store, command, payload, progress)
            with self.lock:
                calls = getattr(self.model, 'call_stats', [])[before:]
                self.jobs[jid].update(status='done', result=result, progress='完成',
                    metrics={'model_calls': len(calls), 'prompt_tokens': sum(c.get('prompt_tokens') or 0 for c in calls),
                             'completion_tokens': sum(c.get('completion_tokens') or 0 for c in calls),
                             'elapsed_seconds': round(time.time() - self.jobs[jid]['started_at'], 2), 'calls': calls})
        except Exception as exc:
            # Model errors are sanitized by ChatModel; do not expose traceback/config/secrets.
            message = str(exc) if isinstance(exc, (ValueError, RuntimeError, OSError)) else type(exc).__name__
            with self.lock:
                self.jobs[jid].update(status='failed', error=message, progress='失败')

    def reconcile(self, store, sid, progress, force=False):
        session = get_session(store, sid)
        pending = records(store, sid, 20, True)
        important = any(r['payload']['kind'] != 'question' for r in pending)
        due = session['teaching_state']['turns_since_plan'] >= 5
        if important or force or (due and session['teaching_state'].get('needs_plan')):
            while records(store, sid, 1, True):
                progress('根据学习证据更新学生模型')
                update_student(store, self.chat(), sid)
        session = get_session(store, sid)
        if force or session['teaching_state'].get('needs_plan'):
            if records(store, sid, 1, True):
                # Initial plan after ordinary questions also uses current evidence.
                while records(store, sid, 1, True):
                    update_student(store, self.chat(), sid)
            progress('规划下一步学习动作')
            session = plan_learning(store, self.chat(), sid,
                                    lambda q, k, m: self.search(store, q, k, m),
                                    session['teaching_state'].get('plan_reason') or 'manual')
        elif due:
            # Five turns is a cheap checkpoint, not an unconditional model call.
            from .learning import save_session
            state = dict(session['teaching_state'])
            state['turns_since_plan'] = 0
            with store.transaction():
                save_session(store, session, state)
        return get_session(store, sid)

    def dispatch(self, store, command, p, progress):
        if command == 'ingest':
            kind = p.get('kind')
            if kind not in ('resource', 'problem'):
                raise ValueError('请选择资料或题目类型')
            files = []
            if p.get('dataset'):
                folder = ROOT / 'data' / 'linear_algebra' / ('resources' if kind == 'resource' else 'problems')
                files = sorted(folder.glob('*.md'))
            else:
                uploads = p.get('files', [])
                if not isinstance(uploads, list) or not 1 <= len(uploads) <= 30:
                    raise ValueError('每次上传1–30份Markdown')
                folder = ROOT / 'runtime' / 'uploads'
                folder.mkdir(parents=True, exist_ok=True)
                for upload in uploads:
                    if not isinstance(upload, dict) or not isinstance(upload.get('text'), str):
                        raise ValueError('上传格式错误')
                    name = Path(str(upload.get('name', ''))).name
                    if not name.lower().endswith('.md'):
                        raise ValueError('仅支持Markdown文件')
                    if len(upload['text'].encode('utf-8')) > 2_000_000:
                        raise ValueError('单文件不得超过2MB')
                    # Stable content-addressed paths make repeat uploads idempotent.
                    from .markdown import digest
                    path = folder / (digest(kind + name + upload['text'])[:16] + '_' + name)
                    path.write_text(upload['text'], encoding='utf-8')
                    files.append(path)
            result = []
            for i, path in enumerate(files):
                progress(f'导入 {i+1}/{len(files)} · {path.name}')
                result.append(ingest_file(store, self.chat(), path, kind))
            return result
        if command == 'index':
            return build_index(store, self.chat(), self.encoder or LocalEncoder(), progress)
        if command == 'create_session':
            session = create_session(store, p.get('goal'), 'local')
            # Creation persists before planning; failure can be retried in the UI.
            return self.reconcile(store, session['id'], progress, True)
        if command == 'search':
            progress('召回三元组与原文，运行检索')
            return self.search(store, p.get('question'), mode=p.get('mode', 'hippo'))
        if command == 'ask':
            sid = p.get('session_id')
            session = get_session(store, sid)
            if session['closed']:
                raise ValueError('session已结束')
            progress('检索课程资料并生成带引用的回答')
            result = ask(store, self.chat(), p.get('question'),
                         retriever=lambda q, k, m: self.search(store, q, k, m, p.get('mode', 'hippo')),
                         context={'student_model': session['student_model'], 'teaching_state': session['teaching_state']})
            retrieval = json.loads(store.rows('SELECT retrieval_json FROM qa_records WHERE id=?', (result['id'],))[0]['retrieval_json'])
            if retrieval['point_ids']:
                submit_record(store, sid, {'kind': 'question', 'point_ids': retrieval['point_ids'][:20],
                                           'content': p['question'], 'qa_id': result['id']})
            try:
                state = self.reconcile(store, sid, progress)
            except Exception as exc:
                # Do not discard a successful, already durable answer if subsequent planning fails.
                return {'answer': result, 'session': get_session(store, sid),
                        'warning': '回答已保存，后续状态/计划处理失败：' + str(exc)}
            return {'answer': result, 'session': state}
        sid = p.get('session_id')
        if command == 'record':
            result = submit_record(store, sid, p.get('record'))
            try:
                session = self.reconcile(store, sid, progress)
                return {'record': result, 'session': session}
            except Exception as exc:
                return {'record': result, 'session': get_session(store, sid),
                        'warning': '记录已保存，评估/规划失败，可重试：' + str(exc)}
        if command == 'plan':
            return self.reconcile(store, sid, progress, True)
        if command == 'complete':
            complete_action(store, sid, p.get('action_id'), p.get('version'))
            return self.reconcile(store, sid, progress)
        if command == 'goal':
            change_goal(store, sid, p.get('goal'), p.get('version'))
            return self.reconcile(store, sid, progress, True)
        if command == 'close':
            while records(store, sid, 1, True):
                progress('汇总待处理学习记录')
                update_student(store, self.chat(), sid)
            return close_session(store, sid)
        raise ValueError('未知操作')

    def snapshot(self):
        with Store(self.database) as store:
            sessions = store.rows('SELECT id,closed FROM sessions ORDER BY rowid DESC LIMIT 20')
            current = next((s for s in sessions if not s['closed']), sessions[0] if sessions else None)
            return {'index': index_status(store), 'graph': store.graph(), 'history': store.history(30),
                    'session': get_session(store, current['id']) if current else None,
                    'records': records(store, current['id']) if current else [],
                    'decisions': [{**d, 'output_json': json.loads(d['output_json'])} for d in store.rows(
                        'SELECT id,created_at,role,reason,output_json FROM learning_decisions ORDER BY id DESC LIMIT 20')],
                    'sessions': sessions}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_json(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self.valid_host():
                return self.send_json({'error': '非法Host'}, 403)
            parsed = urlsplit(self.path)
            try:
                if parsed.path == '/api/state':
                    return self.send_json(app.snapshot())
                if parsed.path == '/api/job':
                    jid = parse_qs(parsed.query).get('id', [''])[0]
                    with app.lock:
                        job = dict(app.jobs[jid]) if jid in app.jobs else None
                    return self.send_json(job or {'error': '任务不存在'}, 200 if job else 404)
                if parsed.path == '/api/point':
                    with Store(app.database) as store:
                        return self.send_json(store.query_point(parse_qs(parsed.query).get('id', [''])[0]))
                assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'application/javascript'),
                          '/style.css': ('style.css', 'text/css')}
                if parsed.path not in assets:
                    return self.send_json({'error': '不存在'}, 404)
                filename, mime = assets[parsed.path]
                body = (STATIC / filename).read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', mime + '; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'")
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, OSError) as exc:
                self.send_json({'error': str(exc)}, 400)

        def do_POST(self):
            origin = self.headers.get('Origin')
            if not self.valid_host() or (origin and origin not in
                (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')):
                return self.send_json({'error': '非法请求来源'}, 403)
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.send_json({'error': '仅接受JSON请求'}, 415)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4_000_000:
                    raise ValueError('请求大小必须小于4MB')
                p = json.loads(self.rfile.read(length))
                if not isinstance(p, dict):
                    raise ValueError('请求必须是JSON对象')
                command = urlsplit(self.path).path.removeprefix('/api/')
                if command not in {'ingest', 'index', 'create_session', 'search', 'ask', 'record', 'plan', 'complete', 'goal', 'close'}:
                    return self.send_json({'error': '未知操作'}, 404)
                return self.send_json(app.submit(command, p), 202)
            except (ValueError, KeyError) as exc:
                self.send_json({'error': str(exc)}, 400)
    return Handler


def main():
    parser = argparse.ArgumentParser(description='AI 学习系统 Level 3 Web')
    parser.add_argument('--db', default=str(ROOT / 'runtime' / 'learning.sqlite3'))
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    load_env(ROOT / '.env')
    app = Application(args.db)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
    print(f'学习系统已启动：http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
