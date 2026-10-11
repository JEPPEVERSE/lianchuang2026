"""Back up, remove the explicit demo learner, select a fresh empty profile.
Teaching documents, problems, indexes and real learning history are preserved.
"""
import json
import sqlite3
import uuid
from pathlib import Path
from learning_system.store import Store
from learning_system.level3_store import initialize,dumps,now
from learning_system.artifacts import student_model
ROOT=Path(__file__).resolve().parents[1]

def main():
    stamp=now().replace(':','').replace('+','_')
    archive=ROOT/'runtime/archive'/('artifacts_v2_'+stamp);archive.mkdir(parents=True)
    with Store(ROOT/'runtime/learning.sqlite3') as store:
        backup=sqlite3.connect(archive/'learning_before_reset.sqlite3')
        store.connection.backup(backup);backup.close()
        before={table:store.rows(f'SELECT count(*) n FROM {table}')[0]['n'] for table in ('documents','chunks','points','problems')}
        initialize(store)
        sessions=store.rows("SELECT id FROM sessions WHERE learner_id='demo-simulated'")
        qa_ids=set()
        removed={'sessions':len(sessions),'records':0,'decisions':0,'qa_records':0}
        fresh='local-'+uuid.uuid4().hex[:12]
        with store.transaction():
            for session in sessions:
                sid=session['id']
                rows=store.rows('SELECT payload FROM learning_records WHERE session_id=?',(sid,))
                qa_ids.update(json.loads(r['payload']).get('qa_id') for r in rows)
                removed['records']+=len(rows)
                removed['decisions']+=store.connection.execute('DELETE FROM learning_decisions WHERE session_id=?',(sid,)).rowcount
                store.connection.execute('DELETE FROM learning_records WHERE session_id=?',(sid,))
                store.connection.execute('DELETE FROM qa_sessions WHERE session_id=?',(sid,))
                store.connection.execute('DELETE FROM sessions WHERE id=?',(sid,))
            store.connection.execute("DELETE FROM learners WHERE id='demo-simulated'")
            for qid in qa_ids-{None}:
                if not store.rows("SELECT id FROM learning_records WHERE json_extract(payload,'$.qa_id')=?",(qid,)):
                    removed['qa_records']+=store.connection.execute('DELETE FROM qa_records WHERE id=?',(qid,)).rowcount
            store.connection.execute('INSERT INTO learners VALUES (?,?,?)',(fresh,0,dumps(student_model({'points':{}}))))
            store.connection.execute("INSERT OR REPLACE INTO app_settings VALUES ('active_learner',?)",(fresh,))
        after={table:store.rows(f'SELECT count(*) n FROM {table}')[0]['n'] for table in before}
        assert before==after
        assert not store.rows('PRAGMA foreign_key_check')
        assert store.rows('PRAGMA integrity_check')[0]['integrity_check']=='ok'
    for name in ('demo_simulation.json','demo_ready.sqlite3','demo-student-preview.jpg','before_demo_simulation.sqlite3'):
        path=ROOT/'runtime'/name
        if path.exists():path.rename(archive/name)
    report={'schema_version':2,'created_at':now(),'active_learner':fresh,'removed_demo':removed,
            'curriculum_counts':after,'backup':str(archive/'learning_before_reset.sqlite3')}
    (ROOT/'runtime/profile_reset.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
