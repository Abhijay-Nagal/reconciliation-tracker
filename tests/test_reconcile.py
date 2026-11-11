import os
from pathlib import Path
import sys
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import reconcile as r


@pytest.fixture
def engine(tmp_path):
    e = r.database(os.getenv('TEST_DATABASE_URL') or 'sqlite:///' + (tmp_path / 'rec.db').as_posix())
    r.META.drop_all(e)
    r.META.create_all(e)
    yield e
    e.dispose()


def demo(engine):
    return r.reconcile(engine, (r.ROOT / 'data' / 'ledger.csv').read_bytes(),
                       (r.ROOT / 'data' / 'settlement.csv').read_bytes())


def extract(body):
    return ('reference,amount_minor,currency\n' + body).encode()


def test_demo_has_expected_independent_categories(engine):
    result = demo(engine)
    assert result['ledger_rows'] == 3000
    assert result['settlement_rows'] == 3004
    assert result['matched'] == 2981
    assert result['exceptions'] == 25
    assert result['categories'] == {'missing_settlement': 5, 'missing_ledger': 6, 'amount_mismatch': 7,
                                    'currency_mismatch': 4, 'duplicate_reference': 3}
    assert result['matched'] + result['exceptions'] == result['references']


def test_duplicate_keys_do_not_produce_a_cartesian_match(engine):
    result = r.reconcile(engine, extract('R1,100,INR\nR1,200,INR\n'), extract('R1,100,INR\nR1,200,INR\n'))
    assert result['matched'] == 0
    assert result['exceptions'] == 1
    assert r.exceptions(engine, result['run_id'])[0]['ledger_count'] == 2


def test_currency_mismatch_takes_precedence_over_amount(engine):
    result = r.reconcile(engine, extract('R1,100,INR\n'), extract('R1,200,GBP\n'))
    assert result['categories'] == {'currency_mismatch': 1}


def test_empty_side_is_a_valid_missing_extract(engine):
    result = r.reconcile(engine, extract('R1,100,INR\n'), extract(''))
    assert result['categories'] == {'missing_settlement': 1}


@pytest.mark.parametrize('body', ['R1,1.25,INR\n', 'R1,-1,INR\n', 'R1,100,USD\n', ',100,INR\n', 'R1,100\n'])
def test_bad_extract_is_atomic(engine, body):
    with pytest.raises(ValueError):
        r.reconcile(engine, extract(body), extract('R1,100,INR\n'))
    with engine.connect() as conn:
        assert conn.execute(select(r.RUNS)).all() == []


def test_status_history_and_rerun_preserve_investigation(engine):
    result = demo(engine)
    item = r.exceptions(engine, result['run_id'])[0]
    r.change_status(engine, item['exception_id'], 'INVESTIGATING', 'Demo analyst', 'Compared both source files.', 0)
    r.change_status(engine, item['exception_id'], 'RESOLVED', 'Demo analyst', 'Confirmed correction in the source system.', 1)
    assert demo(engine) == result
    assert r.exceptions(engine, result['run_id'])[0]['status'] == 'RESOLVED'
    assert [x['new_status'] for x in r.history(engine, item['exception_id'])] == ['INVESTIGATING', 'RESOLVED']


def test_stale_version_cannot_overwrite_or_add_history(engine):
    result = demo(engine)
    item = r.exceptions(engine, result['run_id'])[0]
    r.change_status(engine, item['exception_id'], 'INVESTIGATING', 'Analyst A', 'Initial investigation.', 0)
    with pytest.raises(ValueError, match='Refresh'):
        r.change_status(engine, item['exception_id'], 'RESOLVED', 'Analyst B', 'Stale browser.', 0)
    assert len(r.history(engine, item['exception_id'])) == 1


def test_invalid_transition_and_empty_note_are_rejected(engine):
    item = r.exceptions(engine, demo(engine)['run_id'])[0]
    with pytest.raises(ValueError):
        r.change_status(engine, item['exception_id'], 'RESOLVED', 'Analyst', 'Skipped investigation.', 0)
    with pytest.raises(ValueError):
        r.change_status(engine, item['exception_id'], 'INVESTIGATING', 'Analyst', '', 0)
    assert r.history(engine, item['exception_id']) == []


def test_export_neutralizes_user_supplied_spreadsheet_formula():
    assert "'=HYPERLINK" in r.csv_export([{'note': '=HYPERLINK("x")'}])


def test_dashboard_and_exception_transition(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + (tmp_path / 'app.db').as_posix())
    app = AppTest.from_file(str(r.ROOT / 'app.py'), default_timeout=30).run()
    assert not app.exception
    app.button[0].click().run()
    assert not app.exception
    assert app.metric[0].value == '2981'
    for field in app.text_input:
        if field.label == 'Reviewer':
            field.set_value('Demo analyst')
    app.text_area[0].set_value('Checked source extracts.')
    next(b for b in app.button if b.label == 'Save status change').click().run()
    assert not app.exception
    assert 'INVESTIGATING' in app.selectbox[2].value['status']
