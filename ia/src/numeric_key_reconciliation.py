"""Reconcile expanded WMS identifiers and optionally recover digits from evidence.

Original files are read only. Optional internal data is explicitly requested,
source-bound, and never required by the raw-source comparison.
"""
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
import csv
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from identifier_tools import expand_identifier, scientific_info, recover_identifier
from invoice_tools import digest
from wms_tools import day
from xlsb_scan import scan_rows
from portable_workbook import open_workbook

FIELDS = {'order': '주문번호(연동)', 'tracking': '송장번호', 'erp': '주문번호(ERP)',
          'po': 'Po No.', 'management': '관리번호', 'shipment': '출고번호'}
REFERENCE_COLS = {'order': 'L', 'tracking': 'Y', 'erp': 'O', 'po': 'N', 'management': 'M'}
REFERENCE_PATH = 'data/demo_company/source/legacy_example_54a741f39e4a.xlsb'


def new_group():
    return {'values': defaultdict(set), 'rows': [], 'sku': defaultdict(Decimal),
            'units': set(), 'customers': set(), 'types': set()}


def equal_shipment(wms, reference):
    return bool(wms and reference and wms['sku'] == reference['sku']
                and wms['units'] == reference['units'] == {'EA'}
                and wms['customers'] == reference['customers']
                and wms['types'] == reference['types'])


def run(root, parcel, baseline_orders, period, include_reference=False):
    from actual_claim_wms_b2c import normalize_date
    root = Path(root).resolve()
    base = root / 'data/demo_company/derived'
    config = json.loads((root/'ia/config/demo_company_direct_wms.json').read_text('utf-8'))
    decision_path = root/'ia/config/matching_review_decisions.json'
    decisions = json.loads(decision_path.read_text('utf-8'))
    sources = {k: config['sources'][k] for k in ('claim', 'outbound_july', 'outbound_h1')}
    for source in sources.values():
        path = (root/source['path']).resolve()
        if not path.is_relative_to(root/'data/demo_company/source'):
            raise ValueError('source_outside_active_scope')
        if digest(path).lower() != source['sha256'].lower():
            raise ValueError('numeric_restoration_source_changed')
    source_valid = decisions.get('period') == list(period) and all(
        decisions.get('source_hashes', {}).get(k, '').lower() == v['sha256'].lower()
        for k, v in sources.items())
    reference_enabled = include_reference and source_valid and decisions.get('U20', {}).get('state') == 'approved'
    if include_reference and not reference_enabled:
        raise ValueError('reference_recovery_not_authorized_for_sources')

    groups = {}; cells = []; all_counts = Counter(); scoped_counts = Counter(); scanned = 0
    token_groups = defaultdict(set); exact_index = defaultdict(set); expanded_index = defaultdict(set)
    with (root/sources['outbound_july']['path']).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not (set(FIELDS.values()) | {'출고일', '회사구분', '출고창고', '품번', '수량', '단위', '거래처', '구분'}).issubset(reader.fieldnames):
            raise ValueError('numeric_restoration_headers_changed')
        for n, row in enumerate(reader, 2):
            scanned += 1
            if (row['회사구분'].strip(), row['출고창고'].strip()) != ('90', 'DM001'):
                continue
            date = normalize_date(row['출고일']); in_period = period[0] <= date <= period[1]
            shipment = row['출고번호'].strip()
            gid = (date, shipment or 'missing-shipment-row:'+str(n))
            g = groups.setdefault(gid, new_group()) if in_period else None
            if g is not None:
                g['rows'].append(n); g['sku'][row['품번'].strip()] += Decimal(row['수량'].replace(',', ''))
                g['units'].add(row['단위'].strip()); g['customers'].add(row['거래처'].strip()); g['types'].add(row['구분'].strip())
            for field, column in FIELDS.items():
                raw = row[column].strip(); info = scientific_info(raw)
                if g is not None and raw:
                    g['values'][field].add(raw)
                    exact_index[field, raw].add(gid)
                    expanded_index[field, expand_identifier(raw)].add(gid)
                if info:
                    all_counts[field] += 1
                    cells.append({'csv_row': n, 'field': field, 'date': date, 'shipment': shipment,
                                  'in_period': in_period, **info})
                    if in_period:
                        scoped_counts[field] += 1; token_groups[field, raw].add(gid)

    affected = {(x['date'], x['shipment']) for x in cells if x['in_period'] and x['shipment']}
    refs = {}; reference_reason = 'not_requested'
    if reference_enabled:
        reference_path = root/REFERENCE_PATH
        if reference_path.is_file():
            reference_hash = digest(reference_path)
            if reference_hash.lower() != decisions.get('optional_reference_sha256', '').lower():
                raise ValueError('optional_reference_changed')
            sources['optional_reference'] = {'path': REFERENCE_PATH, 'sha256': reference_hash}
            reference_reason = 'enabled_by_U20'
            with open_workbook(str(reference_path)) as book:
                for n, row in scan_rows(book, '총 LAW'):
                    if n == 1:
                        for col, header in {'B':'출고일', 'C':'출고번호', 'L':'주문번호(연동)', 'P':'품번', 'V':'단위', 'W':'수량', 'Y':'송장번호'}.items():
                            if row.get(col, {}).get('value') != header:
                                raise ValueError('reference_header_changed')
                        continue
                    get = lambda col: row.get(col, {}).get('value')
                    if get('B') is None or get('C') is None:
                        continue
                    key = (day({'value':get('B')}), expand_identifier(get('C')))
                    if key not in affected:
                        continue
                    g = refs.setdefault(key, new_group()); g['rows'].append(n)
                    g['sku'][expand_identifier(get('P'))] += Decimal(str(get('W')))
                    g['units'].add(expand_identifier(get('V')))
                    g['customers'].add(expand_identifier(get('F'))); g['types'].add(expand_identifier(get('H')))
                    for field, col in REFERENCE_COLS.items():
                        value = get(col)
                        raw = str(value).strip() if isinstance(value, str) else expand_identifier(value)
                        if raw: g['values'][field].add(raw)
        else:
            # Optional material cannot block or masquerade as a raw-only run.
            reference_reason = 'optional_file_absent'

    recovery = {}; recovery_counts = Counter(); raw_recovered = defaultdict(set); ref_recovered = defaultdict(set)
    for cell in cells:
        gid = (cell['date'], cell['shipment']); field = cell['field']; raw = cell['raw']
        key = (gid, field, raw)
        if key in recovery: continue
        r = {'restored': None, 'basis': 'expanded_only', 'reason': 'outside_comparison_period' if not cell['in_period'] else 'full_digits_unavailable', 'reference_rows': []}
        if cell['in_period'] and gid in groups and cell['shipment']:
            g = groups[gid]
            full_values = {v for v in g['values'][field] if scientific_info(v) is None}
            restored, reason = recover_identifier(raw, full_values, True)
            if restored:
                r.update(restored=restored, basis='same_raw_wms_shipment', reason=reason)
                raw_recovered[field, restored].add(gid)
            elif reference_enabled and gid in refs:
                ref = refs[gid]
                restored, reason = recover_identifier(raw, ref['values'][field], equal_shipment(g, ref))
                r['reason'] = reason
                if restored:
                    r.update(restored=restored, basis='optional_reference_same_shipment', reference_rows=ref['rows'])
                    ref_recovered[field, restored].add(gid)
            else:
                r['reason'] = reason
        recovery[key] = r
        if cell['in_period']: recovery_counts[r['basis']] += 1

    exact_new = set(); raw_new = set(); ref_new = set(); ambiguous = set(); claim_matches = []
    for claim in parcel:
        hits = {'expanded': set(), 'raw_recovered': set(), 'reference_recovered': set()}
        methods = defaultdict(set)
        for field in ('order', 'tracking'):
            value = expand_identifier(claim[field])
            # Report only a conversion-induced match, not the original exact key.
            for gid in expanded_index.get((field, value), set()) - exact_index.get((field, claim[field]), set()):
                hits['expanded'].add(gid); methods['expanded'].add(field)
            for label, index in [('raw_recovered', raw_recovered), ('reference_recovered', ref_recovered)]:
                ids = index.get((field, value), set())
                if ids: hits[label].update(ids); methods[label].add(field)
        if not any(hits.values()): continue
        is_new = claim['order'] not in baseline_orders
        for label, orders in [('expanded',exact_new), ('raw_recovered',raw_new), ('reference_recovered',ref_new)]:
            if len(hits[label]) == 1 and is_new: orders.add(claim['order'])
            elif len(hits[label]) > 1: ambiguous.add(claim['order'])
        claim_matches.append({'claim_row':claim['row'], 'claim_order':claim['order'], 'claim_tracking':claim['tracking'],
            'already_directly_matched':not is_new, 'paths': {label:[{'date':k[0],'shipment':k[1], 'wms_csv_rows':groups[k]['rows']} for k in sorted(ids)] for label,ids in hits.items() if ids},
            'matched_fields':{k:sorted(v) for k,v in methods.items()}, 'invoice_EA_validated':False, 'freight_validated':False})

    out_csv = base/'WMS_번호복원_근거.csv'
    with out_csv.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['WMS CSV행','출고일','출고번호','필드','원문','일반숫자변환','근거에서복원한번호','복원근거','내부자료행','비교기간포함','상태'])
        for c in cells:
            r = recovery[( (c['date'], c['shipment']), c['field'], c['raw'])]
            writer.writerow([c['csv_row'],c['date'],c['shipment'],FIELDS[c['field']],c['raw'],c['expanded'],r['restored'],r['basis'],','.join(map(str,r['reference_rows'])),c['in_period'],r['reason']])
    examples=[]
    for c in cells:
        r=recovery[((c['date'],c['shipment']),c['field'],c['raw'])]
        if c['in_period'] and c['field']=='tracking' and r['restored'] and len(examples)<3:
            examples.append({**c,**r,'other_shipments_with_same_raw':len(token_groups[c['field'],c['raw']])})
    result={'schema_version':'1.0', 'period':list(period), 'sources':sources,
        'code_hashes':{p.name:digest(p) for p in (Path(__file__),Path(__file__).with_name('identifier_tools.py'))},
        'decision_sha256':digest(decision_path), 'calculation_executed':True,'llm_used':False,
        'scanned_csv_rows':scanned, 'scientific_cells_all_dates':dict(all_counts),'scientific_cells_in_period':dict(scoped_counts),
        'scientific_tokens':[{'field':field,'raw':raw,'expanded':expand_identifier(raw),'distinct_shipments':len(ids)} for (field,raw),ids in sorted(token_groups.items())],
        'recovery_by_shipment_field':dict(recovery_counts),
        'reference_usage':{'requested':include_reference, 'used':'optional_reference' in sources,'reason':reference_reason,'required_for_operation':False},
        'comparison':{'new_claim_orders_exact_expansion':len(exact_new),'new_claim_orders_raw_sibling_recovery':len(raw_new-exact_new),
                      'claim_orders_reference_recovered':len(ref_new-exact_new-raw_new),'ambiguous_claim_orders':len(ambiguous),
                      'reference_recovered_orders':sorted(ref_new),'raw_only_additional_orders':sorted(exact_new|raw_new)},
        'examples':examples,'claim_matches':claim_matches,'evidence_csv':{'path':out_csv.relative_to(root).as_posix(),'sha256':digest(out_csv)},
        'full_invoice_validated':False,'invoice_EA_validated':False,'freight_validated':False}
    for source in sources.values():
        if digest(root/source['path']).lower()!=source['sha256'].lower():raise ValueError('input_changed_during_recovery')
    output=base/'numeric_key_reconciliation.json'
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result
