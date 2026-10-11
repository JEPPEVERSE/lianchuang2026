"""Replay explicitly simulated evidence through the real updater/planner.
Run from project root: PYTHONPATH=. .venv/bin/python scripts/seed_demo.py
Never writes mastery values by hand. Re-running resumes completed stages.
"""
import json
import sqlite3
from pathlib import Path
from learning_system.model import ChatModel, load_env
from learning_system.store import Store
from learning_system.learning import create_session, get_session, submit_record, records, update_student, plan_learning
from learning_system.level3_store import dumps, now
from learning_system.hippo import HippoRetriever
from learning_system.qa import ask

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'runtime/demo_simulation.json'
LEARNER = 'demo-simulated'
PREFIX = '【模拟学习记录，非真实用户表现】'
# Deliberately include persistent weaknesses; no predetermined mastery scores.
STAGES = [
 ('初始诊断', [
  ('confused','矩阵乘法','我把矩阵乘法当成逐元素相乘，也以为AB总等于BA。'),
  ('attempt','LA-Q001','AB=BA=[[1,0],[0,1]]，因为乘法满足交换律。'),
  ('confused','矩阵的秩','我以为秩总是矩阵的行数，不理解消元后零行代表什么。'),
  ('question','矩阵的秩','矩阵的秩与行数有什么区别？'),
  ('attempt','LA-Q003','矩阵有三行，所以秩为3，三列都是列空间的基。'),
  ('confused','线性方程组有解条件','我以为只要方程数等于未知数个数就有唯一解。'),
  ('attempt','LA-Q006','所有t和s下都是唯一解，两个方程对应两个未知量。'),
 ]),
 ('矩阵运算回补', [
  ('understood','矩阵乘法','现在知道AB第i,j项是A第i行与B第j列的内积，需要检查维数。'),
  ('attempt','LA-Q001','AB=[[7,2],[3,1]]，BA=[[1,2],[3,7]]；两者不同，矩阵乘法一般不交换。'),
  ('attempt','LA-Q007','det(A)=5-6=-1，A的逆=[[-5,2],[3,-1]]。A乘逆矩阵得到[[1,0],[0,1]]，逆矩阵乘A也得到单位矩阵。'),
  ('confused','上三角矩阵','不清楚把整个矩阵乘2时行列式为什么不是只乘2。'),
  ('attempt','LA-Q002','上三角行列式为1*3*4=12。我暂时认为det(2A)=24。'),
  ('attempt','LA-Q002','修正：三行各乘2，det(2A)=2^3 det(A)=96，det(A)=12。'),
  ('understood','矩阵乘法','能用AB和BA的数值反例解释交换律不成立，但不会推广成所有矩阵都不交换。'),
 ]),
 ('秩与列空间纠错', [
  ('question','矩阵的秩','消元后的列可以直接作为原矩阵列空间的基吗？'),
  ('attempt','LA-Q003','第二行减第一行2倍变为零行，第三行减第一行得到[0,-1,-2]。两个非零行，所以秩为2。我误把变换后的前两列作为原列空间基。'),
  ('confused','列空间维数','知道秩为2，但选列空间基时会混淆原矩阵列与消元后的列。'),
  ('attempt','LA-Q003','更正：消元后有两个主元，秩为2；取主元位置对应的原矩阵列(1,2,1)^T与(2,4,1)^T。两列线性无关，第三列=-第一列+2第二列，因此它们是原列空间的基。'),
  ('attempt','LA-Q008','三行两列，所以我认为单射满射都成立。'),
  ('attempt','LA-Q008','修正：两列线性无关，秩2等于定义域维数，核为零，故单射。像为R^3中第三坐标为0的平面，维数2小于3，不满射。'),
  ('understood','矩阵的秩','秩衡量独立方向，不是行数；可以通过消元数主元。'),
  ('understood','列空间维数','选原列的基时用主元位置回到原矩阵取列，行变换可能改变列空间。'),
 ]),
 ('方程组练习', [
  ('confused','自由变量','我知道有自由变量，但把一个自由变量当成了唯一答案，漏了另一个。'),
  ('attempt','LA-Q004','基础解系只有(-1,1,0)，所以通解是k(-1,1,0)。'),
  ('attempt','LA-Q004','令y=s,z=t，则x=-s-t。基础解系为(-1,1,0)与(-1,0,1)，通解=s(-1,1,0)+t(-1,0,1)。'),
  ('question','线性方程组有解条件','为什么判断有解需要同时比较系数矩阵与增广矩阵的秩？'),
  ('attempt','LA-Q005','第二式是第一式2倍。第一式减第三式得2y=2，故y=1，x+z=1。通解=(1,1,0)+t(-1,0,1)。两矩阵秩均为2，三个未知量所以一个自由参数。'),
  ('attempt','LA-Q006','t不等于1时，y=(s-2)/(t-1)，x=2-y；t=1时我认为总有无穷多解，没有检查s。'),
  ('confused','线性方程组有解条件','t=1时两式左边相同，但我经常忘记区分右边相等和不等的情况。'),
  ('understood','自由变量','齐次方程组有n-r个自由变量，基础解系需要独立覆盖这些方向。'),
 ]),
 ('复习检查与保留薄弱项', [
  ('attempt','LA-Q001','再次独立计算：AB=[[7,2],[3,1]]，BA=[[1,2],[3,7]]。第一个矩阵的左上元素1*1+2*3=7；两者不相等。'),
  ('attempt','LA-Q002','三角矩阵行列式为对角元素之积12，数乘公式det(cA)=c^n det(A)，此题n=3，因此det(2A)=96。'),
  ('attempt','LA-Q003','复习同一道题：消元得到两主元，rank=2，原矩阵前两列为基，第三列=-c1+2c2。重复练习同一题不代表已掌握所有新题。'),
  ('attempt','LA-Q004','重新参数化：x=-y-z，通解=(-s-t,s,t)，两基向量(-1,1,0)、(-1,0,1)线性无关。'),
  ('attempt','LA-Q006','t不等于1时唯一解。t=1时我仍会把s不等于2判为无穷多解，没有正确处理矛盾。'),
  ('confused','线性方程组有解条件','参数分支仍需要专门复习，特别是t=1且s不等于2时的矛盾与无解。'),
  ('confused','特征值与特征向量的定义','还不理解为什么定义特征向量时必须排除零向量，尚未做过特征值题。'),
  ('understood','正交投影','读过投影是最小二乘几何解释，目前只是自述理解，尚未做相关练习。'),
 ]),
]

def main():
 load_env(ROOT / '.env')
 report = json.loads(REPORT.read_text()) if REPORT.exists() else {'simulated':True,'learner_id':LEARNER,'started_at':now(),'stages':[]}
 with Store(ROOT / 'runtime/learning.sqlite3') as store:
  if not report.get('session_id'):
   backup=sqlite3.connect(ROOT / 'runtime/before_demo_simulation.sqlite3')
   store.connection.backup(backup);backup.close()
   session=create_session(store,'巩固矩阵运算、秩与方程组，定位并修复参数讨论的薄弱环节',LEARNER)
   report['session_id']=session['id'];REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2))
  sid=report['session_id'];model=ChatModel();retriever=HippoRetriever()
  search=lambda q,k=8,m=12000:retriever.retrieve(store,model,q,k,m)
  problems={p['source_id']:p for p in store.rows('SELECT * FROM problems')}
  def point(label):
   try:return store.query_point(label)['id']
   except ValueError:
    candidates=[p for p in store.known_points() if label in p['name']]
    if len(candidates)!=1:raise ValueError(f'知识点匹配不唯一: {label}: {candidates}')
    return candidates[0]['id']
  def save():
   report['model_stats']=report.get('model_stats',[])+model.call_stats;model.call_stats=[]
   REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2))
  if not get_session(store,sid)['learning_plan']['version']:
   plan_learning(store,model,sid,search,'demo_initial');save()
  done={s['number'] for s in report['stages']}
  for number,(title,items) in enumerate(STAGES,1):
   if number in done:continue
   print(f'STAGE {number}/{len(STAGES)} {title}',flush=True)
   for i,(kind,target,content) in enumerate(items):
    rid=f'record_demo_{number:02d}_{i:02d}'
    if store.rows('SELECT id FROM learning_records WHERE id=?',(rid,)):continue
    payload={'id':rid,'kind':kind,'content':PREFIX+content}
    if kind=='attempt':
     p=problems[target];payload['problem_id']=p['id']
     payload['point_ids']=[x['point_id'] for x in store.rows('SELECT point_id FROM problem_points WHERE problem_id=?',(p['id'],))]
    else:payload['point_ids']=[point(target)]
    if kind=='question':
     session=get_session(store,sid)
     result=ask(store,model,content,retriever=search,context={'student_model':session['student_model'],'teaching_state':session['teaching_state']})
     payload['qa_id']=result['id'];print('QA',result['status'],flush=True)
    submit_record(store,sid,payload)
   while records(store,sid,1,True):update_student(store,model,sid)
   session=plan_learning(store,model,sid,search,'demo_'+str(number))
   report['stages'].append({'number':number,'title':title,'record_count':len(items),'session':session})
   save();print('PLAN',session['learning_plan']['version'],'MASTERY_POINTS',len(session['student_model']['points']),flush=True)
  report['finished_at']=now();report['final_session']=get_session(store,sid)
  report['record_count']=store.rows('SELECT count(*) n FROM learning_records WHERE session_id=?',(sid,))[0]['n']
  report['pending_count']=len(records(store,sid,100,True));save()
  print('DONE',report['record_count'],'records',flush=True)
if __name__=='__main__':main()
