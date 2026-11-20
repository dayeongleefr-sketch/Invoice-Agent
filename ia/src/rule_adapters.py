"""C4 bounded, read-only recalculation against registered original documents.

This module does not publish results or adopt business policy. The caller must
bind its returned preview to a saved rule and obtain explicit confirmation.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import json
from pathlib import Path
import re

import chat_intake
from claim_wms_rules import explain, july_date
from invoice_tools import digest, number
from portable_workbook import open_workbook
from wms_tools import day, key_text, validated_rows, xlsb_rows, xlsx_rows

VERSION = 'quantity-components-v1'
BASE = Path('data/demo_company')
CONFIG = Path('ia/config/demo_company_direct_wms.json')
RESULT = BASE / 'derived/direct_wms_result.json'
LIMITATIONS = [
    '선택한 입고·B2B 상품 수량만 다시 비교합니다. 주문 연결·BOX/PCS·포장·금액·계약 적정성은 변경하지 않습니다.',
    '청구 기재 수량은 유지하며 WMS 품명에 (소모품)이 명시된 수량만 포함하거나 제외합니다.',
    '이번 계산은 현재 등록된 2029년 7월 청구에 한합니다. 앞으로 적용하도록 저장해도 다른 월에 자동 적용하지 않습니다.',
]


def _json(root, relative):
    return json.loads(chat_intake._safe_file(root, relative).read_text('utf-8-sig'))


def _decimal(value):
    if value is None or isinstance(value, bool):
        raise ValueError('원본 수량이 없거나 숫자가 아닙니다.')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('원본 수량이 숫자가 아닙니다.') from exc
    if not result.is_finite() or result < 0:
        raise ValueError('유효하지 않은 수량입니다. 별도 기준이 필요합니다.')
    return result


def code_fingerprint(root):
    root = Path(root).resolve()
    files = ('rule_adapters.py', 'claim_wms_rules.py', 'wms_tools.py',
             'invoice_tools.py', 'chat_intake.py')
    payload = {name: digest(chat_intake._safe_file(root, Path('ia/src') / name)) for name in files}
    # Fingerprint the imported implementations also in isolated test roots.
    import cost_references, formula_rules, calculation_expression, rate_rules, cost_verification, xlsb_scan, identifier_tools, quantity_rematch, b2c_link_rules, actual_claim_wms_b2c, pack_conversion_rules, billing_quantity_tools
    for module in (cost_references,formula_rules,calculation_expression,rate_rules,cost_verification,xlsb_scan,identifier_tools,quantity_rematch,b2c_link_rules,actual_claim_wms_b2c,pack_conversion_rules,billing_quantity_tools):
        payload[module.__name__]=digest(Path(module.__file__))
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _record(record):
    if not isinstance(record, dict):
        raise ValueError('저장한 기준을 선택해 주세요.')
    return record.get('payload', record)


def _targets(root, stored_record, *, allow_unlinked=False):
    record = _record(stored_record)
    if record.get('intent') != 'upsert_rule' or record.get('month') != '2029-07':
        raise ValueError('현재는 등록된 2029년 7월 청구의 비교 기준만 실행할 수 있습니다.')
    targets = record.get('targets')
    if not isinstance(targets, list) or not targets or len(targets) > 100:
        raise ValueError('현재 확인 목록에서 1~100개 내역을 선택해 주세요.')
    current = chat_intake._current(root)
    doc = _json(root, RESULT)
    workflow = _json(root, BASE / 'derived/workflow_result.json')
    expected = workflow.get('result_hashes', {}).get(RESULT.name)
    if not expected or digest(root / RESULT).lower() != str(expected).lower():
        raise ValueError('원본 대사 결과의 버전이 일치하지 않습니다.')
    selected, seen = [], set()
    for target in targets:
        index = str(target.get('issue_id', ''))
        if not index.isdecimal() or int(index) >= len(current['rows']):
            raise ValueError('선택한 확인 목록이 바뀌었습니다.')
        row = current['rows'][int(index)]
        canonical = chat_intake._target(row, int(index), current['version'])
        if {k:v for k,v in target.items() if k != 'alias'} != canonical:
            raise ValueError('선택한 청구건의 자료·결과 버전이 바뀌었습니다.')
        ref = re.fullmatch(r'direct_wms_result\.json#/rows/(\d+)', row.get('계산근거', ''))
        if not ref or target.get('check') not in ('입고 상품 수량', 'B2B 상품 수량') or target.get('unit') != 'EA':
            raise ValueError('선택 내역 중 원본과 연결된 입고·B2B 상품 수량이 아닌 항목이 있습니다.')
        number_ = int(ref[1])
        if number_ in seen or number_ >= len(doc.get('rows', [])):
            raise ValueError('중복되거나 잘못된 계산 근거입니다.')
        seen.add(number_)
        calculation = doc['rows'][number_]
        if (calculation.get('scope') not in ('inbound_june', 'inbound_july', 'outbound')
                or not calculation.get('claim_present') or not calculation.get('claim_evidence')
                or (not allow_unlinked and (not calculation.get('wms_present') or not calculation.get('wms_evidence')))):
            raise ValueError('청구와 WMS가 모두 연결되고 수량이 기재된 내역만 재계산할 수 있습니다.')
        selected.append((target, calculation))
    return selected, doc


def options(root, stored_record):
    try: result = _base_options(root, stored_record)
    except (ValueError,OSError,KeyError,TypeError) as exc: result = dict(supported=False,message=str(exc),choices=[])
    from formula_rules import options as formula_options
    choice = formula_options(root, stored_record)
    if choice:
        result = dict(result, supported=True, choices=list(result.get('choices', []))+[choice])
    return result


def _base_options(root, stored_record):
    from pack_conversion_rules import CHECKS, options as pack_options
    if any(t.get('check') in CHECKS for t in _record(stored_record).get('targets',[])):
        return pack_options(Path(root).resolve(),stored_record)
    if any(t.get('check')=='B2C 주문 연결' for t in _record(stored_record).get('targets',[])):
        from b2c_link_rules import options as link_options
        return link_options(Path(root).resolve(),stored_record)
    if any(t.get('check')=='선택 단가 금액 비교' for t in _record(stored_record).get('targets',[])):
        from rate_rules import options as rate_options
        result=rate_options(root,stored_record)
        if _record(stored_record).get('scope')=='future':
            from recurring_rate_rules import options as recurring_options
            try:
                choice=recurring_options(root,stored_record)
                if choice:
                    choices=[] if _record(stored_record).get('application')=='future_only' else result['choices']
                    return dict(result,supported=True,choices=choices+[choice],message='계속 적용할 항목·단위·시작월과 단가를 확인하세요.')
            except (ValueError,OSError,KeyError) as exc:
                if _record(stored_record).get('application')=='future_only':return {'supported':False,'message':str(exc),'choices':[]}
        return result
    from quantity_rematch import choice as rematch_choice
    advanced = rematch_choice(Path(root).resolve(), stored_record)
    try:
        _targets(Path(root).resolve(), stored_record)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return {'supported': bool(advanced), 'message': '새 연결 기준을 선택해 원본을 비교합니다.' if advanced else str(exc), 'choices': [advanced] if advanced else []}
    result={'supported': True, 'message': '원본에 명시된 소모품의 비교 수량 포함 여부를 선택해 주세요.',
            'choices': [{'kind': 'quantity_components', 'label': 'WMS 소모품 포함·제외',
                         'parameters': [{'name': 'consumables', 'label': 'WMS 비교 수량의 소모품 처리',
                                         'type': 'select', 'options': {'include': '소모품 포함', 'exclude': '명시된 소모품 제외'},
                                         'value': 'include', 'required': True}],
                         'description': '청구 수량은 유지하고 원본 WMS 수량을 다시 합산합니다.',
                         'limitations': LIMITATIONS}]}
    if _record(stored_record).get('scope')=='future':
        from recurring_quantity_rules import options as recurring_options
        try:
            choice=recurring_options(root,stored_record)
            choices=[] if _record(stored_record).get('application')=='future_only' else result['choices']
            return dict(result,choices=choices+[choice],message='미래 적용 업무 범위와 소모품 포함 여부를 확인하세요.')
        except (ValueError,OSError,KeyError) as exc:
            if _record(stored_record).get('application')=='future_only':return {'supported':False,'message':str(exc),'choices':[]}
    if advanced: result['choices'].append(advanced)
    return result


def _collect_rows(iterator, wanted):
    found = {}
    try:
        for n, cells in iterator:
            if n in wanted:
                if n in found:
                    raise ValueError('원본 행 번호가 중복되어 있습니다.')
                found[n] = cells
            if n >= max(wanted):
                break
    finally:
        close = getattr(iterator, 'close', None)
        if close:
            close()
    if set(found) != wanted:
        raise ValueError('원본에서 계산 근거 행을 찾지 못했습니다.')
    return found


def _read_claim(path, requested, config):
    result = {}
    with open_workbook(str(path)) as book:
        for sheet_name, wanted in requested.items():
            mappings = [m for m in config['claim_mappings'].values() if m['claim_sheet'] == sheet_name]
            if len(mappings) != 1:
                raise ValueError('청구 양식 연결이 유일하지 않습니다.')
            with book.get_sheet(sheet_name) as sheet:
                rows = xlsb_rows(sheet)
                try:
                    found = _collect_rows(validated_rows(rows, mappings[0]['claim_headers']), wanted)
                finally:
                    rows.close()
            result.update({('claim', sheet_name, n): c for n,c in found.items()})
    return result


XLSX_HEADERS = {
    'inbound_h1': {'A1':'회사구분', 'B1':'입고일', 'F1':'품번', 'I1':'수량(EA)', 'M1':'입고창고'},
    'inbound_july': {'B1':'회사구분', 'D1':'입고일', 'F1':'입고예정번호', 'Q1':'품번', 'V1':'단위', 'W1':'수량', 'AK1':'입고창고'},
    'outbound_h1': {'B1':'출고일', 'I1':'구분', 'J1':'주문번호(연동)', 'P1':'품번', 'T1':'수량'},
}
SHEETS = {'inbound_h1':'입고 Data', 'inbound_july':'Sheet1', 'outbound_h1':'출고 Data(04월~06월)'}
XLSX_COLUMNS = {'inbound_h1': {'A','B','F','G','I','M'},
                'inbound_july': {'B','D','F','Q','R','V','W','AK'},
                'outbound_h1': {'B','I','J','P','Q','T'}}


def _read_wms(paths, requested):
    result = {}
    for (role, sheet), wanted in requested.items():
        if role == 'outbound_july':
            if sheet != 'CSV':
                raise ValueError('WMS CSV 위치가 올바르지 않습니다.')
            with paths[role].open(encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream)
                required = {'회사구분','출고창고','단위','출고일','주문번호(연동)','품번','품명','수량','구분','출고번호'}
                if not required.issubset(reader.fieldnames or []):
                    raise ValueError('WMS CSV 열 제목이 바뀌었습니다.')
                found = _collect_rows(enumerate(reader, 2), wanted)
            if any(None in c or None in c.values() for c in found.values()):
                raise ValueError('WMS CSV 행 형식이 올바르지 않습니다.')
        elif role in SHEETS and sheet == SHEETS[role]:
            rows = xlsx_rows(paths[role], sheet, columns=XLSX_COLUMNS[role])
            try:
                found = _collect_rows(validated_rows(rows, XLSX_HEADERS[role]), wanted)
            finally:
                rows.close()
        else:
            raise ValueError('지원하지 않는 원본 WMS 역할·시트입니다.')
        result.update({(role, sheet, n): c for n,c in found.items()})
    return result


def _evidence_key(evidence):
    role = evidence.get('source')
    sheet = 'CSV' if role == 'outbound_july' else evidence.get('sheet')
    row = evidence.get('csv_record') if role == 'outbound_july' else evidence.get('row')
    if not isinstance(row, int) or isinstance(row, bool) or row < 2:
        raise ValueError('잘못된 원본 행 위치입니다.')
    return role, sheet, row


def _wms_value(role, c, decisions, cost_decisions):
    if role == 'outbound_july':
        if (c['회사구분'],c['출고창고'],c['단위']) != ('90','DM001','EA'):
            raise ValueError('WMS 회사·창고·수량 단위가 일치하지 않습니다.')
        from identifier_tools import expand_identifier
        return ((july_date(c['출고일'], decisions['yearless_date']), expand_identifier(c['주문번호(연동)'])),
                _decimal(c['수량'].replace(',', '').strip()), c['품명'])
    if role == 'outbound_h1':
        unit = cost_decisions.get('june_b2b_unit', {})
        if unit.get('state') != 'confirmed_by_user' or unit.get('mode') != 'EA':
            raise ValueError('6월 B2B 수량의 EA 해석 결정이 필요합니다.')
        return ((day(c.get('B')),key_text(c.get('J'))), _decimal(number(c.get('T'))), str(c.get('Q',{}).get('value','')))
    if role == 'inbound_h1':
        company, warehouse = key_text(c.get('A')), key_text(c.get('M'))
        key, qty, name = (day(c.get('B')),key_text(c.get('F'))), c.get('I'), c.get('G',{})
    elif role == 'inbound_july':
        company, warehouse = key_text(c.get('B')), key_text(c.get('AK'))
        if key_text(c.get('V')) != 'EA':
            raise ValueError('입고 수량 단위가 EA가 아닙니다.')
        key, qty, name = (day(c.get('D')),key_text(c.get('F')),key_text(c.get('Q'))), c.get('W'), c.get('R',{})
    else:
        raise ValueError('지원하지 않는 WMS 역할입니다.')
    if (company,warehouse) != ('[90] DEMO COMPANY','[DM001]DEMO'):
        raise ValueError('입고 회사·창고가 일치하지 않습니다.')
    return key, _decimal(number(qty)), str(name.get('value',''))


def _values(claim, comparison):
    calculation = explain({'quantity':claim}, {'quantity':comparison, 'consumables':Decimal(0)})
    return {'claim':str(claim), 'comparison':str(comparison),
            'difference':str(calculation['difference_claim_minus_wms']),
            'status':'match' if calculation['kind']=='quantity_match' else 'difference', 'unit':'EA'}


def calculate(root, stored_record, kind, parameters):
    if kind == 'formula':
        from formula_rules import calculate as formula_calculate
        return formula_calculate(root, stored_record, parameters)
    if kind == 'pack_conversion':
        from pack_conversion_rules import calculate as pack_calculate
        return pack_calculate(root,stored_record,parameters)
    if kind == 'b2c_link':
        from b2c_link_rules import calculate as link_calculate
        return link_calculate(root,stored_record,parameters)
    if kind == 'quantity_rematch':
        from quantity_rematch import calculate as rematch
        return rematch(Path(root).resolve(), stored_record, parameters)
    if kind=='recurring_quantity':
        from recurring_quantity_rules import calculate as calculate_recurring
        return calculate_recurring(Path(root).resolve(),stored_record,parameters)
    if kind=='recurring_rate':
        from recurring_rate_rules import calculate as calculate_recurring
        return calculate_recurring(Path(root).resolve(),stored_record,parameters)
    if kind=='custom_rate':
        from rate_rules import calculate as calculate_rates
        return calculate_rates(Path(root).resolve(),stored_record,parameters,custom=True)
    if kind=='rate_selection':
        from rate_rules import calculate as calculate_rates
        return calculate_rates(Path(root).resolve(),stored_record,parameters)
    if kind != 'quantity_components' or not isinstance(parameters, dict) or set(parameters) != {'consumables'} or parameters['consumables'] not in ('include','exclude'):
        raise ValueError('지원하지 않는 비교 기준 또는 매개변수입니다.')
    return quantity_calculation(root, stored_record, parameters)


def quantity_calculation(root, stored_record, parameters, *, with_facts=False):
    root = Path(root).resolve()
    selected, doc = _targets(root, stored_record)
    cfg = _json(root, CONFIG)
    decisions = _json(root, Path(cfg['decisions_path']))
    cost_decisions = _json(root, Path('ia/config/demo_company_cost_decisions.json'))
    period = decisions.get('comparison_period', {})
    if (not isinstance(doc.get('period'), dict) or not all(period.get(k) for k in ('start','end'))
            or any(doc['period'].get(k)!=period.get(k) for k in ('start','end'))
            or period.get('state')!='confirmed_by_user'):
        raise ValueError('비교기간 결정과 계산 근거가 다릅니다.')
    claim_requested, wms_requested, used = defaultdict(set), defaultdict(set), set()
    for _, calculation in selected:
        for side in ('claim','wms'):
            local = set()
            for ev in calculation[side+'_evidence']:
                role, sheet, row = _evidence_key(ev)
                if (role,sheet,row) in local or (side == 'claim') != (role == 'claim'):
                    raise ValueError('원본 근거가 중복되거나 역할이 잘못되었습니다.')
                local.add((role,sheet,row));used.add(role)
                (claim_requested[sheet] if side=='claim' else wms_requested[(role,sheet)]).add(row)
    paths = {}
    for role in used:
        source = cfg['sources'].get(role)
        if not source or role not in ('claim','inbound_h1','inbound_july','outbound_h1','outbound_july'):
            raise ValueError('내부 검증파일은 이 계산의 입력으로 사용할 수 없습니다.')
        if doc.get('sources', {}).get(role) != source:
            raise ValueError('대사 결과와 등록된 원본 버전이 다릅니다.')
        path = chat_intake._safe_file(root, Path(source['path']))
        if not path.resolve().is_relative_to((root/BASE/'source').resolve()):
            raise ValueError('원본 경계를 벗어난 자료입니다.')
        if digest(path).upper() != source['sha256'].upper():
            raise ValueError('등록 후 원본이 바뀌었습니다. 먼저 새 자료로 전체 검증해 주세요.')
        paths[role] = path
    if with_facts:
        for sheet in claim_requested:claim_requested[sheet].add(2)
    originals = _read_claim(paths['claim'], claim_requested, cfg)
    originals.update(_read_wms(paths, wms_requested))
    changes = []; facts = []
    for target, calculation in selected:
        expected_key = tuple(calculation['key']);scope = calculation['scope']
        if not period['start'] <= expected_key[0] <= period['end']:
            raise ValueError('선택 내역이 현재 청구기간 밖에 있습니다.')
        claim, wms, consumables = Decimal(0), Decimal(0), Decimal(0)
        refs = []
        direction = 'outbound' if scope=='outbound' else 'inbound'
        mapping = cfg['claim_mappings'][direction]
        for ev in calculation['claim_evidence']:
            role,sheet,row = _evidence_key(ev);c = originals[(role,sheet,row)]
            if sheet != mapping['claim_sheet']:
                raise ValueError('청구 구분과 원본 시트가 다릅니다.')
            m = mapping['claim'];key = (day(c.get(m['date'])),)+tuple(key_text(c.get(k)) for k in m['key'])
            if scope=='inbound_june':key = (key[0],key[-1])
            if key != expected_key:
                raise ValueError('청구 원본의 일자·번호·품번이 계산 대상과 다릅니다.')
            claim += _decimal(number(c.get(m['quantity'])))
            refs.append(cfg['sources'][role]['path']+' / '+sheet+'!'+str(row))
        allowed = {'inbound_june':{'inbound_h1'},'inbound_july':{'inbound_july'},'outbound':{'outbound_h1','outbound_july'}}[scope]
        for ev in calculation['wms_evidence']:
            role,sheet,row = _evidence_key(ev)
            if role not in allowed:
                raise ValueError('선택 내역과 WMS 입출고 역할이 다릅니다.')
            key, qty, name = _wms_value(role,originals[(role,sheet,row)],decisions,cost_decisions)
            if key != expected_key:
                raise ValueError('WMS 원본의 일자·번호·품번이 계산 대상과 다릅니다.')
            wms += qty
            if '(소모품)' in name:consumables += qty
            refs.append(cfg['sources'][role]['path']+' / '+sheet+'!'+str(row))
        if (claim,wms,consumables) != tuple(_decimal(calculation.get(k)) for k in ('claim_quantity','wms_quantity','wms_consumables')):
            raise ValueError('원본 재집계와 저장된 수량이 다릅니다. 전체 검증을 먼저 실행해 주세요.')
        before = _values(claim,wms)
        if any(_decimal(target.get(k)) != _decimal(before[v]) for k,v in (('claim_value','claim'),('comparison_value','comparison'))):
            raise ValueError('선택한 확인 목록과 원본 계산 수량이 다릅니다.')
        if Decimal(str(target.get('difference'))) != Decimal(before['difference']):
            raise ValueError('확인 목록에 표시된 차이가 원본 수량 차이와 다릅니다.')
        comparator = wms-consumables if parameters['consumables']=='exclude' else wms
        changes.append({'target_id':target['target_id'], 'check':target['check'], 'label':target['label'],
                        'before':before, 'after':_values(claim,comparator),
                        'basis':f"청구 기재 수량 유지; WMS 전체 {wms}EA, 품명에 (소모품) 명시 {consumables}EA " + ('제외' if parameters['consumables']=='exclude' else '포함'),
                        'evidence_refs':refs})
        if with_facts:
            def optional_label(c):
                return str((c or {}).get('value') or '').strip() if not (c or {}).get('error') else ''
            channels={optional_label(originals[_evidence_key(e)].get('C')) for e in calculation['claim_evidence']} if direction=='outbound' else set()
            header=optional_label(originals.get(('claim',mapping['claim_sheet'],2),{}).get('C'))
            names=[_wms_value(*(_evidence_key(e)[0], originals[_evidence_key(e)], decisions, cost_decisions))[2] for e in calculation['wms_evidence']]
            facts.append({'청구수량':claim,'WMS수량':wms,
                '소모품수량':consumables if all(n.strip() for n in names) else None,
                '상품수량':wms-consumables if all(n.strip() for n in names) else None,
                '일자':expected_key[0],'번호':expected_key[1] if scope!='inbound_june' else None,
                '품번':expected_key[-1] if direction=='inbound' else None,
                '거래처':next(iter(channels)) if header=='채널명' and len(channels)==1 and '' not in channels else None})
    for role,path in paths.items():
        if digest(path).upper() != cfg['sources'][role]['sha256'].upper():
            raise ValueError('재계산 중 원본이 바뀌었습니다.')
    _targets(root, stored_record)
    result = {'adapter_version':VERSION, 'changes':changes, 'limitations':LIMITATIONS,'calculation_executed':True}
    return (result,facts) if with_facts else result
