"""Shared, deterministic monetary summary for tools, chat and reports.

Only aggregate fields are returned by observation(); never identifiers or cells.
The original numeric verdict and provisional adoption remain distinct.
"""
from decimal import Decimal
import re

from financial_overview import load_overview

START = '<!-- financial-summary:start -->'
END = '<!-- financial-summary:end -->'
STATUS = {'match': '일치', 'difference': '불일치',
          'provisional': '잠정 확정', 'unverified': '미검증'}


def observation(data):
    coverage = data['coverage']
    total = data['totals']['net']
    return {
        'invoice_month': data['month'],
        'period': {k:v for k,v in data['period'].items() if k in ('start','end','state')},
        'currency': 'KRW',
        'billed_amount': str(total), 'vat_included_amount': str(data['totals'].get('payable', '')),
        'review_amount': str(data['review_amount']),
        'review_percent': str(data['ratio']) if data['ratio'] is not None else None,
        'cost_line_count': data['claim_item_count'],
        'count_unit': '청구내역 시트에서 금액이 있는 비용 줄',
        'comparison_basis': 'WMS·청구 상세·추가 증빙 대사 결과. 요약행 곱셈만으로 일치 처리하지 않으며 실비·별도 협의 금액은 지출·합의 증빙을 확인함. 지급 승인과는 구분',
        'provisional_policy': '증빙 확인 전 임시 인정. 확인 필요 금액에서 제외하며 정상 검증·지급 승인과 구분',
        'review_basis': '불일치 + 미검증의 관련 청구액. 잠정 확정·담당자 처리완료는 제외하며 요약과 상세 중복은 한 번만 합산',
        'review_breakdown': [{'status': p['label'], 'billed_amount': str(p['amount'])}
                             for p in data['money_breakdown']],
        'statuses': [{
            'status': label, 'cost_lines': coverage['comparison_counts'][key],
            'billed_amount': str(coverage['comparison_amounts'][key]),
            'percent': str(coverage['comparison_amounts'][key] / total * 100) if total > 0 else None,
        } for key, label in STATUS.items()],
        'cost_sections': [{
            'label': s['label'], 'billed_amount': str(s['amount']),
            'compared_amount': str(s['verdict_amounts']['match'] + s['verdict_amounts']['difference']),
            'provisional_amount': str(s['verdict_amounts']['provisional']),
            'unverified_amount': str(s['verdict_amounts']['unverified']),
            'difference_lines': s['difference_count'],
        } for s in coverage['sections']],
        'review_sections': [{'label': s['label'], 'review_amount': str(s['amount']),
                             'breakdown': {k: str(v) for k,v in s['breakdown'].items()}}
                            for s in data['review_sections']],
        'amount_approved': False,
    }


def markdown(data):
    return markdown_observation(observation(data))


def markdown_observation(o):
    def won(value):
        amount = Decimal(value)
        remainder, parts = abs(amount), []
        for divisor, unit in ((Decimal(10**12), '조'), (Decimal(10**8), '억'), (Decimal(10**4), '만')):
            group = int(remainder // divisor)
            if group:
                parts.append(f'{group:,}{unit}')
                remainder -= group * divisor
        if remainder or not parts:
            text = f'{remainder:,f}'
            parts.append(text.rstrip('0').rstrip('.') if '.' in text else text)
        return ('-' if amount < 0 else '') + ' '.join(parts) + '원'
    compact = lambda v: f'{Decimal(v) / Decimal(100000000):.2f}억원'
    lines = [START, '### 물류비 대사 요약', '',
             '| 전체 청구 물류비 | 확인 필요 물류비 | 확인 필요 비중 |',
             '|---:|---:|---:|',
             f"| {compact(o['billed_amount'])} | {compact(o['review_amount'])} | {Decimal(o['review_percent']):.2f}% |" if o['review_percent'] is not None else
             f"| {compact(o['billed_amount'])} | {compact(o['review_amount'])} | 산정 불가 |", '',
             f"청구 비용 {o['cost_line_count']:,}항목(금액이 있는 비용 한 줄) · 차감 후 공급가액 · 부가세 제외", '',
             '| 청구 요약 항목의 대사 결과 | 청구 비용 줄 수 | 해당 청구액 |', '|---|---:|---:|']
    lines += [f"| {s['status']} | {s['cost_lines']:,} | {won(s['billed_amount'])} |"
              for s in o['statuses'] if s['cost_lines']]
    if o.get('review_breakdown'):
        lines += ['', '확인 필요 = ' + ' + '.join(
            f"{p['status']} {won(p['billed_amount'])}" for p in o['review_breakdown']) + ' (요약·상세 중복 제외)', '']
    if o.get('review_sections'):
        billed = {s['label']: s['billed_amount'] for s in o['cost_sections']}
        lines += ['', f"확인 필요 합계 **{won(o['review_amount'])}**", '',
                  '| 비용 구분 | 청구액 | 확인 필요 금액 |', '|---|---:|---:|']
        for s in o['review_sections']:
            values = [billed[s['label']], s['review_amount']]
            lines.append('| ' + s['label'].replace('|', '／') + ' | ' + ' | '.join(won(v) for v in values) + ' |')
    lines += ['', '일치는 WMS·청구 상세·증빙과 대조한 결과입니다. 실비·협의금액의 증빙 미확인은 잠정 확정으로 구분합니다.',
              '잠정 확정은 증빙 확인 전 청구액을 임시 인정한 상태이며, 확인 필요 금액에서 제외합니다.',
              '불일치 금액은 해당 비용 줄의 청구액이며 차액은 상세 검사에서 확인합니다.', END]
    return '\n'.join(lines)


def replace_summary(text, data):
    block = markdown(data)
    if START in text and END in text:
        return re.sub(re.escape(START) + r'.*?' + re.escape(END), lambda _: block, text, count=1, flags=re.S)
    heading, _, rest = text.partition('\n')
    return heading + '\n\n' + block + '\n\n' + rest.lstrip('\n')


def saved_observation(root, rows):
    return observation(load_overview(root, rows))
