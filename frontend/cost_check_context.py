"""Carry invoice-item identity and display order into the checklist (UI only)."""


def make_context(section, ordered_rows):
    by_row = {item['row']: item for item in section['items']}
    targets = {}
    labels = []
    for rank, row in enumerate(ordered_rows):
        item = by_row[row]
        label = ' · '.join(str(v) for v in (item.get('category'), item['label'], item.get('unit')) if v)
        labels.append(label)
        for check_id in item['check_ids']:
            # A shared check is displayed once, attached to its first invoice item.
            targets.setdefault(check_id, {'label': label, 'rank': rank, 'row': row})
    return {'label': section['label'], 'items': labels, 'targets': targets}


def contextual_rows(rows, context, include_normal=False, status='전체'):
    if not context:
        return rows
    from review_catalog import filter_values
    statuses=filter_values(status)
    targets = context['targets']
    selected = []
    for row in rows:
        target = targets.get(row['_id'])
        if target is None:
            continue
        effective = row.get('_effective_status', row.get('결과'))
        if effective == '정상' and not include_normal and not any(s in statuses for s in ('정상', '직접 완료')):
            continue
        selected.append(dict(row, _cost_context_label=target['label'], _cost_context_rank=target['rank']))
    return sorted(selected, key=lambda r: (r['_cost_context_rank'],
        {'불일치': 0, '미검증': 1, '잠정 확정': 2, '정상': 3}.get(r.get('_effective_status', r.get('결과')), 4)))
