"""Импорт приватного снимка Linear через API v2. Данные и mapping — вне Git.

python3 import_linear.py SNAPSHOT_DIR ACCESS_JSON STATE_JSON [--verify-only]
VIKUNJA_IMPORT_BASE может указывать внутренний HTTP-адрес того же контейнера.
"""
import collections
import datetime
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request


PRIORITIES = {0: 0, 1: 4, 2: 3, 3: 2, 4: 1}
CLOSED = {'completed', 'canceled', 'duplicate'}
STATUS_ORDER = ['Triage', 'Backlog', 'Todo', 'Todo now', 'In Progress', 'Done', 'Canceled', 'Duplicate']


def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def markdown(text):
    # Linear embeds issue tags in Markdown. Convert them to ordinary links.
    return re.sub(r'<issue\b[^>]*href="([^"]+)"[^>]*>(.*?)</issue>',
                  lambda m: f'[{m[2]}]({m[1]})', text or '', flags=re.S)


class API:
    def __init__(self, access):
        self.base = os.environ.get('VIKUNJA_IMPORT_BASE', access['url']).rstrip('/')
        self.token = None
        self.token = self.request('/api/v1/login', 'POST', {
            'username': access['username'], 'password': access['password']})['token']

    def request(self, path, method='GET', body=None):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        for attempt in range(6):
            req = urllib.request.Request(self.base + path,
                json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=45) as response:
                    raw = response.read()
                    if method != 'GET': time.sleep(0.025)
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                detail = e.read().decode()
                if e.code == 304 and method in {'PUT', 'PATCH'}: return body
                if e.code == 429 and attempt < 5:
                    time.sleep(max(2, int(e.headers.get('Retry-After', 5)))); continue
                # SQLite may briefly contend with background event handlers.
                # Only retry idempotent operations; POST is reconciled on restart.
                if e.code >= 500 and method in {'GET', 'PUT', 'PATCH'} and attempt < 5:
                    time.sleep(1 + attempt); continue
                raise RuntimeError(f'{method} {path}: {e.code} {detail}') from None
        raise RuntimeError('Retry budget exhausted')

    def items(self, path):
        out, page = [], 1
        while True:
            result = self.request(path + ('&' if '?' in path else '?') + f'page={page}&per_page=50')
            if isinstance(result, list):
                out.extend(result)
                if len(result) < 50: return out
            else:
                out.extend(result.get('items') or [])
                if page >= result.get('total_pages', 1): return out
            page += 1


def description(issue):
    metadata = [f"Источник: [{issue['id']}]({issue['url']})",
        f"Linear UUID: `{issue['uuid']}`",
        f"Статус в Linear: **{issue['status']}**",
        f"Создано в Linear: {issue['createdAt']}",
        f"Обновлено в Linear: {issue['updatedAt']}"]
    for key, title in [('completedAt', 'Завершено в Linear'), ('archivedAt', 'Архивировано в Linear'),
                       ('dueDate', 'Срок в Linear (дата без времени)'), ('assignee', 'Исполнитель в Linear')]:
        if issue.get(key): metadata.append(f'{title}: {issue[key]}')
    if issue.get('estimate') is not None:
        metadata.append(f"Оценка Linear: {issue['estimate']['value']} points (не часы)")
    if issue['statusType'] in {'canceled', 'duplicate'} or issue.get('archivedAt'):
        metadata.append('В Vikunja закрыта для исключения из активных задач; это не означает выполненную работу.')
    history = issue.get('stateHistory') or []
    if history:
        metadata.append('\n### История статусов Linear')
        for row in history:
            metadata.append(f"- {row.get('startedAt')} → {row.get('endedAt') or 'по момент выгрузки'}: {row['state']['name']}")
    for key, title in [('attachments', 'Ссылки и вложения Linear'), ('documents', 'Документы Linear')]:
        if issue.get(key):
            metadata.append('\n### ' + title)
            for item in issue[key]:
                url = item.get('url')
                label = item.get('title') or item.get('name') or item.get('id') or 'Документ'
                metadata.append(f'- [{label}]({url})' if url else '- ' + json.dumps(item, ensure_ascii=False))
                if item.get('content'):
                    metadata.append(markdown(item['content']))
    return markdown(issue.get('description')) + '\n\n---\n## Данные миграции из Linear\n\n' + '\n\n'.join(metadata)


def run():
    source, access_path, state_path = map(Path, sys.argv[1:4])
    snapshot = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    issues = []
    for filename in snapshot['issue_files']:
        issues.extend(json.loads((source / filename).read_text(encoding='utf-8')))
    documents = {d['id']:d for d in (json.loads(f.read_text(encoding='utf-8')) for f in source.glob('document-*.json'))}
    for issue in issues:
        issue['documents'] = [documents.get(d['id'], d) for d in issue.get('documents') or []]
    assert len(issues) == snapshot['issue_count']
    assert len({i['uuid'] for i in issues}) == len(issues)
    assert all(not re.search(r'truncated, use `get_issue`', i.get('description') or '') for i in issues)
    access = json.loads(access_path.read_text(encoding='utf-8'))
    api = API(access)
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {
        'projects': {}, 'tasks': {}, 'labels': {}, 'enriched': [], 'comments': {}, 'relations': []}
    save = lambda: atomic_json(state_path, state)
    projects = snapshot['projects'] + [{'uuid': 'unprojected', 'name': 'Linear — без проекта', 'description': ''}]
    projects = [p for p in projects if any((i.get('projectId') or 'unprojected') == p['uuid'] for i in issues)]
    imported = lambda i: state['tasks'][i['uuid']]
    if '--verify-only' not in sys.argv:
        existing_projects = api.items('/api/v2/projects')
        for p in projects:
            key = p['uuid']
            if key not in state['projects']:
                marker = f'Linear project UUID: {key}'
                matches = [x for x in existing_projects if marker in (x.get('description') or '')]
                assert len(matches) < 2
                body = {'title': p['name'], 'description': markdown(p.get('description')) +
                        '\n\n---\n' + marker + '\n\n' + (p.get('url') or '')}
                if p.get('resources'):
                    body['description'] += '\n\n' + '\n'.join(
                        f"- [{r.get('label') or r.get('title') or r.get('id')}]({r.get('url')})" for r in p['resources'])
                created = matches[0] if matches else api.request('/api/v2/projects?format=markdown', 'POST', body)
                state['projects'][key] = {'id': created['id']}; save()
            target = state['projects'][key]
            pid = target['id']
            views = api.items(f'/api/v2/projects/{pid}/views')
            view = next(x for x in views if x['view_kind'] == 'kanban')
            target['view'] = view['id']
            # Temporarily disable automatic done-bucket moves while restoring historical columns.
            api.request(f"/api/v2/projects/{pid}/views/{view['id']}", 'PATCH', {'done_bucket_id': 0})
            buckets = api.items(f"/api/v2/projects/{pid}/views/{view['id']}/buckets")
            by_name = {x['title']: x for x in buckets}
            for old, new in [('To-Do', 'Todo'), ('Doing', 'In Progress')]:
                if old in by_name and new not in by_name:
                    b = by_name.pop(old)
                    b = api.request(f"/api/v2/projects/{pid}/views/{view['id']}/buckets/{b['id']}", 'PUT',
                                    {'title': new, 'position': (STATUS_ORDER.index(new)+1)*100})
                    by_name[new] = b
            wanted = set(STATUS_ORDER[:6]) | {i['status'] for i in issues if (i.get('projectId') or 'unprojected') == key}
            for name in STATUS_ORDER:
                if name not in wanted: continue
                if name not in by_name:
                    by_name[name] = api.request(f"/api/v2/projects/{pid}/views/{view['id']}/buckets", 'POST',
                                               {'title': name, 'position': (STATUS_ORDER.index(name)+1)*100})
                else:
                    api.request(f"/api/v2/projects/{pid}/views/{view['id']}/buckets/{by_name[name]['id']}", 'PUT',
                                {'title': name, 'position': (STATUS_ORDER.index(name)+1)*100})
            target['buckets'] = {k:v['id'] for k,v in by_name.items()}; save()
        print('Projects and columns ready:', len(projects), flush=True)
        # Reconcile markers before creating tasks: safe to resume after an interrupted POST.
        existing_tasks = api.items('/api/v2/tasks')
        for t in existing_tasks:
            match = re.search(r'Linear UUID:.*?([0-9a-f]{8}-[0-9a-f-]{27,})', t.get('description') or '')
            if match and match[1] not in state['tasks']:
                state['tasks'][match[1]] = t['id']
        save()
        known_labels = {x['title']: x['id'] for x in api.items('/api/v2/labels')}
        def label(name):
            if name not in known_labels:
                known_labels[name] = api.request('/api/v2/labels', 'POST', {'title': name})['id']
            state['labels'][name] = known_labels[name]
            return known_labels[name]
        for n, issue in enumerate(sorted(issues, key=lambda x:x['createdAt']), 1):
            key = issue['uuid']; project = state['projects'][issue.get('projectId') or 'unprojected']
            if key not in state['tasks']:
                body = {'title': f"[{issue['id']}] {issue['title']}", 'description': description(issue),
                        'priority': PRIORITIES[issue.get('priority', {}).get('value', 0)],
                        'done': bool(issue['statusType'] in CLOSED or issue.get('archivedAt'))}
                if issue.get('dueDate'): body['due_date'] = issue['dueDate'] + 'T23:59:59+03:00'
                result = api.request(f"/api/v2/projects/{project['id']}/tasks?format=markdown", 'POST', body)
                state['tasks'][key] = result['id']; save()
            tid = imported(issue)
            if key not in state['enriched']:
                names = list(issue.get('labels') or [])
                if issue.get('estimate') is not None: names.append(f"Linear estimate: {issue['estimate']['value']} points")
                if issue['statusType'] in {'canceled', 'duplicate'}: names.append('Linear: ' + issue['status'])
                if issue.get('archivedAt'): names.append('Linear: Archived')
                existing = {x['id'] for x in api.items(f'/api/v2/tasks/{tid}/labels')}
                for name in names:
                    lid = label(name)
                    if lid not in existing: api.request(f'/api/v2/tasks/{tid}/labels', 'POST', {'label_id': lid})
                if issue.get('assigneeId') == snapshot['user']['id']:
                    if not api.items(f'/api/v2/tasks/{tid}/assignees'):
                        api.request(f'/api/v2/tasks/{tid}/assignees', 'POST', {'user_id': 1})
                api.request(f"/api/v2/projects/{project['id']}/views/{project['view']}/buckets/{project['buckets'][issue['status']]}/tasks",
                            'PUT', {'task_id': tid})
                for comment in issue.get('comments', []):
                    cid = comment['id']
                    if cid in state['comments']: continue
                    marker = f'Linear comment ID: {cid}'
                    previous = api.items(f'/api/v2/tasks/{tid}/comments')
                    match = next((x for x in previous if marker in x['comment']), None)
                    if match:
                        state['comments'][cid] = match['id']; continue
                    author = comment.get('author') or {}
                    author = author.get('name', author.get('id', 'неизвестен')) if isinstance(author, dict) else author
                    text = f"**Из Linear · {author} · {comment.get('createdAt', '')}**\n\n" + markdown(comment.get('body'))
                    if comment.get('quotedText'): text += '\n\nЦитируемый фрагмент:\n\n' + markdown(comment['quotedText'])
                    text += '\n\n' + marker
                    if comment.get('parentId'): text += '\n\nРодительский комментарий Linear: ' + comment['parentId']
                    if comment.get('resolvedAt'): text += '\n\nОбсуждение закрыто в Linear: ' + comment['resolvedAt']
                    result = api.request(f'/api/v2/tasks/{tid}/comments?format=markdown', 'POST', {'comment': text})
                    state['comments'][cid] = result['id']; save()
                state['enriched'].append(key); save()
            if n % 50 == 0 or n == len(issues): print(f'Tasks: {n}/{len(issues)}', flush=True)
        by_id = {i['id']:i for i in issues}
        by_id.update({i['uuid']:i for i in issues})
        edges = set()
        unresolved = []
        def edge(issue, other, kind):
            other = other.get('id') if isinstance(other, dict) else other
            if not other: return
            if other not in by_id:
                unresolved.append({'source':issue['id'], 'target':other, 'kind':kind}); return
            a,b = imported(issue), imported(by_id[other])
            if kind == 'related': a,b = sorted([a,b])
            if kind == 'blocked': a,b,kind = b,a,'blocking'
            if a != b: edges.add((a,b,kind))
        for issue in issues:
            edge(issue, issue.get('parentId'), 'parenttask')
            rel = issue.get('relations') or {}
            for k,kind in [('blocks','blocking'),('blockedBy','blocked'),('relatedTo','related')]:
                for other in rel.get(k,[]): edge(issue,other,kind)
            edge(issue,rel.get('duplicateOf'),'duplicateof')
        for a,b,kind in sorted(edges):
            key = f'{a}:{b}:{kind}'
            if key in state['relations']: continue
            current = api.request(f'/api/v2/tasks/{a}').get('related_tasks') or {}
            if not any(t['id']==b for t in current.get(kind, [])):
                api.request(f'/api/v2/tasks/{a}/relations', 'POST', {'other_task_id':b,'relation_kind':kind})
            state['relations'].append(key); save()
        state['unresolved_relations'] = unresolved
        for p in state['projects'].values():
            api.request(f"/api/v2/projects/{p['id']}/views/{p['view']}", 'PATCH',
                        {'done_bucket_id': p['buckets']['Done'], 'default_bucket_id': p['buckets']['Todo']})
        save()
        print('Relations:',len(state['relations']),'unresolved:',len(unresolved), flush=True)
    verify(api, state, issues, state_path, snapshot['user']['id'])


def verify(api, state, issues, state_path, source_user_id):
    actual = {t['id']:t for t in api.items('/api/v2/tasks')}
    failures = []
    for i in issues:
        tid = state['tasks'].get(i['uuid']); t = actual.get(tid)
        if not t: failures.append([i['id'],'missing']); continue
        checks = {'title': t['title'] == f"[{i['id']}] {i['title']}",
                  'priority': t['priority'] == PRIORITIES[i['priority']['value']],
                  'done': t['done'] == bool(i['statusType'] in CLOSED or i.get('archivedAt')),
                  'source': i['uuid'] in t['description'],
                  'project': t['project_id'] == state['projects'][i.get('projectId') or 'unprojected']['id']}
        if i.get('dueDate'):
            due = datetime.datetime.fromisoformat(t['due_date'].replace('Z','+00:00'))
            checks['due_date'] = due.astimezone(datetime.timezone(datetime.timedelta(hours=3))).date().isoformat()==i['dueDate']
        names = {l['title'] for l in t.get('labels') or []}
        checks['labels'] = set(i.get('labels') or []).issubset(names)
        if i.get('estimate') is not None: checks['estimate'] = f"Linear estimate: {i['estimate']['value']} points" in names
        if i.get('assigneeId'): checks['assignee'] = bool(t.get('assignees')) if i['assigneeId']==source_user_id else True
        failures.extend([i['id'],k] for k,v in checks.items() if not v)
    report = {'source_tasks':len(issues),'imported_tasks':len(state['tasks']),
              'projects':len(state['projects']),'comments':len(state['comments']),
              'relations':len(state['relations']),'failures':failures,
              'source_status_counts':dict(collections.Counter(i['status'] for i in issues)),
              'unresolved_relations':state.get('unresolved_relations',[])}
    atomic_json(state_path.with_name('verification.json'),report)
    print(json.dumps(report,ensure_ascii=False),flush=True)
    if failures: raise RuntimeError('Verification failed')


if __name__ == '__main__':
    run()
