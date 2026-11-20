"""Atomic checkbox decisions, independent of numeric verdicts and chat text."""
import json
import time
import uuid
from business_memory import BusinessMemory,digest,VersionConflict

def _schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS checklist_events (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL, saved_at REAL NOT NULL) STRICT')
    db.execute('CREATE TABLE IF NOT EXISTS checklist_invalidations (id TEXT PRIMARY KEY REFERENCES checklist_events(id), saved_at REAL NOT NULL) STRICT')
    for table in ('checklist_events','checklist_invalidations'):
        for op in ('UPDATE','DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{op.lower()}_blocked BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_checklist'); END")

def save(root,targets,status,source_fingerprint,expected_revision):
    if status not in ('reviewed','review_required','reset'):raise ValueError('처리완료·재검토·처리 해제 중 선택하세요.')
    if not targets or len(targets)>15000 or len({t['id'] for t in targets})!=len(targets):raise ValueError('처리할 항목을 중복 없이 선택하세요.')
    store=BusinessMemory(root);payload=dict(targets=targets,status=status,source_fingerprint=source_fingerprint,scope='current_snapshot',actor='local-pc-owner')
    with store._connect(write=True) as db:
        revision=db.execute('SELECT revision FROM memory_head WHERE singleton=1').fetchone()[0]
        if revision!=expected_revision:raise VersionConflict('다른 처리가 저장되었습니다. 목록을 갱신해 다시 선택하세요.')
        _schema(db);event_id=uuid.uuid4().hex
        db.execute('INSERT INTO checklist_events VALUES(?,?,?,?,?)',(event_id,revision+1,json.dumps(payload,ensure_ascii=False),digest(payload),time.time()))
        db.execute('UPDATE memory_head SET revision=revision+1 WHERE singleton=1')
    return dict(event_id=event_id,count=len(targets),status=status)

def events(root,fingerprint=None):
    store=BusinessMemory(root)
    with store._connect() as db:
        if db is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='checklist_events'").fetchone():return []
        entries=[]
        for row in db.execute('SELECT e.*, i.id AS invalid FROM checklist_events e LEFT JOIN checklist_invalidations i ON i.id=e.id ORDER BY revision'):
            payload=json.loads(row['payload'])
            if digest(payload)!=row['payload_hash']:raise ValueError('처리 기록이 변경되었습니다.')
            entries.append(dict(row,payload=payload))
    stale=[e['id'] for e in entries if not e['invalid'] and fingerprint is not None and e['payload']['source_fingerprint']!=fingerprint]
    if stale:
        with store._connect(write=True) as db:
            for key in stale:db.execute('INSERT OR IGNORE INTO checklist_invalidations VALUES(?,?)',(key,time.time()))
            db.execute('UPDATE memory_head SET revision=revision+1 WHERE singleton=1')
        for entry in entries:
            if entry['id'] in stale:entry['invalid']=entry['id']
    return entries

def project(root,rows):
    items=events(root)
    if not items:return rows
    from chat_commands import source_snapshot
    try:stamp=digest(source_snapshot(root))
    except (ValueError,OSError) as exc:stamp=digest({'unavailable':str(exc)})
    items=events(root,stamp);latest={}
    from review_catalog import signature,identity
    by_identity={}
    for row in rows:by_identity.setdefault(identity(row),[]).append(row)
    for e in items:
        for target in e['payload']['targets']:
            matches=by_identity.get(target.get('identity'),[])
            if len(matches)>1 and not e['invalid']:
                matches=[r for r in matches if r['_id']==target['id']]
            if len(matches)==1:latest[matches[0]['_id']]=(e,target)
    chat_events={e['disposition_id']:e['disposition_version'] for e in BusinessMemory(root).list_dispositions(limit=None)}
    for row in rows:
        if row['_id'] not in latest:continue
        e,target=latest[row['_id']]
        if chat_events.get(row.get('_review_event'),0)>e['revision']:continue
        # A cancelled completion has no approval left to invalidate.
        stale=(e['invalid'] or signature(row)!=target['signature']) if e['payload']['status']!='reset' else False
        status='review_required' if stale else e['payload']['status']
        if status=='reset':status='unreviewed'
        row.update(_review_status=status,_review_event=e['id'],_review_invalidated=bool(stale),
            _review_reason='근거 변경으로 재검토 필요' if stale else '담당자 체크박스 처리완료' if status=='reviewed' else '담당자 재검토 요청' if status=='review_required' else '',
            _checklist_event=True)
    return rows
