"""Versioned business artifacts; compatibility never invents learning evidence."""
from copy import deepcopy
import uuid

VERSION = 2

def stamp(value):
    return {**value, 'schema_version': VERSION}

def student_model(value):
    result = stamp(deepcopy(value))
    result.setdefault('points', {})
    for pid, point in result['points'].items():
        point.setdefault('point_id', pid)
        point.setdefault('self_report', None)
        point.setdefault('performance', None)
        point.setdefault('misconceptions', [])
        point.setdefault('evidence_summary', {})
    return result

def session_artifacts(sid, state, plan):
    state, plan = stamp(deepcopy(state)), stamp(deepcopy(plan))
    goal_id = state.setdefault('goal_id', 'goal_' + sid.removeprefix('session_'))
    state.setdefault('target_point_ids', [])
    state.setdefault('success_criteria', {'kind': 'evidence_review', 'description': '完成当前动作后，依据新作答检查目标进展'})
    state.setdefault('current_issue', None)
    plan.setdefault('goal_id', goal_id if plan.get('goal')==state.get('goal') else 'goal_previous_'+sid.removeprefix('session_'))
    plan.setdefault('archived_actions', [])
    for action in plan.get('actions', []):
        decorate_action(action, plan['goal_id'])
    return state, plan

def decorate_action(action, goal_id):
    action.setdefault('schema_version', VERSION)
    action.setdefault('goal_id', goal_id)
    action.setdefault('evaluation_status', 'unknown')
    action.setdefault('completion_record_ids', [])
    action.setdefault('completion_criteria', {'kind': 'submit_attempt' if action['type'] in ('practice','assess') else 'user_confirmation'})
    return action

def action_key(action):
    return (action['goal_id'], action['type'], action['point_id'],
            tuple(sorted(set(action['chunk_ids']))), tuple(sorted(set(action['problem_ids']))))

def reconcile_actions(old_plan, proposals, goal_id, timestamp):
    """Keep execution identities; changed/removed actions remain auditable."""
    same_goal = old_plan.get('goal_id') == goal_id
    kept = [deepcopy(a) for a in old_plan['actions'] if same_goal and a['status']=='completed']
    reusable = {action_key(a): a for a in old_plan['actions'] if same_goal and a['status']=='pending'}
    used, signatures = set(), set()
    for proposal in proposals:
        a = decorate_action(deepcopy(proposal), goal_id)
        key = action_key(a)
        if key in signatures:
            raise ValueError('计划包含重复动作')
        signatures.add(key)
        old = reusable.get(key)
        if old:
            a = {**deepcopy(old), **a}
            used.add(old['id'])
        else:
            a.update(id='action_'+uuid.uuid4().hex, status='pending')
        # Proposed text must not reset evidence metadata.
        if old:
            for field in ('id','status','completion_record_ids','evaluation_status'):
                a[field] = deepcopy(old[field])
        kept.append(a)
    archived = deepcopy(old_plan.get('archived_actions', []))
    for a in old_plan['actions']:
        if not same_goal or (a['status']=='pending' and a['id'] not in used):
            archived.append({**deepcopy(a), 'status':'superseded' if a['status']=='pending' else a['status'],
                             'archived_at': timestamp, 'archive_reason':'replanned' if same_goal else 'goal_changed'})
    return kept, archived
