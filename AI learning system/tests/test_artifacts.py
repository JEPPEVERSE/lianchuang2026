"""Version migration and business evidence/lifecycle regressions."""
import json
import unittest
from test_level3 import Level3Tests as _Fixture
from support import QueueModel
from learning_system.learning import get_session,submit_record,update_student,plan_learning,complete_action,change_goal
from learning_system.level3_store import initialize,dumps
from learning_system.artifacts import student_model

class ArtifactTests(unittest.TestCase):
    setUp=_Fixture.setUp
    tearDown=_Fixture.tearDown
    action=_Fixture.action
    plan=_Fixture.plan
    record=_Fixture.record
    def assessment(self, rid, basis='answer_evidence', mastery=.7, status='correct', misconceptions=None):
        return {'updates':[{'point_id':self.pid,'mastery':mastery,'confidence':.5,'basis':basis,
            'weakness':'','evidence_ids':[rid],'reason':'依据本轮记录',
            'performance_status':status,'misconceptions':misconceptions or []}], 'deferred_points':[]}
    def test_origin_and_repeat_count_are_explicit(self):
        p={'id':'record_v2','kind':'attempt','point_ids':[self.pid],'problem_id':self.problem['id'],'content':'模拟作答'}
        submit_record(self.store,self.sid,p,origin='simulated')
        self.assertEqual(submit_record(self.store,self.sid,p,origin='simulated')['status'],'duplicate')
        p.update(id='record_v2_next',assistance='independent');submit_record(self.store,self.sid,p)
        rows=self.store.rows('SELECT payload FROM learning_records ORDER BY rowid')
        first,second=[json.loads(r['payload']) for r in rows]
        self.assertEqual((first['origin'],first['attempt_no']),('simulated',1))
        self.assertEqual((second['origin'],second['attempt_no']),( 'real_user',2))
    def test_self_report_preserves_previous_performance(self):
        rid=self.record('attempt',problem_id=self.problem['id'])['id']
        update_student(self.store,QueueModel([self.assessment(rid)]),self.sid)
        rid=self.record('confused')['id']
        update_student(self.store,QueueModel([self.assessment(rid,'self_report',.1,'unknown')]),self.sid)
        point=get_session(self.store,self.sid)['student_model']['points'][self.pid]
        self.assertEqual(point['mastery'],.7);self.assertEqual(point['performance']['status'],'correct')
        self.assertEqual(point['self_report']['status'],'confused')
    def test_no_silent_omission(self):
        self.record()
        with self.assertRaises(RuntimeError):
            update_student(self.store,QueueModel([{'updates':[],'deferred_points':[]}]*2),self.sid)
        self.assertEqual(self.store.rows('SELECT processed FROM learning_records')[0]['processed'],0)
        out={'updates':[],'deferred_points':[{'point_id':self.pid,'reason':'证据不足'}]}
        update_student(self.store,QueueModel([out]),self.sid)
        self.assertIn(self.pid,get_session(self.store,self.sid)['student_model']['deferred_points'])
    def test_practice_id_survives_replanning(self):
        a=self.action();a.update(type='practice',chunk_ids=[],problem_ids=[self.problem['id']])
        s=self.plan(a);aid=s['teaching_state']['current_action_id']
        rid=self.record('attempt',problem_id=self.problem['id'],action_id=aid)['id']
        update_student(self.store,QueueModel([self.assessment(rid,status='incorrect',mastery=.2)]),self.sid)
        a['instruction']='改写教学文字';s=self.plan(a)
        self.assertEqual(s['teaching_state']['current_action_id'],aid)
        self.assertEqual(s['learning_plan']['actions'][0]['evaluation_status'],'incorrect')
        s=complete_action(self.store,self.sid,aid,s['version'])
        self.assertEqual(s['learning_plan']['actions'][0]['completion_record_ids'],[rid])
        self.assertEqual(s['learning_plan']['actions'][0]['status'],'completed')
    def test_goal_change_archives_old_actions(self):
        s=self.plan();old=s['learning_plan']['actions'][0]['id'];gid=s['teaching_state']['goal_id']
        s=change_goal(self.store,self.sid,'新目标',s['version']);self.assertNotEqual(s['teaching_state']['goal_id'],gid)
        s=self.plan();self.assertNotEqual(s['learning_plan']['actions'][0]['id'],old)
        self.assertIn(old,[a['id'] for a in s['learning_plan']['archived_actions']])
    def test_misconception_needs_answer_to_resolve(self):
        rid=self.record('understood')['id'];m=[{'description':'误解','status':'resolved','evidence_ids':[rid]}]
        out=self.assessment(rid,'self_report',.4,'unknown',m)
        with self.assertRaises(RuntimeError):update_student(self.store,QueueModel([out,out]),self.sid)
    def test_repeated_question_does_not_become_independent_evidence(self):
        ids=[self.record('attempt',problem_id=self.problem['id'])['id'] for _ in range(2)]
        out=self.assessment(ids[-1]);update_student(self.store,QueueModel([out]),self.sid)
        summary=get_session(self.store,self.sid)['student_model']['points'][self.pid]['evidence_summary']
        self.assertEqual(summary['attempt_count'],2);self.assertEqual(summary['distinct_problem_count'],1)
        self.assertEqual(summary['independent_attempt_count'],0)
    def test_migration_is_idempotent_and_does_not_invent_evidence(self):
        with self.store.transaction():
            self.store.connection.execute("DELETE FROM app_settings WHERE key='artifacts_version'")
            self.store.connection.execute('UPDATE learners SET model=?',(dumps({'points':{self.pid:{'mastery':.6,'basis':'answer_evidence'}}}),))
        initialize(self.store);version=self.store.rows('SELECT version FROM learners')[0]['version']
        initialize(self.store)
        self.assertEqual(self.store.rows('SELECT version FROM learners')[0]['version'],version)
        p=get_session(self.store,self.sid)['student_model']['points'][self.pid]
        self.assertIsNone(p['performance']);self.assertEqual(p['mastery'],.6)

    def test_retrieval_distinguishes_query_and_evidence_points(self):
        from learning_system.retrieval import retrieve
        result=retrieve(self.store,'矩阵的秩')
        self.assertEqual(result['schema_version'],2)
        self.assertIn(self.pid,result['query_point_ids'])
        self.assertIn(self.pid,result['evidence_point_ids'])
        self.assertTrue(all('scores' in c for c in result['chunks']))
    def test_blank_active_profile_hides_other_student_history(self):
        from learning_system.web import Application
        with self.store.transaction():
            self.store.connection.execute('INSERT INTO learners VALUES (?,?,?)',('fresh',0,dumps(student_model({'points':{}}))))
            self.store.connection.execute("INSERT OR REPLACE INTO app_settings VALUES ('active_learner','fresh')")
        app=Application(self.db)
        try:
            snapshot=app.snapshot()
            self.assertIsNone(snapshot['session']);self.assertEqual(snapshot['student_model']['points'],{})
            self.assertEqual(snapshot['records'],[]);self.assertEqual(snapshot['decisions'],[])
        finally:app.pool.shutdown()

del _Fixture
