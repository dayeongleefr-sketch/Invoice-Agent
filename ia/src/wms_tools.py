"""Local, streaming document comparisons. No inferred key substitutions."""
from collections import defaultdict, Counter
from contextlib import ExitStack
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
import json
import re
import itertools
import zipfile
from identifier_tools import expand_identifier
from lxml import etree as ET

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
RID = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'


def xlsx_rows(path, sheet_name, columns=None):
    """Yield nonempty rows with original row numbers; preserve text and error types."""
    row_tag, data_tag, value_tag = NS+'row', NS+'sheetData', NS+'v'
    text_tag = NS+'t'
    with ExitStack() as resources:
        book = resources.enter_context(zipfile.ZipFile(path))
        strings = []
        if 'xl/sharedStrings.xml' in book.namelist():
            for _, element in ET.iterparse(resources.enter_context(book.open('xl/sharedStrings.xml')),
                                           events=('end',), tag=NS+'si', resolve_entities=False, no_network=True):
                if element.tag == NS+'si':
                    strings.append(''.join(t.text or '' for t in element.iter(NS+'t')))
                    element.clear()
                    element.getparent().remove(element)
        relationships = {e.attrib['Id']: e.attrib['Target'] for e in ET.fromstring(book.read('xl/_rels/workbook.xml.rels'))}
        sheets = ET.fromstring(book.read('xl/workbook.xml')).find(NS+'sheets')
        selected = [s for s in sheets if s.attrib['name'] == sheet_name]
        if len(selected) != 1:
            raise ValueError('registered_sheet_missing')
        target = relationships[selected[0].attrib[RID]]
        target = target.lstrip('/') if target.startswith('/') else 'xl/'+target
        # Filter row events in the C parser, rather than dispatching every
        # cell/value start and end into Python in million-row worksheets.
        stream = ET.iterparse(resources.enter_context(book.open(target)), events=('end',),
                              tag=row_tag, resolve_entities=False, no_network=True)
        for event, element in stream:
            row = {}
            row_number = int(element.attrib['r'])
            for c in element:
                # Cell references are column letters followed by a row number.
                # This runs for every cell in million-row WMS workbooks.
                col = c.attrib.get('r', '').rstrip('0123456789')
                if columns and col not in columns:
                    continue
                value = None
                formula_cached = False
                # Visit children once instead of running two XPath searches
                # for each cell in million-row worksheets.
                for child in c:
                    if child.tag == value_tag:
                        value = child.text
                    elif child.tag == NS+'f':
                        formula_cached = True
                kind = c.attrib.get('t', 'n')
                if kind == 's' and value is not None:
                    value = strings[int(value)]
                elif kind == 'inlineStr':
                    value = ''.join(t.text or '' for t in c.iter(text_tag))
                elif kind == 'n' and value is not None:
                    value = Decimal(value)
                elif kind == 'b':
                    value = value == '1'
                if value is not None:
                    row[col] = {'value': value, 'error': kind == 'e', 'row': row_number,
                                'cell_type': kind, 'formula_cached': formula_cached}
            if row:
                yield row_number, row
            element.clear()
            # Remove only the consumed row; keep queued siblings intact and
            # avoid retaining a cleared element for every historical row.
            element.getparent().remove(element)


def xlsb_rows(sheet):
    from invoice_tools import cells
    last, row = None, {}
    for address, cell in cells(sheet):
        if last is not None and cell['row'] != last:
            yield last, row
            row = {}
        last = cell['row']
        row[re.sub(r'\d+', '', address)] = cell
    if row:
        yield last, row


def key_text(cell):
    if not cell or cell.get('error') or isinstance(cell['value'], bool):
        raise ValueError('missing_key')
    text = expand_identifier(cell['value'])
    if not text:
        raise ValueError('missing_key')
    # Do not remove prefixes, leading zeros, or apply number offsets.
    return text


def day(cell):
    value = key_text(cell)
    serial = (float(cell['value']) if isinstance(cell['value'], (int, float, Decimal))
              and Decimal(1) < Decimal(str(cell['value'])) < Decimal(100000) else None)
    return _day_text(value, serial)


@lru_cache(maxsize=8192)
def _day_text(value, serial):
    if re.fullmatch(r'\d{8}', value):
        return datetime.strptime(value, '%Y%m%d').date().isoformat()
    if re.fullmatch(r'\d{4}[-.]\s*\d{1,2}[-.]\s*\d{1,2}', value):
        return datetime(*map(int, re.findall(r'\d+', value))).date().isoformat()
    if serial is not None and Decimal(1) < Decimal(value) < Decimal(100000):
        return (datetime(1899, 12, 30)+timedelta(days=serial)).date().isoformat()
    raise ValueError('invalid_date')


def aggregate(rows, mapping, start=2):
    from invoice_tools import number
    groups, rejected, dates = {}, [], Counter()
    for index, row in rows:
        if index < start:
            continue
        if not any(col in row for col in mapping['key']):
            continue  # decorative/subtotal rows, not transactions
        try:
            date = day(row.get(mapping['date']))
            key = (date,)+tuple(key_text(row.get(col)) for col in mapping['key'])
            qty = number(row.get(mapping['quantity']))
            if mapping.get('unit') and key_text(row.get(mapping['unit'])) != 'EA':
                raise ValueError('unsupported_quantity_unit')
        except ValueError as exc:
            rejected.append({'row': index, 'reason': str(exc),
                             'note': str(row.get(mapping.get('note'), {}).get('value', ''))})
            continue
        dates[date[:7]] += 1
        group = groups.setdefault(key, {'quantity': Decimal(0), 'rows': [], 'consumables': Decimal(0), 'consumable_rows': [], 'signatures': {}, 'source_rows': []})
        group['quantity'] += qty
        group['rows'].append(index)
        group['source_rows'].append({'row': index, 'quantity': qty,
            'name': str(row.get(mapping.get('name'), {}).get('value', '')),
            'sku': str(row.get(mapping.get('sku'), {}).get('value', '')),
            'note': str(row.get(mapping.get('note'), {}).get('value', '')),
            'explicit_consumable': '(소모품)' in str(row.get(mapping.get('name'), {}).get('value', '')),
            'charges': {col: row.get(col, {}).get('value') for col in mapping.get('charges', [])}})
        if mapping.get('name') and '(소모품)' in str(row.get(mapping['name'], {}).get('value', '')):
            group['consumables'] += qty
            group['consumable_rows'].append(index)
        if mapping.get('signature'):
            signature = json.dumps([str(row.get(c, {}).get('value', '')) for c in mapping['signature']], ensure_ascii=False)
            entry = group['signatures'].setdefault(signature, {'quantity': qty, 'rows': []})
            entry['rows'].append(index)
    return groups, rejected, dict(sorted(dates.items()))


def compare_groups(claim, reference):
    results = {'matched': [], 'different': [], 'claim_only': [], 'reference_only': []}
    for key in sorted(set(claim)|set(reference)):
        a, b = claim.get(key), reference.get(key)
        status = 'claim_only' if b is None else 'reference_only' if a is None else 'matched' if a['quantity'] == b['quantity'] else 'different'
        results[status].append({'key': list(key), 'claim': a, 'reference': b,
                                'difference': a['quantity']-b['quantity'] if a and b else None})
    return results


def apply_exclusion_policy(claim, reference, policy):
    """Keep originals; apply user exclusions without inventing claim SKU allocation."""
    output = {'basis': policy, 'matched': [], 'different': [], 'allocation_unknown': [],
              'consumables': [], 'duplicate_scenarios': [], 'unlinked': [],
              'reference_consumables_total': Decimal(0), 'claim_explicit_consumables_total': Decimal(0)}
    duplicate_keys = {tuple(k) for k in policy.get('duplicate_order_keys', [])}
    for key, a in claim.items():
        b = reference.get(key)
        if b is None:
            output['unlinked'].append({'key': list(key), 'claim_quantity': a['quantity'], 'claim_rows': a['rows']})
            continue
        excluded = b['consumables']
        output['reference_consumables_total'] += excluded
        output['claim_explicit_consumables_total'] += a['consumables']
        if excluded or a['consumables']:
            output['consumables'].append({'key': list(key), 'reference_quantity': excluded,
                'claim_explicit_quantity': a['consumables'],
                'reference_rows': [r for r in b['source_rows'] if r['explicit_consumable']],
                'claim_rows': a['source_rows'],
                'claim_classification': 'explicit' if a['consumables'] else 'inferred_from_linked_order' if excluded == b['quantity'] else 'unallocated_mixed_order',
                'claim_order_quantity': a['quantity'], 'pure_consumable_order': excluded == b['quantity'],
                'original_difference': a['quantity']-b['quantity'], 'included_in_billable_quantity': False})
        if excluded == b['quantity'] and excluded > 0:
            # Exclude the whole order by explicit user scope; do not pretend the claim labels it.
            continue
        expected = b['quantity']-excluded
        observed = a['quantity']-a['consumables']
        removed = Decimal(0)
        if key in duplicate_keys:
            removed = sum((v['quantity']*(len(v['rows'])-1) for v in b['signatures'].values()), Decimal(0))
            # Only the explicitly selected two non-consumable orders are deduplicated.
            if excluded:
                raise ValueError('duplicate_and_consumable_scope_overlap')
            expected -= removed
            output['duplicate_scenarios'].append({'key': list(key), 'claim_quantity': observed,
                'reference_original': b['quantity'], 'reference_after_dedup': expected,
                'removed_quantity': removed, 'match': expected == observed,
                'duplicate_groups': [v for v in b['signatures'].values() if len(v['rows'])>1]})
        item = {'key': list(key), 'claim_quantity': observed, 'reference_quantity': expected,
                'difference': observed-expected, 'claim_rows': a['rows'], 'reference_rows': b['rows']}
        if excluded and not a['consumables'] and observed != expected:
            # Claim is an order total without SKU detail. Subtracting the reference's
            # consumables from both sides would manufacture a passing comparison.
            item['reason'] = '청구가 주문 합계만 있어 상품·소모품별 청구 수량 및 작업비 배분을 확정할 수 없습니다.'
            output['allocation_unknown'].append(item)
        else:
            output['matched' if observed == expected else 'different'].append(item)
    output['counts'] = {k: len(output[k]) for k in ('matched','different','allocation_unknown','consumables','duplicate_scenarios','unlinked')}
    return output


def include_consumables_comparison(claim, reference, policy):
    """Include materials without silently reverting the selected duplicate scenario."""
    selected = {tuple(k) for k in policy.get('duplicate_order_keys', [])}
    adjusted_reference = {k: dict(v) for k, v in reference.items()}
    scenarios = []
    for key in selected & set(reference) & set(claim):
        value = reference[key]
        if value['consumables']:
            raise ValueError('duplicate_and_consumable_scope_overlap')
        duplicates = [v for v in value['signatures'].values() if len(v['rows']) > 1]
        removed = sum((v['quantity'] * (len(v['rows']) - 1) for v in duplicates), Decimal(0))
        adjusted_reference[key]['quantity'] -= removed
        scenarios.append({'key': list(key), 'reference_original': value['quantity'],
                          'reference_after_dedup': adjusted_reference[key]['quantity'],
                          'claim_quantity': claim[key]['quantity'], 'removed_quantity': removed,
                          'duplicate_groups': duplicates})
    rows = compare_groups(claim, adjusted_reference)
    for item in rows['different']:
        ref = item['reference']
        item['investigation'] = {'consumables_quantity': ref['consumables'],
            'difference_equals_consumables': -item['difference'] == ref['consumables'],
            'difference_equals_repeated_rows': False}
    counts = {k: len(v) for k, v in rows.items()}
    rows['reference_only'] = rows['reference_only'][:2]
    # Signatures are preserved for the selected scenarios; keep ordinary evidence compact.
    for category in rows.values():
        for item in category:
            for side in ('claim', 'reference'):
                if item[side] is not None:
                    item[side] = {k: v for k, v in item[side].items() if k != 'signatures'}
    return {'counts': counts, 'rows': rows, 'duplicate_scenarios': scenarios,
            'basis': policy, 'consumables_included': True}


def reference_file(root, spec):
    from invoice_tools import digest
    raw = root/spec['path']
    boundary = (root/'data/demo_company/source').resolve()
    if not raw.resolve().is_relative_to(boundary):
        raise ValueError('reference_outside_scope')
    for cursor in [raw, *raw.parents]:
        if cursor == root:
            break
        if cursor.is_symlink() or cursor.is_junction():
            raise ValueError('reference_reparse_point')
    if digest(raw) != spec['sha256'].upper():
        raise ValueError('reference_hash_mismatch')
    return raw


def validated_rows(rows, headers):
    iterator = iter(rows)
    head = []
    last = max(int(re.search(r'\d+', addr).group()) for addr in headers)
    for index, row in iterator:
        head.append((index, row))
        if index >= last:
            break
    actual = {col+str(index): cell['value'] for index, row in head for col, cell in row.items()}
    if any(actual.get(addr) != label for addr, label in headers.items()):
        raise ValueError('comparison_header_changed')
    return itertools.chain(head, iterator)


def compare_documents(root, source, spec):
    from invoice_tools import digest
    from portable_workbook import open_workbook
    reference = reference_file(root, spec['reference'])
    output = {'reference': spec['reference'], 'comparisons': [], 'policy': spec.get('policy', {})}
    with open_workbook(str(source)) as book:
        for rule in spec['comparisons']:
            with book.get_sheet(rule['claim_sheet']) as sheet:
                a, invalid_a, months_a = aggregate(validated_rows(xlsb_rows(sheet), rule['claim_headers']), rule['claim'], rule['claim_start'])
            b, invalid_b, months_b = aggregate(validated_rows(xlsx_rows(reference, rule['reference_sheet']), rule['reference_headers']), rule['reference'])
            matches = compare_groups(a, b)
            adjusted = apply_exclusion_policy(a, b, spec['policy']) if spec.get('policy', {}).get('exclude_consumables') else None
            inclusive = (include_consumables_comparison(a, b, spec['policy'])
                         if spec.get('policy', {}).get('exclude_consumables') is False else None)
            for item in matches['different']:
                ref = item['reference']
                repeated = sum((v['quantity']*(len(v['rows'])-1) for v in ref['signatures'].values()), Decimal(0))
                item['investigation'] = {
                    'consumables_quantity': ref['consumables'],
                    'difference_equals_consumables': -item['difference'] == ref['consumables'],
                    'repeated_signature_quantity': repeated,
                    'difference_equals_repeated_rows': repeated != 0 and -item['difference'] == repeated,
                    'note': '원문 표시·행 반복과의 수치 비교이며 제외 조건이나 중복 청구 확정이 아닙니다.'}
            other_dates = defaultdict(list)
            for key, value in b.items():
                other_dates[key[1:]].append({'date': key[0], 'quantity': value['quantity'], 'rows': value['rows']})
            for item in matches['claim_only']:
                item['same_key_other_dates'] = other_dates[tuple(item['key'][1:])]
            # Reference-only orders are outside this B2B claim's selected keys.
            # Retain counts/months and samples, never classify them as missing invoices.
            counts = {k: len(v) for k, v in matches.items()}
            matches['reference_only'] = matches['reference_only'][:2]
            for category, items in matches.items():
                for item in items:
                    for side in ('claim', 'reference'):
                        if item[side] is not None:
                            signatures = item[side].pop('signatures', {})
                            item[side]['repeated_rows'] = [v for v in signatures.values() if len(v['rows']) > 1] if category == 'different' else []
            output['comparisons'].append({'id': rule['id'], 'label': rule['label'],
                'claim_sheet': rule['claim_sheet'], 'reference_sheet': rule['reference_sheet'],
                'mapping': rule, 'claim_month_rows': months_a, 'reference_month_rows': months_b,
                'adjusted': adjusted, 'inclusive': inclusive,
                'claim_rejected': invalid_a, 'reference_rejected': invalid_b,
                'counts': counts, 'reference_only_examples_truncated': counts['reference_only'] > 2, 'rows': matches})
        if spec.get('policy', {}).get('exclude_consumables'):
            from invoice_tools import cells, number
            with book.get_sheet('청구내역') as sheet:
                invoice = dict(cells(sheet))
            fees = []
            # Direct source row quantities only; never spread a reference SKU count
            # across an order's BOX/PCS handling charge or freight amount.
            for comparison in output['comparisons']:
                selected = comparison['adjusted']['consumables']
                charge_rows = [r for item in selected for r in item['claim_rows']
                               if r['explicit_consumable'] or item['pure_consumable_order']]
                mappings = [('L',20,'소모품 입고 BOX'),('M',21,'소모품 입고 PCS')] if comparison['id']=='inbound' else [('L',34,'소모품 전용 주문 BOX 피킹'),('M',35,'소모품 전용 주문 PCS 피킹'),('N',36,'소모품 전용 주문 BOX 패킹'),('O',37,'소모품 전용 주문 PCS 패킹')]
                for col, invoice_row, label in mappings:
                    unit = 'BOX' if col in ('L','N') else 'PCS'
                    if invoice.get('J'+str(invoice_row),{}).get('value') != unit:
                        raise ValueError('consumable_fee_unit_changed')
                    rate = number(invoice.get('L'+str(invoice_row)))
                    quantity, missing = Decimal(0), []
                    for row in charge_rows:
                        try: quantity += number({'value': row['charges'].get(col), 'error': False})
                        except ValueError: missing.append(row['row'])
                    fees.append({'label':label,'unit':unit,'known_quantity':quantity,'rate':rate,
                                 'known_excluded_amount':quantity*rate,'missing_rows':missing,
                                 'complete':not missing,'included_in_billable_amount':False,
                                 'evidence':[comparison['claim_sheet']+'!'+col, '청구내역!L'+str(invoice_row)]})
                if comparison['id']=='outbound':
                    known,missing=Decimal(0),[]
                    for row in charge_rows:
                        try:known+=number({'value':row['charges'].get('J'),'error':False})
                        except ValueError:missing.append(row['row'])
                    fees.append({'label':'소모품 전용 주문 운임 원문','known_excluded_amount':known,'missing_rows':missing,'complete':not missing,'included_in_billable_amount':False,'evidence':['B2B출고!J'], 'note':'정률 수수료·부가세 등을 포함하는 최종 차감액으로 확정하지 않음'})
            output['excluded_fee_components']=fees
            output['billable_invoice_total']=None
            output['billable_total_reason']='소모품은 청구 대상에서 제외. 혼합 주문의 비용 배분과 빈 BOX/PCS·운임이 있어 제외 후 전체 청구액은 미확정. 원문 금액은 보존.'
    if digest(reference) != spec['reference']['sha256'].upper():
        raise ValueError('reference_changed_during_comparison')
    return output


def comparison_checks(evidence):
    from invoice_tools import check
    checks = []
    reference_name = Path(evidence['reference']['path']).name
    for group in evidence['comparisons']:
        prefix, label = 'wms.'+group['id'], group['label']
        if group.get('adjusted') is not None:
            checks.extend(adjusted_checks(group, reference_name))
            continue
        inclusive = group.get('inclusive')
        rows, counts = (inclusive['rows'], inclusive['counts']) if inclusive else (group['rows'], group['counts'])
        refs = [group['claim_sheet'], reference_name+'#'+group['reference_sheet']]
        if rows['matched']:
            expected = sum((r['reference']['quantity'] for r in rows['matched']), Decimal(0))
            observed = sum((r['claim']['quantity'] for r in rows['matched']), Decimal(0))
            item = check(prefix+'.matched', label+(' 소모품 포함·지정 반복 행 조정 후 ' if inclusive else ' 동일 날짜·번호 기준 ')+str(counts['matched'])+'개 항목 수량', expected, observed, refs, 'document_comparison')
            item['unit'] = '원문 수량'
            checks.append(item)
        explained = [r for r in rows['different'] if r['investigation']['difference_equals_consumables']]
        buckets = [('consumables', explained)] if explained else []
        buckets += [('difference.'+str(i), [r]) for i, r in enumerate(rows['different']) if not r['investigation']['difference_equals_consumables']]
        for suffix, items in buckets:
            sample = items[0]
            reason = (('소모품 포함 기준으로 차이를 유지합니다. ' if inclusive else '')+'검증자료의 소모품 수량과 차이가 같은 '+str(len(items))+'개 주문. 어느 수량이 실제 과금 대상인지는 별도입니다.' if suffix == 'consumables' else
                      '검증자료의 날짜·주문·품번·수량·LOT·유통기한이 반복된 행의 수량과 차이가 같습니다. 실제 중복 출고/청구로 확정하지 않았습니다.' if sample['investigation']['difference_equals_repeated_rows'] else
                      '두 문서의 수량 차이입니다. 어느 수량이 과금 대상인지 현재 자료로 판단 불가입니다.')
            display = label+(' 소모품 수량과 같은 차이 '+str(len(items))+'개 주문' if suffix=='consumables' else ' 수량 차이 · '+sample['key'][0]+' · 청구 '+str(sample['claim']['rows'][0])+'행')
            item = check(prefix+'.'+suffix, display,
                         sum((r['reference']['quantity'] for r in items), Decimal(0)),
                         sum((r['claim']['quantity'] for r in items), Decimal(0)),
                         [group['claim_sheet']+'!G'+str(sample['claim']['rows'][0]), reference_name+'#'+group['reference_sheet']+'!H'+str(sample['reference']['rows'][0])], 'document_comparison', reason)
            item['unit'] = '원문 수량'
            checks.append(item)
        if inclusive:
            for i, scenario in enumerate(inclusive['duplicate_scenarios']):
                item = check(prefix+'.deduplicated.'+str(i), label+' 지정 반복 행 제외 후 비교 '+str(i+1),
                             scenario['reference_after_dedup'], scenario['claim_quantity'], refs,
                             'document_comparison', '소모품은 포함하고, 앞서 지정한 두 주문의 반복 행 제외 시나리오는 유지했습니다. 원문 수량과 제외 행은 보존합니다.')
                item['unit'] = '원문 수량'; checks.append(item)
        if counts['claim_only']:
            checks.append(check(prefix+'.unlinked', label+' 미연결 '+str(counts['claim_only'])+'개 항목', None, None, refs,
                                reason='동일 날짜·원형 번호로 연결되는 실적이 없어 해당 수량은 현재 자료로 판단 불가입니다. 누락 출고·과다청구로 확정하지 않습니다.'))
        if group['claim_rejected'] or group['reference_rejected']:
            checks.append(check(prefix+'.missing_fields', label+' 수량 등 비교값 부족 '+str(len(group['claim_rejected'])+len(group['reference_rejected']))+'행', None, None, refs,
                                reason='수량이 비어 있는 재출고·전월 작업비/용차비 별도 표기 등을 0으로 대체하지 않았습니다. 원문 사유는 전체 연결 근거에 보존합니다.'))
    if evidence.get('excluded_fee_components') is not None:
        checks.append(check('wms.policy.billable_total','소모품 제외 후 전체 청구금액',None,None,['청구내역','B2B출고'],
                            reason=evidence['billable_total_reason']))
    return checks


def adjusted_checks(group, reference_name):
    from invoice_tools import check
    adjusted = group['adjusted']
    prefix, label = 'wms.'+group['id'], group['label']
    refs = [group['claim_sheet'], reference_name+'#'+group['reference_sheet']]
    checks = []
    if adjusted['matched']:
        item = check(prefix+'.matched', label+' 소모품 제외 후 '+str(len(adjusted['matched']))+'개 항목 수량',
                     sum((r['reference_quantity'] for r in adjusted['matched']), Decimal(0)),
                     sum((r['claim_quantity'] for r in adjusted['matched']), Decimal(0)), refs, 'document_comparison',
                     '사용자 지정 소모품 제외 기준을 적용했습니다. 혼합 주문의 청구 구성 미확정 항목은 일치 건수에 포함하지 않았습니다.')
        item['unit'] = '원문 수량';checks.append(item)
    for i, r in enumerate(adjusted['different']):
        item = check(prefix+'.remaining.'+str(i), label+' 제외 후 수량 차이 · 청구 '+str(r['claim_rows'][0])+'행',r['reference_quantity'],r['claim_quantity'],refs,'document_comparison')
        item['unit']='원문 수량';checks.append(item)
    for i, r in enumerate(adjusted['duplicate_scenarios']):
        item = check(prefix+'.deduplicated.'+str(i), label+' 반복 행 제외 후 수량 비교 '+str(i+1),r['reference_after_dedup'],r['claim_quantity'],refs,'document_comparison',
                     '사용자가 지정한 반복 주문만 날짜·주문·품번·수량·LOT·유통기한 동일 행을 한 번씩 세었습니다. 원본과 제외 행 근거는 보존했습니다.')
        item['unit']='원문 수량';checks.append(item)
    for i,r in enumerate(adjusted['consumables']):
        if r['pure_consumable_order'] and r['original_difference']:
            item=check(prefix+'.consumable_only.'+str(i),'소모품 별도관리 수량 차이 · 물류비 대상 제외',r['reference_quantity'],r['claim_order_quantity'],
                       [group['claim_sheet']+'!G'+str(r['claim_rows'][0]['row']),reference_name+'#'+group['reference_sheet']+'!G'+str(r['reference_rows'][0]['row'])], 'document_comparison',
                       '검증자료 품명에는 소모품이 명시돼 있습니다. 청구에는 품명·소모품 구분이 없어 주문 연결로 분류했습니다. 원문 수량 차이는 별도 관리하고 물류비 대상에서 제외합니다.')
            item['unit']='원문 수량';item['included_in_billable_quantity']=False;checks.append(item)
    if adjusted['allocation_unknown']:
        checks.append(check(prefix+'.allocation_unknown',label+' 상품·소모품 혼합 '+str(len(adjusted['allocation_unknown']))+'개 주문 비용 분리',None,None,refs,
                     reason='소모품 수량은 별도 집계·제외했습니다. 청구에는 주문 합계만 있어 상품 수량과 소모품별 BOX/PCS 작업비·운임 배분은 현재 자료로 판단 불가입니다. 소모품 수량을 양쪽에서 임의 차감해 일치 처리하지 않았습니다.'))
    if adjusted['unlinked']:
        checks.append(check(prefix+'.unlinked',label+' 미연결 '+str(len(adjusted['unlinked']))+'개 항목',None,None,refs,
                     reason='동일 날짜·원형 번호로 연결되는 실적이 없어 해당 수량은 현재 자료로 판단 불가입니다.'))
    if group['claim_rejected'] or group['reference_rejected']:
        checks.append(check(prefix+'.missing_fields',label+' 수량 등 비교값 부족 '+str(len(group['claim_rejected'])+len(group['reference_rejected']))+'행',None,None,refs,
                     reason='수량이 빈 재출고·전월 작업비/용차비 별도 표기를 0으로 대체하지 않았습니다.'))
    return checks
