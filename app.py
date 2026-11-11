"""Run: streamlit run app.py --server.port 8502"""
import json
import os
import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
import reconcile as r

st.set_page_config(page_title='Transaction Reconciliation', page_icon='⚖️', layout='wide')
st.markdown('**DATA OPERATIONS / RECONCILIATION**')
st.title('Every difference deserves an explanation.')
st.caption('Transaction Reconciliation & Exception Tracker · Exact-reference matching · Synthetic data')


@st.cache_resource
def connection(url):
    return r.database(url or None)


try:
    engine = connection(os.getenv('DATABASE_URL', ''))
except SQLAlchemyError:
    st.error('Cannot connect to the configured database. Check DATABASE_URL and the server, or unset DATABASE_URL to use the local SQLite demo. See README for the WSL PostgreSQL setup.')
    st.stop()
with st.sidebar:
    st.header('Compare two extracts')
    st.caption(f'Database: {engine.dialect.name}. Amounts are integer minor units; currencies are never mixed into one total.')
    source = st.radio('Data source', ['Demo extracts', 'Upload CSVs'])
    payloads = {}
    if source == 'Upload CSVs':
        for name in ['ledger', 'settlement']:
            file = st.file_uploader(f'{name}.csv', type='csv', key=name)
            if file:
                payloads[name] = file.getvalue()
    else:
        payloads = {n: (r.ROOT / 'data' / f'{n}.csv').read_bytes() for n in ['ledger', 'settlement']}
    if st.button('Run reconciliation', type='primary'):
        try:
            if set(payloads) != {'ledger', 'settlement'}:
                raise ValueError('Supply both extracts.')
            result = r.reconcile(engine, payloads['ledger'], payloads['settlement'])
            st.session_state['run_id'] = result['run_id']
            st.success('Comparison saved. Reruns retain existing investigation history.')
        except (ValueError, UnicodeError) as exc:
            st.error(str(exc))
with engine.connect() as conn:
    runs = [dict(row) for row in conn.execute(select(r.RUNS).order_by(r.RUNS.c.created_at.desc())).mappings()]
if not runs:
    st.info('Select Run reconciliation to compare 3,000 ledger rows with the settlement extract.')
    st.markdown('**Matching order:** duplicate key → missing side → currency difference → amount difference → matched.')
    st.stop()
run_ids = [x['run_id'] for x in runs]
selected = st.selectbox('Saved comparison', run_ids,
                        index=run_ids.index(st.session_state.get('run_id')) if st.session_state.get('run_id') in run_ids else 0,
                        format_func=lambda s: s[:16])
summary = json.loads(next(x['summary'] for x in runs if x['run_id'] == selected))
all_exceptions = r.exceptions(engine, selected)
cols = st.columns(4)
for col, label, value in zip(cols, ['Matched references', 'Exception references', 'Ledger rows', 'Unresolved exceptions'],
                             [summary['matched'], summary['exceptions'], summary['ledger_rows'],
                              sum(x['status'] != 'RESOLVED' for x in all_exceptions)]):
    col.metric(label, value)
st.caption('One outcome per reference. Duplicate references are isolated before comparing amounts; an arbitrary duplicate is never selected as a match.')
overview, investigation, totals = st.tabs(['Exceptions', 'Investigate & resolve', 'Control totals & evidence'])
with overview:
    status = st.selectbox('Status filter', ['All', 'OPEN', 'INVESTIGATING', 'RESOLVED'])
    shown = [x for x in all_exceptions if status == 'All' or x['status'] == status]
    st.dataframe(pd.DataFrame(shown).drop(columns=['exception_id', 'run_id'], errors='ignore'), hide_index=True, width='stretch')
    st.bar_chart(pd.DataFrame({'category': list(summary['categories']), 'references': list(summary['categories'].values())}).set_index('category'))
    st.download_button('Download exceptions CSV', r.csv_export(all_exceptions), 'exceptions.csv', 'text/csv')
with investigation:
    if all_exceptions:
        item = st.selectbox('Exception reference', all_exceptions, format_func=lambda x: f'{x["reference"]} · {x["category"]} · {x["status"]}')
        st.json({k: v for k, v in item.items() if k not in {'exception_id', 'run_id'}})
        allowed = {'OPEN': ['INVESTIGATING'], 'INVESTIGATING': ['RESOLVED', 'OPEN'], 'RESOLVED': ['INVESTIGATING']}
        with st.form('transition'):
            target = st.selectbox('Next status', allowed[item['status']])
            actor = st.text_input('Reviewer')
            note = st.text_area('Evidence / investigation note', help='Record what was checked and why this change is justified.')
            if st.form_submit_button('Save status change'):
                try:
                    r.change_status(engine, item['exception_id'], target, actor, note, item['version'])
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        events = r.history(engine, item['exception_id'])
        st.subheader('Investigation history')
        st.dataframe(pd.DataFrame(events).drop(columns=['event_id', 'exception_id'], errors='ignore'), hide_index=True)
        if events:
            st.download_button('Download investigation history', r.csv_export(events), 'investigation-history.csv', 'text/csv')
    else:
        st.success('Every reference matched.')
with totals:
    st.dataframe(pd.DataFrame(summary['control_totals']), hide_index=True)
    st.caption('Raw extract totals include duplicate rows. They are control totals, not reconciled net spend. All three supported currencies use 100 minor units per major unit.')
    st.json(summary)
    st.download_button('Download comparison JSON', json.dumps(summary, indent=2), 'reconciliation-summary.json', 'application/json')
    st.caption('Reviewers are self-declared in this local demo. The event log is transactional but is not tamper-proof or an authenticated approval system.')
