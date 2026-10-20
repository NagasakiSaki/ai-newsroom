"""Local information library and exports; never calls a model or fetches a page."""
import csv
import io
import json
from .collect import matches_keywords

FIELDS = ('id', 'title', 'url', 'source_id', 'source', 'published_at', 'collected_at', 'date_basis',
          'text', 'excerpt', 'source_report', 'custom_source')


def query(store, q='', source_id='', limit=50, offset=0):
    words = q.split()
    items = []
    for item in store.all('stories'):
        if source_id and item.get('source_id') != source_id:
            continue
        text = '\n'.join(str(item.get(k) or '') for k in ('title', 'text', 'excerpt', 'feed_text'))
        if not all(matches_keywords(text, [word]) for word in words):
            continue
        record = {k: item.get(k) for k in FIELDS}
        record['has_body'] = bool(item.get('text'))
        items.append(record)
    items.sort(key=lambda row: row.get('collected_at') or '', reverse=True)
    return {'total': len(items), 'offset': offset, 'limit': limit, 'items': items[offset:offset + limit]}


def detail(store, ident):
    item = store.get('stories', ident)
    return {k: item.get(k) for k in FIELDS}


def export(store, fmt, q='', source_id=''):
    # Bound exports to avoid loading unbounded output in one HTTP response.
    result = query(store, q, source_id, limit=5000)
    if fmt == 'json':
        return json.dumps({**result, 'truncated': result['total'] > len(result['items'])}, ensure_ascii=False, indent=2)
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    columns = ('title', 'url', 'source', 'published_at', 'collected_at', 'date_basis', 'text')
    writer.writerow(columns)
    for row in result['items']:
        values = []
        for key in columns:
            value = str(row.get(key) or '')
            if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r')):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return '\ufeff' + output.getvalue()
