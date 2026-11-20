"""Presentation-only coverage of every nonzero billed cost line.

Connection means a performed comparison to WMS or invoice detail exists. It
does not mean the whole billed amount, work occurrence, or rate was approved.
Arithmetic within a summary row and manual completion do not create a link.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import re


METHODS = {'wms': 'WMS 비교', 'detail': '청구 상세 비교',
           'evidence': '추가 증빙 비교', 'provisional': '잠정 확정 · 증빙 미검증',
           'unlinked': '자료 대조 미완료'}
LINKED = {'wms', 'detail', 'evidence'}


def occurrence_pending_items(full, cost, supporting):
    """Previously accepted costs without a performed supporting comparison.

    Read-only display policy: keep historical decisions and numeric verdicts.
    A performed detail/WMS comparison is retained as such, including differences.
    """
    items = []
    for raw in full.get('claim_items', []):
        try:
            amount = Decimal(str(raw.get('amount')))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if not amount.is_finite() or not amount:
            continue
        items.append(dict(raw, amount=amount, check_ids=[]))
    section = {'label': '', 'amount': sum((i['amount'] for i in items), Decimal(0)), 'items': items}
    return {i['row']: i for i in build_coverage([section], full, cost, supporting)['items']
            if i['method'] == 'provisional'}


def build_coverage(sections, full, cost, supporting=None):
    from cost_assessment import assess
    assessments = assess(full, cost, supporting)
    comparisons = defaultdict(list)
    # Use actual performed checks, not older claim-item capability flags.
    checks = [(c, 'detail') for c in full.get('checks', [])
              if c.get('id', '').startswith(('detail.', 'b2c.summary.')) and c.get('source') == 'claim']
    for c in cost.get('checks', []):
        ident = c.get('id', '')
        if ident.startswith(('cost.b2c_wms_quantity.', 'cost.b2c_wms_amount.')):
            checks.append((c, 'wms'))
        elif ident.startswith(('cost.detail.', 'cost.expanded_detail.',
                               'cost.expanded_amount.', 'cost.b2c_parcel_summary.')):
            checks.append((c, 'detail'))
    for c, method in checks:
        if not c.get('check_performed') or c.get('status') not in ('match', 'difference'):
            continue
        evidence = c.get('evidence', [])
        # A summary-to-summary formula is arithmetic, not detail linkage.
        if method == 'detail' and not any('!' in e and not e.startswith('청구내역!')
                                           and '.json' not in e for e in evidence):
            continue
        targets = {int(m[1]) for e in evidence
                   for m in re.finditer(r'청구내역![$A-Z]*(\d+)', e)}
        if len(targets) != 1:
            continue
        comparisons[next(iter(targets))].append({
            'id': c['id'], 'method': method, 'status': c['status'],
            'evidence': list(evidence), 'basis': c.get('basis') or c.get('reason') or '',
        })

    accepted = set()
    if supporting and supporting.get('active'):
        for entry in supporting.get('rows', []):
            n = entry.get('invoice_row')
            if entry.get('axis') not in ('work', 'actual_cost'):
                continue
            if entry.get('status') == 'provisional':
                accepted.add(n)
            elif (entry.get('check_performed') and entry.get('status') in ('match', 'difference')
                  and entry.get('evidence')):
                comparisons[n].append({'id': f"evidence.{n}.{entry['axis']}",
                                       'method': 'evidence', 'status': entry['status'],
                                       'evidence': entry['evidence'], 'basis': entry.get('reason', '')})

    out, ledger = [], []
    totals = {key: Decimal(0) for key in METHODS}
    counts = {key: 0 for key in METHODS}
    comparison_amounts = {key: Decimal(0) for key in ('match', 'difference', 'provisional', 'unverified')}
    comparison_counts = {key: 0 for key in comparison_amounts}
    for section in sections:
        amounts = {key: Decimal(0) for key in METHODS}
        scounts = {key: 0 for key in METHODS}
        members = []
        for item in section['items']:
            n = item['row']
            found = comparisons[n]
            method = next((key for key in ('evidence', 'wms', 'detail')
                           if any(c['method'] == key for c in found)), None)
            method = method or ('provisional' if n in accepted else 'unlinked')
            result_key = ('difference' if any(c['status'] == 'difference' for c in found)
                          else 'match' if found else
                          'provisional' if method == 'provisional' else 'unverified')
            assessment = assessments.get(n)
            if assessment:
                result_key = {'정상':'match','불일치':'difference','잠정 확정':'provisional','미검증':'unverified'}[assessment['결과']]
            # Count each original billed line once; multiple axes are not money.
            amounts[method] += item['amount']; scounts[method] += 1
            totals[method] += item['amount']; counts[method] += 1
            comparison_amounts[result_key] += item['amount']
            comparison_counts[result_key] += 1
            row = dict(item)
            row.update(section=section['label'], method=method, connection=METHODS[method],
                       verdict=result_key, assessment=assessment,
                       comparisons=found,
                       comparison_result={'difference': '비교값 차이', 'match': '비교값 일치',
                                          'provisional': '실제 발생 증빙 없음', 'unverified': '비교 미수행'}[result_key])
            members.append(row); ledger.append(row)
        linked = sum((amounts[k] for k in LINKED), Decimal(0))
        no_link = amounts['provisional'] + amounts['unlinked']
        assert linked + no_link == section['amount']
        out.append({'label': section['label'], 'amount': section['amount'],
                    'linked_amount': linked, 'unlinked_amount': no_link,
                    'amounts': amounts, 'counts': scounts, 'items': members,
                    'difference_count': sum(i['comparison_result'] == '비교값 차이' for i in members),
                    'verdict_counts': {key: sum(i['verdict']==key for i in members) for key in comparison_counts},
                    'verdict_amounts': {key: sum((i['amount'] for i in members if i['verdict']==key),Decimal(0)) for key in comparison_counts},
                    'ids': list(dict.fromkeys(cid for i in members for cid in i['check_ids']))})
    return {'sections': out, 'items': ledger, 'amounts': totals, 'counts': counts,
            'comparison_amounts': comparison_amounts, 'comparison_counts': comparison_counts,
            'linked_amount': sum((totals[k] for k in LINKED), Decimal(0)),
            'unlinked_amount': totals['provisional'] + totals['unlinked'],
            'linked_count': sum(counts[k] for k in LINKED),
            'unlinked_count': counts['provisional'] + counts['unlinked']}
