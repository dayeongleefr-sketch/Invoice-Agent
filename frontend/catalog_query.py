"""Read-only questions over the complete, current checklist and its visible details.

The model supplies a bounded query, never code. Counts and matching row IDs are
computed locally. Source identities stay local; this tool never changes decisions.
"""
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from pathlib import Path

from calculation_view import brief
from dashboard_view import display_rows, format_number


def read_rows(root, month):
    from review_catalog import original_rows, annotate, label_cost_rows
    from checklist_review import project
    from chat_commands import ChatCommands
    root = Path(root)
    policy_path = root/'ia/config/demo_company_supporting_evidence.json'
    policy = json.loads(policy_path.read_text('utf-8-sig')) if policy_path.is_file() else {}
    if month != policy.get('invoice_month'):
        # No implicit fallback to July when another month is requested.
        raise ValueError('이 월의 전체 확인 목록 조회는 아직 연결되지 않았습니다. 월별 검증 화면에서 해당 월 결과를 조회해 주세요.')
    rows = original_rows(root)
    projection = ChatCommands(root, session_id='catalog-query-read').disposition_projection()
    for row in rows:
        row.update(projection['rows'].get(row['_id'], {}))
    return label_cost_rows(root, [annotate(r) for r in project(root, rows)])


def version(rows):
    payload = [(r['_id'], r.get('_result_version'), r.get('_effective_status'),
                r.get('검사'), r.get('사유'), r.get('적용기준'), r.get('청구값'),
                r.get('비교값'), r.get('차이'), r.get('일자'), r.get('_cost_checks')) for r in rows]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def query_defaults(**kwargs):
    query = dict(terms=[], field='all', match='all', checks=[], statuses=[],
                 scope='all', same_reason_as_selected=False, date_from=None, date_to=None,
                 numeric_field=None, numeric_operator=None, numeric_value=None)
    query.update(kwargs)
    return query


def normalize(value):
    return re.sub(r'\s+', '', str(value or '')).casefold()


def search(rows, query, selected_ids=()):
    """Search every row, not the eight examples shared with the language model."""
    selected = set(map(str, selected_ids))
    if query['scope'] == 'selected' and not selected:
        raise ValueError('선택한 내역이 없습니다. 전체 목록을 조회하거나 대상 내역을 선택해 주세요.')
    for key in ('date_from', 'date_to'):
        if query[key]:
            date.fromisoformat(query[key])
    if query['date_from'] and query['date_to'] and query['date_from'] > query['date_to']:
        raise ValueError('조회 시작일이 종료일보다 늦습니다.')
    if query['numeric_field']:
        if query['numeric_operator'] not in ('eq', 'ne', 'gt', 'gte', 'lt', 'lte'):
            raise ValueError('숫자 조회의 비교 연산이 필요합니다.')
        try:
            threshold = Decimal(query['numeric_value'])
        except (InvalidOperation, TypeError):
            raise ValueError('숫자 조회 조건을 해석할 수 없습니다.')
        if not threshold.is_finite():
            raise ValueError('유한한 숫자 조회 조건이 필요합니다.')
    elif query['numeric_operator'] or query['numeric_value'] is not None:
        raise ValueError('청구값·비교값·차이 중 숫자를 비교할 대상을 지정해 주세요.')
    records = []
    for row in display_rows(rows):
        parts = brief(row)
        reason = ' '.join(v for k, v in parts if k in ('판단 이유', '판정 이유', '미검증 이유')) or row.get('_display_reason', '')
        fields = dict(reason=reason,
                      method=' '.join(v for k, v in parts if k in ('비교 방법',)),
                      criterion=row.get('_display_basis', ''))
        fields['all'] = ' '.join([row.get('검사', ''), row.get('_display_target', ''),
                                  row.get('사유', ''), *fields.values(), *(v for _, v in parts)])
        records.append((row, fields))
    same_reasons = {normalize(f['reason']) for r, f in records if r['_id'] in selected and f['reason']}
    if query['same_reason_as_selected'] and not same_reasons:
        raise ValueError('선택한 내역의 판단 이유를 찾지 못했습니다. 찾을 사유를 알려 주세요.')
    matched = []
    reasons = Counter(); groups = Counter(); statuses = Counter()
    terms = [normalize(t) for t in query['terms'] if normalize(t)]
    for row, fields in records:
        if query['scope'] == 'selected' and row['_id'] not in selected:
            continue
        if query['checks'] and not any(normalize(t) in normalize(row['검사']) for t in query['checks']):
            continue
        status = row.get('_effective_status') or row.get('결과')
        if query['statuses'] and status not in query['statuses']:
            continue
        day = str(row.get('일자', ''))
        if query['date_from'] or query['date_to']:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
                continue
            if query['date_from'] and day < query['date_from'] or query['date_to'] and day > query['date_to']:
                continue
        if query['numeric_field']:
            try:
                number = Decimal(str(row.get(query['numeric_field'], '')).replace(',', ''))
            except InvalidOperation:
                continue
            if not number.is_finite():
                continue
            passes = {'eq':number == threshold, 'ne':number != threshold,
                      'gt':number > threshold, 'gte':number >= threshold,
                      'lt':number < threshold, 'lte':number <= threshold}
            if not passes.get(query['numeric_operator'], False):
                continue
        if query['same_reason_as_selected'] and normalize(fields['reason']) not in same_reasons:
            continue
        text = normalize(fields[query['field']])
        if terms and not (all(t in text for t in terms) if query['match'] == 'all' else any(t in text for t in terms)):
            continue
        matched.append(row['_id']); groups[row['검사']] += 1; statuses[status] += 1
        reasons[fields['reason'] or '사유 기재 없음'] += 1
    return dict(total_rows=len(rows), matched_count=len(matched), matched_ids=matched,
                counts_by_check=dict(groups), counts_by_status=dict(statuses),
                reasons=[dict(reason=key, count=count) for key, count in reasons.most_common(8)],
                reason_group_count=len(reasons), query=query, version=version(rows),
                count_unit='확인 목록의 검증 내역 수', verification_executed=False)


def answer(result, month):
    count = result['matched_count']
    scope = '선택 내역' if result['query']['scope'] == 'selected' else '전체 확인 목록'
    lines = [f"{month} 저장 결과의 **{scope}에서 {count:,}건**을 찾았습니다."]
    query = result['query']
    terms = query['terms']
    if terms:
        joiner = ' + ' if query['match'] == 'all' else ' 또는 '
        lines.append('조회 조건: ' + joiner.join(terms))
    if query['same_reason_as_selected']:
        lines.append('조회 조건: 선택한 내역과 같은 판단 이유')
    if query['checks'] or query['statuses']:
        lines.append('범위: ' + ' · '.join(query['checks'] + query['statuses']))
    if query['numeric_field']:
        operator = {'eq':'=', 'ne':'≠', 'gt':'>', 'gte':'≥', 'lt':'<', 'lte':'≤'}[query['numeric_operator']]
        lines.append(f"숫자 조건: {query['numeric_field']} {operator} {format_number(query['numeric_value'])}")
    if query['date_from'] or query['date_to']:
        lines.append(f"일자: {query['date_from'] or '전체 시작'} ~ {query['date_to'] or '전체 종료'}")
    if count:
        lines.extend(['', '| 검증 항목 | 해당 내역 |', '|---|---:|'])
        lines.extend(f'| {key} | {number:,}건 |' for key, number in result['counts_by_check'].items())
        lines.append('\n상태: ' + ' · '.join(f'{k} {n:,}건' for k, n in result['counts_by_status'].items()))
        if len(result['reasons']) == 1:
            lines.append('\n판단 이유: ' + result['reasons'][0]['reason'])
        elif result['reasons']:
            lines.extend(['', '| 기록된 사유 | 해당 내역 |', '|---|---:|'])
            lines.extend(f"| {r['reason'].replace('|', '·').replace(chr(10), ' ')} | {r['count']:,}건 |" for r in result['reasons'])
            if result['reason_group_count'] > len(result['reasons']):
                lines.append('자주 기록된 사유 8개만 표시했습니다. 전체 대상은 조회 버튼에서 볼 수 있습니다.')
        if any('추정' in r['reason'] or '후보' in r['reason'] for r in result['reasons']):
            lines.append('\n‘추정·후보’로 기록된 사유는 확정 원인이 아닙니다.')
    else:
        lines.append('위 조건에 맞는 기록이 없습니다. 조건을 바꾸면 전체 목록에서 다시 검색할 수 있습니다.')
    return '\n'.join(lines)
