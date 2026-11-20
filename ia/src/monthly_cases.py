"""Month-scoped intake and immutable publication. Never mutates the July case."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

BASE = Path('data/demo_company/monthly')
SOURCE = Path('data/demo_company/source/월별등록')
ROLES = {'claim':{'.xlsb','.xlsx','.xlsm'}, 'inbound':{'.xlsx','.csv'}, 'outbound':{'.xlsx','.csv'},
         'contract':{'.pdf','.xlsx','.csv'}, 'master':{'.xlsx','.csv'}, 'rates':{'.xlsx','.csv'}}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def load(path, default=None):
    return json.loads(Path(path).read_text('utf-8-sig')) if Path(path).is_file() else default


def save(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    temp.replace(path)


def safe(root, relative, boundary):
    root=Path(root).resolve();p=root/relative;b=root/boundary
    if not p.resolve().is_relative_to(b.resolve()) or b.resolve()!=b or p.resolve()!=p:
        raise ValueError('허용 범위 밖 파일 경로입니다.')
    for parent in (p,*p.parents):
        if parent==root:break
        if parent.is_symlink() or (hasattr(parent,'is_junction') and parent.is_junction()):
            raise ValueError('연결된 외부 경로는 사용할 수 없습니다.')
    return p


def case_dir(root, month):
    if not isinstance(month,str) or not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',month):
        raise ValueError('청구월은 YYYY-MM 형식이어야 합니다.')
    # The original case is intentionally outside this intake path.
    if month=='2029-07':raise ValueError('2029-07은 기존 검증 화면에서 관리합니다. 원본 결과를 덮어쓰지 않습니다.')
    return safe(root,BASE/month,BASE)


@contextmanager
def locked(folder):
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/'case.lock'
    try:fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise ValueError('이 청구월을 처리 중입니다. 완료 후 다시 시도해 주세요.') from None
    try:
        os.close(fd);yield
    finally:path.unlink(missing_ok=True)


def register(root, payload):
    root=Path(root).resolve();month=payload['month'];folder=case_dir(root,month)
    start=date.fromisoformat(payload['start']);end=date.fromisoformat(payload['end'])
    if start>end or (end-start).days>93:raise ValueError('청구기간은 시작일 이후 93일 이내로 지정해 주세요.')
    if end.strftime('%Y-%m')!=month:raise ValueError('청구월과 기간 종료월이 다릅니다. 청구기간을 확인해 주세요.')
    if payload.get('confirmed') is not True:raise ValueError('회사·센터·기간·연결 기준을 확인해 주세요.')
    rules=payload.get('rules',{})
    if rules.get('consumables') not in ('include','exclude'):raise ValueError('소모품 포함 기준을 선택해 주세요.')
    if rules.get('blank_unit') not in ('ask','EA'):raise ValueError('빈 수량 단위 기준을 선택해 주세요.')
    if rules.get('yearless_date') not in ('ask','period'):raise ValueError('연도 없는 날짜 기준을 선택해 주세요.')
    if not rules.get('company') or not rules.get('warehouse'):raise ValueError('WMS 회사·창고 표기를 입력해 주세요.')
    for key,allowed in {'packing_group':('unconfirmed','shipment','order_carrier_tracking'),
                        'prepacked':('unconfirmed','include','exclude_explicit'),
                        'carton_basis':('unconfirmed','master_then_claim'),
                        'unresolved_policy':('ask','record')}.items():
        if key in rules and rules[key] not in allowed:raise ValueError('지원하지 않는 비교 기준: '+key)
    if rules.get('packing_threshold') is not None and (type(rules['packing_threshold']) is not int or not 1<=rules['packing_threshold']<=10000):
        raise ValueError('기본포장 포함 개수는 1~10,000의 정수로 입력해 주세요.')
    materials=payload.get('materials',[])
    counts={r:sum(m.get('role')==r for m in materials) for r in ROLES}
    if not counts['claim'] or not counts['inbound'] or not counts['outbound']:
        raise ValueError('청구서와 입고·출고 WMS를 각각 1개 이상 등록해 주세요.')
    checked=[];seen=set()
    for m in materials:
        role=m['role']
        if role not in ROLES:raise ValueError('지원하지 않는 자료 역할입니다.')
        if m.get('reuse'):
            if role not in ('contract','master'):raise ValueError('계약·Master만 명시적으로 재사용할 수 있습니다.')
            src=safe(root,m['reuse'],'data/demo_company/source')
        else:
            token=m['token']
            if Path(token).name!=token:raise ValueError('잘못된 업로드 식별자입니다.')
            src=safe(root,Path('frontend/.runtime/uploads')/token,'frontend/.runtime/uploads')
        if not src.is_file() or src.suffix.lower() not in ROLES[role]:raise ValueError('자료 확장자 또는 파일 위치를 확인해 주세요.')
        h=digest(src)
        if h in seen:raise ValueError('같은 파일을 중복 등록할 수 없습니다. 겹치는 월별 WMS도 합계 중복 여부를 확인해 주세요.')
        seen.add(h);checked.append((role,src,h))
    with locked(folder):
        state=load(folder/'state.json',{})
        if state.get('processing_status')=='running':raise ValueError('실행 중에는 자료를 교체할 수 없습니다.')
        revision=uuid.uuid4().hex[:12];records=[]
        for i,(role,src,h) in enumerate(checked):
            filename=re.sub(r'[^\w.가-힣()-]','_',src.name)
            dest=safe(root,SOURCE/month/revision/f'{i+1}_{filename}',SOURCE)
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dest)
            if digest(dest)!=h:raise ValueError('등록 도중 파일이 변경되었습니다.')
            records.append({'role':role,'path':dest.relative_to(root).as_posix(),'sha256':h,'name':src.name})
        case={'month':month,'company':'DEMO COMPANY','warehouse':'가상센터','company_id':'demo_company',
              'revision':revision,'period':{'start':start.isoformat(),'end':end.isoformat()},'currency':'KRW',
              'rules':rules,'materials':records,'confirmed_at':datetime.now(timezone.utc).isoformat(),
              'approval':'해당 등록 자료·기간에만 적용. 기존 7월 결정은 승계하지 않음'}
        save(folder/'case.json',case)
        # A previous completed pointer remains available after replacing input.
        save(folder/'state.json',{'processing_status':'registered','waiting_for_input':False,'revision':revision})
    return {'month':month,'registered':True,'revision':revision,'explanation':f'{month} 자료를 등록했습니다. 월별 검증에서 실행해 주세요.'}


def verify_sources(root, case):
    for m in case['materials']:
        p=safe(root,m['path'],SOURCE)
        if not p.is_file() or digest(p)!=m['sha256']:raise ValueError('등록 원본이 변경되었거나 없습니다: '+m['name'])


def identity(case):
    return hashlib.sha256(json.dumps(case,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def current_result(root, month, pending=False):
    folder=case_dir(root,month)
    pointer=load(folder/('pending.json' if pending else 'latest.json'))
    if not pointer:return None
    for rel,h in pointer['hashes'].items():
        p=safe(root,rel,BASE/month)
        if not p.is_file() or digest(p)!=h:raise ValueError('월별 결과 근거 파일이 변경되었습니다.')
    return load(root/pointer['result'])


def publish(root, case, result, report, csv_text):
    folder=case_dir(root,case['month'])
    if identity(load(folder/'case.json'))!=identity(case):raise ValueError('실행 중 자료 등록이 바뀌었습니다.')
    verify_sources(root,case)
    out=folder/'results'/uuid.uuid4().hex[:12];out.mkdir(parents=True)
    result.update(report=(out/'report.md').relative_to(root).as_posix(),
                  csv=(out/'확인목록.csv').relative_to(root).as_posix())
    save(out/'result.json',result)
    (out/'report.md').write_text(report,encoding='utf-8')
    (out/'확인목록.csv').write_text(csv_text,encoding='utf-8-sig')
    pointer={'revision':case['revision'],'result':(out/'result.json').relative_to(root).as_posix(),
             'hashes':{p.relative_to(root).as_posix():digest(p) for p in out.iterdir()}}
    # Publish all artifacts by switching one pointer only after verification.
    name='pending.json' if result['waiting_for_input'] else 'latest.json'
    save(folder/name,pointer)
    if not result['waiting_for_input']:
        (folder/'pending.json').unlink(missing_ok=True)
    retained={Path(p['result']).parent for n in ('latest.json','pending.json') if (p:=load(folder/n))}
    boundary=(folder/'results').resolve()
    # Keep only the last complete result and the current pending preview for this month.
    for old in boundary.iterdir():
        if old.relative_to(root) in retained:continue
        if not old.is_dir() or old.resolve().parent!=boundary or old.is_symlink() or old.is_junction():
            continue
        if any(p.is_symlink() or p.is_junction() for p in old.rglob('*')):continue
        try:shutil.rmtree(old)
        except OSError:pass  # A locked old artifact must not invalidate the published result.
    return result
