"""Scoped provisional dispositions and deterministic later evidence comparison.

Original calculation results are immutable inputs. Evidence selectors are explicit;
PDF/image amounts require a reviewed transcription, never guessed extraction.
"""
import csv
import hashlib
import io
import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

CONFIG = 'ia/config/demo_company_supporting_evidence.json'
BASE = Path('data/demo_company/derived')
RESULT = 'supporting_evidence_result.json'
CSV = '잠정확정_추가증빙대사.csv'
AXES = {'work': ('실제 과금 작업량', '작업량'),
        'contract': ('계약 적용·실비 적정성', '적용 단가'),
        'actual_cost': ('계약 적용·실비 적정성', '실비'),
        'stated_rate': ('계약서·청구 기재단가', '사용자 채택 단가')}


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest().upper()


def load(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def numeric(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value).replace(',', '').strip())
        return n if n.is_finite() else None
    except InvalidOperation:
        return None


def source_file(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or '\\' in relative or ':' in relative or '..' in relative.split('/'):
        raise ValueError('evidence_path_invalid')
    path = root / relative
    boundary = root / 'data/demo_company/source'
    if not path.resolve().is_relative_to(boundary) or not path.is_file():
        raise ValueError('evidence_outside_source')
    for p in (path, *path.parents):
        if p == root:
            break
        if p.is_symlink() or p.is_junction():
            raise ValueError('evidence_redirected_path')
    return path


def source_value(root, record):
    path = source_file(root, record['path'])
    if sha(path) != record['sha256'].upper():
        raise ValueError('evidence_hash_changed')
    selector = record['selector']
    if selector['format'] == 'csv' and path.suffix.lower() == '.csv':
        number = selector['row']
        if type(number) is not int or number < 2:
            raise ValueError('evidence_csv_row_invalid')
        with path.open(encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise ValueError('evidence_csv_headers_ambiguous')
            value = next((r[selector['column']] for i, r in enumerate(reader, 2) if i == number), None)
        ref = f"CSV!{number} / {selector['column']}"
    elif selector['format'] == 'xlsx' and path.suffix.lower() == '.xlsx':
        from wms_tools import xlsx_rows
        match = re.fullmatch(r'([A-Z]+)([1-9][0-9]*)', selector['cell'])
        if not match:
            raise ValueError('evidence_cell_invalid')
        col, number = match[1], int(match[2])
        cells = next((r for n, r in xlsx_rows(path, selector['sheet'], {col}) if n == number), {})
        cell = cells.get(col, {})
        value = cell.get('value') if not cell.get('error') else None
        ref = selector['sheet'] + '!' + selector['cell']
    elif selector['format'] == 'xlsb' and path.suffix.lower() == '.xlsb':
        from portable_workbook import open_workbook
        from xlsb_scan import scan_rows
        match = re.fullmatch(r'([A-Z]+)([1-9][0-9]*)', selector['cell'])
        if not match:
            raise ValueError('evidence_cell_invalid')
        col, number = match[1], int(match[2])
        if number > 1048576 or len(col) > 3:
            raise ValueError('evidence_cell_invalid')
        value = None
        with open_workbook(str(path)) as book:
            if selector['sheet'] not in book.sheets:
                raise ValueError('evidence_sheet_not_found')
            for n, cells in scan_rows(book, selector['sheet']):
                if n > number:
                    break
                if n == number:
                    cell = cells.get(col, {})
                    value = cell.get('value') if not cell.get('error') else None
                    break
        ref = selector['sheet'] + '!' + selector['cell'] + ' (원본 저장값)'
    elif selector['format'] == 'reviewed_transcription':
        if selector.get('confirmed') is not True or not selector.get('reference'):
            raise ValueError('transcription_review_required')
        value, ref = selector.get('value'), selector['reference']
    else:
        raise ValueError('unsupported_evidence_selector')
    value = numeric(value)
    if value is None:
        raise ValueError('evidence_numeric_value_required')
    return value, record['path'] + ' / ' + ref


def active_policy(root, full, config=None):
    path = Path(root) / CONFIG
    p = config if config is not None else load(path) if path.is_file() else {}
    if not p or p.get("enabled") is False:
        return None
    if p.get('company_id') != 'demo_company' or p.get('warehouse_id') != 'demo_center' or p.get('invoice_month') != '2029-07':
        raise ValueError('supporting_scope_invalid')
    if p.get('claim_sha256', '').upper() != full['sources']['claim']['sha256'].upper():
        return None  # Never carry a July approval/evidence to a different invoice.
    if p.get('decision', {}).get('id') != 'U44' or p['decision'].get('state') != 'provisionally_confirmed_by_user':
        raise ValueError('provisional_decision_required')
    return p


def evidence_sources(root):
    path = Path(root) / CONFIG
    if not path.is_file():
        return {}
    cfg = load(Path(root) / 'ia/config/demo_company_direct_wms.json')
    policy = active_policy(root, {'sources': cfg['sources']})
    out = {}
    for e in (policy or {}).get('entries', []):
        for s in e['sources']:
            path = source_file(root, s['path'])
            actual = sha(path)
            if actual != s['sha256'].upper():
                raise ValueError('evidence_hash_changed')
            out[s['path']] = actual
    return out


def evaluate(root, cost, full, config=None):
    policy = active_policy(root, full, config)
    output = {'schema_version': 1, 'decision_id': 'U44', 'active': bool(policy), 'rows': [],
              'counts': {}, 'invoice_approved': False, 'llm_used': False}
    if not policy:
        return output
    output['claim_sha256'] = policy['claim_sha256']
    source_items = {i['row']: i for i in full['claim_items']}
    entries = {}
    used_sources = set()
    for entry in policy.get('entries', []):
        key = (entry['axis'], entry['invoice_row'])
        if key in entries or key[0] not in ('work','contract','actual_cost') or key[1] not in source_items:
            raise ValueError('duplicate_or_unknown_evidence_target')
        if entry.get('scope_confirmed') is not True or entry.get('coverage_complete') is not True:
            raise ValueError('evidence_scope_or_completeness_not_confirmed')
        if entry.get('currency') != 'KRW' or entry.get('amount_basis') != 'supply_amount':
            raise ValueError('evidence_currency_or_tax_basis_mismatch')
        if entry.get('period') != policy['period']:
            raise ValueError('evidence_period_mismatch')
        if not entry.get('sources') or (key[0] == 'contract' and len(entry['sources']) != 1):
            raise ValueError('evidence_values_ambiguous')
        values, refs = [], []
        for s in entry['sources']:
            # Do not count the same cell/receipt twice, even on two invoice lines.
            selector = s['selector']
            identity = (key[0], s['sha256'].upper(), json.dumps({k:v for k,v in selector.items() if k not in ('value','confirmed')}, sort_keys=True))
            if identity in used_sources:
                raise ValueError('duplicate_evidence_value')
            used_sources.add(identity)
            value, ref = source_value(root, s)
            values.append(value); refs.append(ref)
        entries[key] = (entry, sum(values, Decimal(0)), refs)
    for item in cost['items']:
        n = item['row']; original = source_items[n]
        axes = ['work', 'contract'] + (['actual_cost'] if item.get('actual_cost_application') or ('actual_cost',n) in entries else [])
        for axis in axes:
            col = {'work': 'M', 'contract': 'L', 'actual_cost': 'N'}[axis]
            raw = original[{'work': 'quantity', 'contract': 'unit_price', 'actual_cost': 'amount'}[axis]]
            unit = 'KRW' if axis == 'actual_cost' else str(original.get('unit') or '')
            actual = numeric(raw)
            row = {'axis': axis, 'invoice_row': n, 'label': item['label'], 'group': AXES[axis][0],
                   'status': 'provisional', 'check_performed': False, 'actual': str(actual) if actual is not None else None,
                   'expected': None, 'difference': None, 'unit': unit, 'claim_location': f'청구내역!{col}{n}',
                   'reason': 'U44 사용자 요청: 현재 청구값 잠정 확정; 추가 증빙 등록 시 재대사', 'evidence': []}
            if (axis, n) in entries:
                e, expected, refs = entries.pop((axis, n))
                if e.get('unit') != unit or not unit:
                    raise ValueError('evidence_unit_mismatch')
                row.update(expected=str(expected), evidence=refs)
                if actual is None:
                    row.update(status='data_unavailable', reason=f'청구서 {row["claim_location"]}에 비교할 숫자 없음; 증빙값만 확보')
                else:
                    diff = actual - expected
                    row.update(status='match' if diff == 0 else 'difference', check_performed=True,
                               difference=str(diff), reason='등록 증빙 합계와 청구 기재값 비교')
            output['rows'].append(row)
    if entries:
        raise ValueError('evidence_axis_not_applicable_to_invoice_row')
    decision=policy.get('stated_rate_decision',{})
    if (decision.get('id')=='U45' and decision.get('state')=='provisionally_confirmed_by_user'
            and decision.get('period')==policy['period']
            and decision.get('contract_sha256')==cost.get('sources',{}).get('contract',{}).get('sha256')):
        for accepted in decision.get('rates',[]):
            n=accepted['invoice_row']; item=source_items[n]
            check=next((c for c in cost['checks'] if c['id']==f'cost.contract_rate.{n}'),None)
            rate=numeric(accepted['rate']); actual=numeric(item['unit_price'])
            if not check or rate is None or actual!=rate or numeric(check['actual'])!=rate:
                continue
            quantity=numeric(item['quantity']);amount=numeric(item['amount'])
            calculated=quantity*rate if quantity is not None else None
            difference=amount-calculated if amount is not None and calculated is not None else None
            output['rows'].append({'axis':'stated_rate','invoice_row':n,'label':check['label'],
                'group':AXES['stated_rate'][0],'status':'provisional','check_performed':False,
                'actual':str(rate),'expected':str(check['expected']),'difference':str(check['difference']),
                'unit':'원/과금단위','claim_location':f'청구내역!L{n}',
                'reason':'U45 사용자 채택 단가 잠정 확정; 계약 원문 차이 보존','evidence':check['evidence'],
                'applied_rate_comparison':{'quantity':str(quantity) if quantity is not None else None,
                    'rate':str(rate),'claim_amount':str(amount) if amount is not None else None,
                    'calculated_amount':str(calculated) if calculated is not None else None,
                    'difference':str(difference) if difference is not None else None,
                    'status':'data_unavailable' if difference is None else 'match' if difference==0 else 'difference'}})
    output['counts'] = dict(Counter(r['status'] for r in output['rows']))
    output['policy_sha256'] = hashlib.sha256(json.dumps(policy, ensure_ascii=False, sort_keys=True).encode()).hexdigest().upper()
    return output


def save_result(root, result):
    base = Path(root) / BASE; path = base / RESULT
    previous = load(path) if path.is_file() else {}
    old = {(r['axis'], r['invoice_row']): r for r in previous.get('rows', [])}
    changes = []
    for r in result['rows']:
        before = old.get((r['axis'], r['invoice_row']))
        if before and any(before.get(k) != r.get(k) for k in ('status','actual','expected','difference','evidence')):
            changes.append({'axis': r['axis'], 'invoice_row': r['invoice_row'], 'before': before['status'],
                            'after': r['status'], 'difference_before': before['difference'], 'difference_after': r['difference'],
                            'resolved': before['status'] != 'match' and r['status'] == 'match'})
    result['changes'] = changes if changes else previous.get('changes', []) if previous.get('policy_sha256') == result.get('policy_sha256') else []
    if changes:
        result['previous_result_sha256'] = sha(path)
    elif previous.get('policy_sha256') == result.get('policy_sha256') and 'previous_result_sha256' in previous:
        result['previous_result_sha256'] = previous['previous_result_sha256']
    base.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    with (base / CSV).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['청구행','항목','대사 종류','처리','청구 위치','청구값','증빙값','차이','단위','증빙 원본','사유'])
        labels = {'provisional':'잠정 확정','match':'정상','difference':'불일치','data_unavailable':'미검증'}
        for r in result['rows']:
            w.writerow([r['invoice_row'],r['label'],AXES[r['axis']][1],labels[r['status']],r['claim_location'],
                        r['actual'],r['expected'],r['difference'],r['unit'],'; '.join(r['evidence']),r['reason']])
    # Refresh existing practitioner views without changing their numerical inputs.
    index={(r['axis'],str(r['invoice_row'])):r for r in result['rows']}
    for filename in ('비용검사_결과.csv','청구전체_검사범위.csv'):
        p=base/filename
        if not p.is_file() or not result['active']:continue
        with p.open(encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f);fields=reader.fieldnames;records=list(reader)
        for r in records:
            for axis,col in (('work','WMS과금단위' if filename=='비용검사_결과.csv' else '과금단위'),
                             ('contract','계약적용' if filename=='비용검사_결과.csv' else '계약'),
                             ('actual_cost','실비적정성')):
                v=index.get((axis,r['청구행']))
                if v and col in r:r[col]=labels[v['status']]+' / U44·추가 증빙 대사'
        with p.open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(records)
    return result


def refresh(root, cost=None, full=None):
    root = Path(root)
    return save_result(root, evaluate(root, cost or load(root/BASE/'cost_verification.json'),
                                     full or load(root/BASE/'full_invoice_result.json')))


def register_and_refresh(root, registration=None):
    """Validate explicit evidence, refresh saved calculations and publish atomically."""
    import os
    import shutil
    import tempfile
    import demo_company_workflow as w
    from artifact_transaction import recover_pending, publish_files
    root=Path(root).resolve();recover_pending(root)
    before=w.snapshot(root)
    baseline=w.publication_baseline(root)
    config_before=sha(root/CONFIG) if (root/CONFIG).is_file() else None
    policy=load(root/CONFIG)
    full=load(root/BASE/'full_invoice_result.json');cost=load(root/BASE/'cost_verification.json')
    w.verify_artifacts(root,load(root/BASE/'workflow_result.json'))
    if registration is not None:
        for k in ('company_id','warehouse_id','invoice_month','claim_sha256','period'):
            if registration.get(k)!=policy.get(k):raise ValueError('evidence_registration_scope_mismatch:'+k)
        indexed={(e['axis'],e['invoice_row']):e for e in policy.get('entries',[])}
        incoming=registration.get('entries',[])
        if not incoming:raise ValueError('evidence_entries_required')
        if len({(e['axis'],e['invoice_row']) for e in incoming})!=len(incoming):raise ValueError('duplicate_evidence_target')
        for e in incoming:indexed[(e['axis'],e['invoice_row'])]=e
        policy['entries']=list(indexed.values())
    evaluate(root,cost,full,policy)  # Fails before any publication.
    source_hashes=dict(before['sources'])
    for e in policy['entries']:
        for s in e['sources']:source_hashes[s['path']]=sha(source_file(root,s['path']))
    with tempfile.TemporaryDirectory(prefix='s4_evidence_') as td:
        stage=Path(td)
        for folder in ('ia/src','ia/config',str(BASE)):
            dest=stage/folder;dest.mkdir(parents=True,exist_ok=True)
            for p in (root/folder).iterdir():
                if p.is_file() and (folder!=str(BASE) or p.suffix in ('.json','.csv')):shutil.copyfile(p,dest/p.name)
        for relative in source_hashes:
            dest=stage/relative;dest.parent.mkdir(parents=True,exist_ok=True);os.link(root/relative,dest)
        w.save(stage/CONFIG,policy)
        w.aggregate(stage,reused=True)
        from issue_revision import prepare
        prepare(stage,root)
        w.update_report(stage)
        if w.snapshot(root)!=before or sha(root/CONFIG)!=config_before:raise ValueError('evidence_inputs_changed_during_refresh')
        if any(sha(root/p)!=h for p,h in source_hashes.items()):raise ValueError('evidence_source_changed_during_refresh')
        payload={p.relative_to(stage).as_posix():p.read_bytes() for p in (stage/BASE).iterdir() if p.suffix in ('.json','.csv')}
        report='data/demo_company/runs/가상센터/report.md'
        payload[report]=(stage/report).read_bytes();payload[CONFIG]=(stage/CONFIG).read_bytes()
        guards={p:baseline.get(p) for p in set(baseline)|set(payload)};guards[CONFIG]=config_before
        publish_files(root,payload,expected_files=guards)
    return load(root/BASE/RESULT)
