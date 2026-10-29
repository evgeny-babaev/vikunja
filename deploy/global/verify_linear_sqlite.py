"""Проверка колонок, комментариев и связей импорта. SQLite открывается read-only."""
import json
from pathlib import Path
import sqlite3
import sys

source, db_path = map(Path, sys.argv[1:3])
manifest = json.loads((source / 'manifest.json').read_text())
state = json.loads((source / 'state.json').read_text())
issues = []
for name in manifest['issue_files']:
    issues.extend(json.loads((source / name).read_text()))
errors = []
with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as db:
    for issue in issues:
        tid = state['tasks'][issue['uuid']]
        project = state['projects'][issue.get('projectId') or 'unprojected']
        row = db.execute('SELECT bucket_id FROM task_buckets WHERE task_id=? AND project_view_id=?',
                         (tid, project['view'])).fetchone()
        if not row or row[0] != project['buckets'][issue['status']]:
            errors.append([issue['id'], 'bucket'])
        description = db.execute('SELECT description FROM tasks WHERE id=?', (tid,)).fetchone()[0]
        for value in [issue['uuid'], issue['createdAt'], issue['updatedAt']]:
            if value not in description: errors.append([issue['id'], 'metadata', value])
        for item in issue.get('stateHistory') or []:
            if item['startedAt'] not in description: errors.append([issue['id'], 'stateHistory'])
        for comment in issue.get('comments') or []:
            cid = state['comments'][comment['id']]
            row = db.execute('SELECT task_id, comment FROM task_comments WHERE id=?', (cid,)).fetchone()
            if not row or row[0] != tid or comment['id'] not in row[1] or comment['createdAt'] not in row[1]:
                errors.append([issue['id'], 'comment', comment['id']])
    for relation in state['relations']:
        a,b,kind = relation.split(':')
        if db.execute('SELECT count(*) FROM task_relations WHERE task_id=? AND other_task_id=? AND relation_kind=?',
                      (a,b,kind)).fetchone()[0] != 1:
            errors.append(['relation',relation])
    counts = dict(db.execute('SELECT id, count(*) FROM tasks GROUP BY id').fetchall())
    if any(counts.get(tid) != 1 for tid in state['tasks'].values()): errors.append(['task count'])
    integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    if integrity != 'ok': errors.append(['integrity',integrity])
report = {'tasks':len(issues),'comments':len(state['comments']),'relations':len(state['relations']),
          'buckets_verified':len(issues),'integrity':integrity,'errors':errors}
(source / 'sqlite-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False))
if errors: raise SystemExit(1)
