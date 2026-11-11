"""Exact-reference reconciliation and a versioned exception workflow."""
import argparse
import csv
import hashlib
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (Column, Integer, MetaData, String, Table, Text, create_engine,
                        insert, select, text, update)

ROOT = Path(__file__).resolve().parent
META = MetaData()
RUNS = Table('rec_runs', META, Column('run_id', String(64), primary_key=True),
             Column('created_at', String(40)), Column('summary', Text))
ENTRIES = Table('rec_entries', META, Column('run_id', String(64), primary_key=True),
                Column('side', String(15), primary_key=True), Column('row_id', Integer, primary_key=True),
                Column('reference', String(80)), Column('amount_minor', Integer), Column('currency', String(3)))
EXCEPTIONS = Table('rec_exceptions', META, Column('exception_id', String(64), primary_key=True),
                   Column('run_id', String(64)), Column('reference', String(80)), Column('category', String(40)),
                   Column('ledger_count', Integer), Column('settlement_count', Integer),
                   Column('ledger_amount', Integer), Column('settlement_amount', Integer),
                   Column('ledger_currency', String(3)), Column('settlement_currency', String(3)),
                   Column('status', String(20)), Column('version', Integer))
EVENTS = Table('rec_events', META, Column('event_id', String(32), primary_key=True),
               Column('exception_id', String(64)), Column('changed_at', String(40)),
               Column('actor', String(100)), Column('old_status', String(20)),
               Column('new_status', String(20)), Column('note', Text), Column('version', Integer))

MATCH_SQL = """
WITH l AS (
  SELECT reference, COUNT(*) AS n, MIN(amount_minor) AS amount, MIN(currency) AS currency
  FROM rec_entries WHERE run_id=:run AND side='ledger' GROUP BY reference
), s AS (
  SELECT reference, COUNT(*) AS n, MIN(amount_minor) AS amount, MIN(currency) AS currency
  FROM rec_entries WHERE run_id=:run AND side='settlement' GROUP BY reference
), refs AS (SELECT reference FROM l UNION SELECT reference FROM s)
SELECT refs.reference, COALESCE(l.n,0) AS ledger_count, COALESCE(s.n,0) AS settlement_count,
       l.amount AS ledger_amount, s.amount AS settlement_amount,
       l.currency AS ledger_currency, s.currency AS settlement_currency
FROM refs LEFT JOIN l ON refs.reference=l.reference LEFT JOIN s ON refs.reference=s.reference
ORDER BY refs.reference
"""


def database(url=None):
    url = url or os.getenv('DATABASE_URL') or 'sqlite:///' + (ROOT / 'reconciliation.db').as_posix()
    engine = create_engine(url, connect_args={'connect_timeout': 5} if url.startswith('postgresql') else {})
    META.create_all(engine)
    return engine


def parse_extract(payload):
    if len(payload) > 10_000_000:
        raise ValueError('Each extract must be under 10 MB.')
    reader = csv.DictReader(io.StringIO(payload.decode('utf-8-sig'), newline=''))
    if reader.fieldnames != ['reference', 'amount_minor', 'currency']:
        raise ValueError('Expected columns: reference,amount_minor,currency')
    rows = []
    for line, raw in enumerate(reader, 2):
        if None in raw or any(v is None for v in raw.values()):
            raise ValueError(f'Malformed row {line}.')
        row = {k: v.strip() for k, v in raw.items()}
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', row['reference']):
            raise ValueError(f'Invalid reference at row {line}.')
        if not re.fullmatch(r'\d{1,9}', row['amount_minor']):
            raise ValueError(f'Amount must be a nonnegative integer in minor units at row {line}.')
        if row['currency'] not in {'INR', 'GBP', 'SGD'}:
            raise ValueError(f'Unsupported currency at row {line}.')
        row['amount_minor'] = int(row['amount_minor'])
        rows.append(row)
        if len(rows) > 100_000:
            raise ValueError('Extract exceeds the 100,000-row demo limit.')
    # Empty sides are legitimate: every reference on the other side becomes an exception.
    return rows


def classify(row):
    if row['ledger_count'] > 1 or row['settlement_count'] > 1:
        return 'duplicate_reference'
    if row['ledger_count'] == 0:
        return 'missing_ledger'
    if row['settlement_count'] == 0:
        return 'missing_settlement'
    if row['ledger_currency'] != row['settlement_currency']:
        return 'currency_mismatch'
    if row['ledger_amount'] != row['settlement_amount']:
        return 'amount_mismatch'
    return None


def reconcile(engine, ledger_payload, settlement_payload):
    extracts = {'ledger': parse_extract(ledger_payload), 'settlement': parse_extract(settlement_payload)}
    hashes = [hashlib.sha256(p).hexdigest() for p in [ledger_payload, settlement_payload]]
    run_id = hashlib.sha256((':'.join(hashes) + ':v1').encode()).hexdigest()
    with engine.begin() as conn:
        existing = conn.execute(select(RUNS.c.summary).where(RUNS.c.run_id == run_id)).scalar_one_or_none()
        if existing:
            return json.loads(existing)
        # Parent and entries are committed atomically with the computed summary.
        conn.execute(insert(RUNS), dict(run_id=run_id, created_at=datetime.now(timezone.utc).isoformat(), summary='{}'))
        for side, rows in extracts.items():
            if rows:
                conn.execute(insert(ENTRIES), [dict(r, run_id=run_id, side=side, row_id=i)
                                              for i, r in enumerate(rows, 1)])
        joined = conn.execute(text(MATCH_SQL), {'run': run_id}).mappings().all()
        exceptions, counts = [], {}
        for row in joined:
            category = classify(row)
            if category:
                counts[category] = counts.get(category, 0) + 1
                exceptions.append(dict(row, category=category, status='OPEN', version=0, run_id=run_id,
                                       exception_id=hashlib.sha256(f'{run_id}:{row["reference"]}'.encode()).hexdigest()))
        if exceptions:
            conn.execute(insert(EXCEPTIONS), exceptions)
        totals = [dict(r) for r in conn.execute(text('''
            SELECT side, currency, COUNT(*) AS rows, SUM(amount_minor) AS amount_minor
            FROM rec_entries WHERE run_id=:run GROUP BY side,currency ORDER BY side,currency
        '''), {'run': run_id}).mappings()]
        summary = dict(run_id=run_id, ledger_rows=len(extracts['ledger']), settlement_rows=len(extracts['settlement']),
                       references=len(joined), matched=len(joined)-len(exceptions), exceptions=len(exceptions),
                       categories=counts, control_totals=totals)
        conn.execute(update(RUNS).where(RUNS.c.run_id == run_id).values(summary=json.dumps(summary)))
    return summary


def exceptions(engine, run_id, status=None):
    query = select(EXCEPTIONS).where(EXCEPTIONS.c.run_id == run_id)
    if status:
        query = query.where(EXCEPTIONS.c.status == status)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(query.order_by(EXCEPTIONS.c.reference)).mappings()]


def change_status(engine, exception_id, new_status, actor, note, expected_version):
    actor, note = actor.strip(), note.strip()
    if not actor or len(actor) > 100 or not note or len(note) > 2000:
        raise ValueError('Reviewer (1-100 characters) and evidence note (1-2000 characters) are required.')
    allowed = {'OPEN': {'INVESTIGATING'}, 'INVESTIGATING': {'RESOLVED', 'OPEN'}, 'RESOLVED': {'INVESTIGATING'}}
    with engine.begin() as conn:
        row = conn.execute(select(EXCEPTIONS).where(EXCEPTIONS.c.exception_id == exception_id)).mappings().first()
        if not row:
            raise ValueError('Unknown exception.')
        if row['version'] != expected_version:
            raise ValueError('This exception changed. Refresh before saving.')
        if new_status not in allowed[row['status']]:
            raise ValueError(f'Cannot move from {row["status"]} to {new_status}.')
        changed = conn.execute(update(EXCEPTIONS).where(
            EXCEPTIONS.c.exception_id == exception_id, EXCEPTIONS.c.version == expected_version
        ).values(status=new_status, version=expected_version + 1))
        if changed.rowcount != 1:
            raise ValueError('Concurrent update detected. Refresh before saving.')
        conn.execute(insert(EVENTS), dict(event_id=uuid4().hex, exception_id=exception_id,
                                         changed_at=datetime.now(timezone.utc).isoformat(), actor=actor,
                                         old_status=row['status'], new_status=new_status, note=note,
                                         version=expected_version + 1))


def history(engine, exception_id):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(EVENTS).where(EVENTS.c.exception_id == exception_id)
                                              .order_by(EVENTS.c.version)).mappings()]


def csv_export(rows):
    if not rows:
        return 'reference,category,status\r\n'
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader()
    # Neutralize spreadsheet formulas in user-authored evidence notes.
    writer.writerows({k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v
                     for k, v in r.items()} for r in rows)
    return out.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, default=ROOT / 'data' / 'ledger.csv')
    parser.add_argument('--settlement', type=Path, default=ROOT / 'data' / 'settlement.csv')
    parser.add_argument('--database-url')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts')
    args = parser.parse_args()
    engine = database(args.database_url)
    summary = reconcile(engine, args.ledger.read_bytes(), args.settlement.read_bytes())
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    (args.output / 'exceptions.csv').write_text(csv_export(exceptions(engine, summary['run_id'])), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
