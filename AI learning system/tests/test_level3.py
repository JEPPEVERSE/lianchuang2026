"""Tests use explicit deterministic model/vector fixtures, not production AI."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from learning_system.analysis import ingest_file
from learning_system.store import Store
from learning_system.level3_store import initialize, active_index, index_status, resource_chunks
from learning_system.hippo import build_index, HippoRetriever, ppr
from learning_system.learning import (create_session, get_session, submit_record, update_student,
                                    plan_learning, complete_action, close_session, change_goal)
from learning_system.web import Application, make_handler
from support import FixtureModel, QueueModel, RESOURCE_ONE, RESOURCE_TWO, PROBLEM

class TestEncoder:
    identity = 'TEST_ONLY-v1'
    def cached(self, store, text, query=False):
        return [[1.,0.,0.]] if '秩' in text or '唯一' in text else [[0.,1.,0.]]

class HippoModel:
    model = 'TEST_ONLY'
    def complete(self, system, payload, schema, correction=None):
        if payload['task'] == 'openie':
            quote = payload['chunk']['text'].splitlines()[-1]
            return {'triples':[{'subject':'矩阵的秩','relation':'解释','object':quote,'quote':quote}]}
        return {'triple_ids':[x['id'] for x in payload['triples'][:2]]}

class Level3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.db=self.root/'test.sqlite3'; self.store=Store(self.db);initialize(self.store)
        for filename,text,kind in [('a.md',RESOURCE_ONE,'resource'),('b.md',RESOURCE_TWO,'resource'),('q.md',PROBLEM,'problem')]:
            path=self.root/filename;path.write_text(text);ingest_file(self.store,FixtureModel(),path,kind)
        self.pid=self.store.query_point('矩阵秩')['id'];self.problem=self.store.rows('SELECT * FROM problems')[0]
        self.sid=create_session(self.store,'掌握矩阵的秩')['id']
    def tearDown(self):
        self.store.close();self.tmp.cleanup()
    def action(self):
        c=next(c for c in self.store.query_point(self.pid)['chunks'] if c['kind']=='resource')
        return {'type':'review','point_id':self.pid,'chunk_ids':[c['id']],'problem_ids':[],
                'instruction':'复习秩','reason':'掌握未知'}
    def plan(self, action=None):
        return plan_learning(self.store,QueueModel([{'summary':'先复习','actions':[action or self.action()],'tool_requests':[]}]),self.sid,lambda *_:{})
    def record(self,kind='confused',**kwargs):
        return submit_record(self.store,self.sid,{'kind':kind,'point_ids':[self.pid],'content':'分不清秩与行数',**kwargs})
    def update(self,rid,basis='self_report',confidence=.5):
        out={'updates':[{'point_id':self.pid,'mastery':.25,'confidence':confidence,'basis':basis,'weakness':'概念不清','evidence_ids':[rid],'reason':'用户报告困惑'}]}
        return QueueModel([out,out])
    def test_ppr_dangling_probability(self):
        scores,_=ppr(3,[[0,1,1.,'relation']],{0:1,2:.2})
        self.assertAlmostEqual(sum(scores),1);self.assertGreater(scores[0],scores[1]);self.assertGreater(scores[2],0)
    def test_atomic_index(self):
        build_index(self.store,HippoModel(),TestEncoder());old=active_index(self.store)['id']
        class Broken(TestEncoder):
            def cached(self,*a,**k):raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):build_index(self.store,HippoModel(),Broken())
        self.assertEqual(active_index(self.store)['id'],old)
    def test_retrieval_sources_and_filter(self):
        build_index(self.store,HippoModel(),TestEncoder())
        r=HippoRetriever(TestEncoder()).retrieve(self.store,HippoModel(),'矩阵的秩')
        self.assertTrue(r['selected_triple_ids']);self.assertGreater(r['ppr_iterations'],0)
        source={c['id']:c['text'] for c in resource_chunks(self.store)}
        self.assertTrue(all(c['kind']=='resource' and c['text']==source[c['id']] for c in r['chunks']))
        with self.assertRaises(RuntimeError):HippoRetriever(TestEncoder()).retrieve(self.store,QueueModel([{'triple_ids':['bad']},{'triple_ids':['bad']}]),'new query')
    def test_stale_index(self):
        build_index(self.store,HippoModel(),TestEncoder())
        path=self.root/'new.md';path.write_text(RESOURCE_TWO+'\n更多说明。');ingest_file(self.store,FixtureModel(),path,'resource')
        self.assertEqual(index_status(self.store)['status'],'stale')
        with self.assertRaises(ValueError):active_index(self.store)
    def test_record_duplicate_and_durable_failure(self):
        self.record(id='record_fixed');self.assertEqual(self.record(id='record_fixed')['status'],'duplicate')
        with self.assertRaises(RuntimeError):update_student(self.store,self.update('record_fixed','answer_evidence',1),self.sid)
        self.assertEqual(self.store.rows('SELECT processed FROM learning_records')[0]['processed'],0)
        with self.assertRaises(ValueError):self.plan()
    def test_updater_once_restart_and_plan(self):
        rid=self.record()['id'];model=self.update(rid);update_student(self.store,model,self.sid)
        self.assertEqual(update_student(self.store,model,self.sid)['status'],'unchanged');self.assertEqual(model.calls,1)
        with Store(self.db) as reopened:self.assertEqual(get_session(reopened,self.sid)['student_model']['points'][self.pid]['mastery'],.25)
        self.assertFalse(self.plan()['teaching_state']['needs_plan'])
    def test_planner_tools_and_illegal_actions(self):
        tool={'summary':'查询知识','actions':[],'tool_requests':[{'name':'point','query':self.pid}]}
        final={'summary':'复习','actions':[self.action()],'tool_requests':[]}
        self.assertEqual(plan_learning(self.store,QueueModel([tool,final]),self.sid,lambda *_:{})['learning_plan']['version'],1)
        bad=self.action();bad['chunk_ids']=['invented']
        with self.assertRaises(RuntimeError):plan_learning(self.store,QueueModel([{'summary':'bad','actions':[bad],'tool_requests':[]}]*2),self.sid,lambda *_:{})
        self.assertEqual(get_session(self.store,self.sid)['learning_plan']['version'],1)
        with self.assertRaises(ValueError):plan_learning(self.store,QueueModel([tool]*3),self.sid,lambda *_:{})
    def test_action_and_goal_versions(self):
        s=self.plan();aid=s['teaching_state']['current_action_id']
        with self.assertRaises(ValueError):complete_action(self.store,self.sid,aid,-1)
        s=complete_action(self.store,self.sid,aid,s['version']);self.assertEqual(s['learning_plan']['actions'][0]['status'],'completed')
        s=change_goal(self.store,self.sid,'学习方程组',s['version']);self.assertTrue(s['teaching_state']['needs_plan'])
        self.assertTrue(close_session(self.store,self.sid)['closed'])
        with self.assertRaises(ValueError):self.plan()
    def test_practice_requires_attempt(self):
        a=self.action();a.update(type='practice',chunk_ids=[],problem_ids=[self.problem['id']])
        s=self.plan(a);aid=s['teaching_state']['current_action_id']
        with self.assertRaises(ValueError):complete_action(self.store,self.sid,aid,s['version'])
        self.record('attempt',problem_id=self.problem['id'],action_id=aid)
        s=get_session(self.store,self.sid);complete_action(self.store,self.sid,aid,s['version'])
    def test_attempt_cannot_complete_unrelated_action(self):
        s=self.plan()
        with self.assertRaises(ValueError):
            self.record('attempt',problem_id=self.problem['id'],action_id=s['teaching_state']['current_action_id'])
    def test_self_report_confidence_is_bounded(self):
        rid=self.record('understood')['id']
        with self.assertRaises(RuntimeError):
            update_student(self.store,self.update(rid,'self_report',1),self.sid)
    def test_http_jobs_and_origin(self):
        app=Application(self.db,HippoModel(),TestEncoder());server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base+'/api/state') as r:self.assertTrue(json.load(r)['graph']['points'])
            req=Request(base+'/api/index',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://evil.example'})
            with self.assertRaises(HTTPError) as exc:urlopen(req)
            self.assertEqual(exc.exception.code,403)
            req=Request(base+'/api/index',data=b'{}',headers={'Content-Type':'application/json','Origin':base})
            with urlopen(req) as r:jid=json.load(r)['job_id']
            for _ in range(100):
                with urlopen(base+'/api/job?id='+jid) as r:job=json.load(r)
                if job['status'] in ('done','failed'):break
                time.sleep(.02)
            self.assertEqual(job['status'],'done',job)
            with urlopen(base+'/app.js') as r:self.assertIn(b'textContent',r.read())
        finally:server.shutdown();server.server_close();app.pool.shutdown();thread.join()
