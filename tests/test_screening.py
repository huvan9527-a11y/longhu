import copy
import datetime
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

import preview


def fixture(code='000420', name='吉林化纤'):
    identity = dict(SECURITY_CODE=code, TRADE_DATE='2026-10-09 00:00:00',
                    TRADE_ID='123', CHANGE_TYPE='daily_gain',
                    EXPLANATION='日涨幅偏离值达到7%的前5只证券')
    overview = dict(identity, SECURITY_NAME_ABBR=name, CLOSE_PRICE=3.81,
                    CHANGE_RATE=10.12, BUY_SEAT_NEW='11333', SELL_SEAT_NEW='11111')
    buyers = []
    for i, amount in enumerate([76087200, 65125700, 43561400, 20815400, 20153900]):
        buyers.append(dict(identity, OPERATEDEPT_NAME='机构专用' if i >= 2 else '普通席位',
                           OPERATEDEPT_CODE=str(i), BUY=amount, SELL=0, NET=amount))
    sellers = [dict(identity, OPERATEDEPT_NAME='普通席位', OPERATEDEPT_CODE=str(i),
                    BUY=0, SELL=1000000, NET=-1000000) for i in range(5)]
    return dict(date='2026-10-09', overview=[overview], buy=buyers, sell=sellers)


def combine(*snapshots):
    return dict(date=snapshots[0]['date'],
                **{side: [r for s in snapshots for r in s[side]]
                   for side in ('overview', 'buy', 'sell')})


class ScreeningTests(unittest.TestCase):
    def test_jilin_passes_current_rules_before_twenty(self):
        snapshot = preview.validate_snapshot(fixture())
        signals, audit = preview.screen_snapshot(snapshot)
        self.assertEqual(signals.code.tolist(), ['000420'])
        self.assertEqual(signals.iloc[0].qualifying_amount, 43561400)
        self.assertTrue(audit.iloc[0].qualifies)

    def test_institution_seller_excludes(self):
        snapshot = fixture()
        snapshot['sell'][0]['OPERATEDEPT_NAME'] = '机构专用'
        signals, audit = preview.screen_snapshot(snapshot)
        self.assertTrue(signals.empty)
        self.assertEqual(audit.iloc[0].reason, 'institution_in_disclosed_sell_seats')

    def test_same_stock_missing_second_reason_isolated(self):
        snapshot = fixture()
        other = dict(snapshot['overview'][0], TRADE_ID='124', EXPLANATION='换手率')
        snapshot['overview'].append(other)
        filtered = preview.validate_snapshot(snapshot)
        self.assertFalse(filtered['overview'])
        self.assertIn('disclosure reasons', filtered['skipped'][0]['reason'])

    def test_partial_seats_isolated(self):
        snapshot = fixture()
        snapshot['sell'].pop()
        filtered = preview.validate_snapshot(snapshot)
        self.assertFalse(filtered['overview'])
        self.assertIn('Incomplete seat', filtered['skipped'][0]['reason'])

    def test_200056_invalid_sell_mask_excluded_by_scope(self):
        excluded = fixture('200056', '测试B股')
        excluded['overview'][0]['SELL_SEAT_NEW'] = None
        filtered = preview.validate_snapshot(combine(fixture(), excluded))
        self.assertEqual([r['SECURITY_CODE'] for r in filtered['overview']], ['000420'])
        self.assertEqual(filtered['skipped'][0]['stage'], 'scope')
        self.assertEqual(preview.screen_snapshot(filtered)[0].code.tolist(), ['000420'])
        self.assertFalse(any(r['SECURITY_CODE'] == '200056' for r in filtered['sell']))

    def test_st_missing_seats_filtered_before_validation(self):
        st = fixture('600111', '*ST测试')
        st['sell'].clear()
        filtered = preview.validate_snapshot(combine(fixture(), st))
        self.assertEqual(len(filtered['overview']), 1)
        self.assertEqual(filtered['skipped'][0]['code'], '600111')
        self.assertEqual(filtered['skipped'][0]['stage'], 'scope')

    def test_one_bad_one_good(self):
        bad = fixture('600111', '测试沪股')
        bad['sell'].pop()
        filtered = preview.validate_snapshot(combine(fixture(), bad))
        self.assertEqual(preview.screen_snapshot(filtered)[0].code.tolist(), ['000420'])
        self.assertEqual(len(filtered['skipped']), 1)
        self.assertEqual(filtered['skipped'][0]['code'], '600111')

    def test_missing_single_stock_price_isolated(self):
        bad = fixture('600111', '测试沪股')
        bad['overview'][0].pop('CLOSE_PRICE')
        filtered = preview.validate_snapshot(combine(fixture(), bad))
        self.assertEqual(preview.screen_snapshot(filtered)[0].code.tolist(), ['000420'])
        self.assertEqual(filtered['skipped'][0]['stage'], 'price')

    def test_missing_identity_is_not_non_st(self):
        x = fixture('600111', '')
        filtered = preview.validate_snapshot(combine(fixture(), x))
        self.assertEqual(len(filtered['overview']), 1)
        self.assertEqual(filtered['skipped'][0]['stage'], 'identity')

    def test_all_excluded_reports_no_candidate_and_correct_count(self):
        x = fixture('200056', '测试B股')
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            now = datetime.datetime(2026, 10, 9, 17, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
            with patch.object(preview, 'REPORT', root), patch.object(
                    preview, 'fetch_verified_snapshot', return_value=preview.validate_snapshot(x)):
                signals = preview.publish('2026-10-09', now)
            self.assertTrue(signals.empty)
            self.assertEqual(json.loads((root/'screening_status.json').read_text())['skipped_stocks'], 1)
            self.assertEqual(len(pd.read_csv(root/'skipped_stocks.csv', dtype={'code': str})), 1)
            self.assertIn('所有股票均被过滤', (root/'latest_signals.md').read_text())

    def test_empty_source_still_fatal(self):
        x = fixture()
        x['buy'] = []
        with self.assertRaisesRegex(RuntimeError, 'source empty'):
            preview.validate_snapshot(x)

    def test_source_fetch_exception_still_fatal(self):
        with patch.object(preview, 'fetch_snapshot', side_effect=RuntimeError('provider offline')):
            with self.assertRaisesRegex(RuntimeError, 'provider offline'):
                preview.fetch_verified_snapshot('2026-10-09', pause=0)

    def test_associated_seats_excluded_and_not_duplicated(self):
        x = combine(fixture(), fixture('200056', '测试B股'), fixture('600111', '*ST测试'))
        filtered = preview.validate_snapshot(x)
        for side in ('buy', 'sell'):
            self.assertEqual(len(filtered[side]), 5)
            self.assertEqual({r['SECURITY_CODE'] for r in filtered[side]}, {'000420'})
        self.assertEqual(len(filtered['skipped']), 2)

    def test_duplicate_seat_row_excludes_only_bad_code(self):
        bad = fixture('600111', '测试沪股')
        bad['buy'][1] = copy.deepcopy(bad['buy'][0])
        filtered = preview.validate_snapshot(combine(fixture(), bad))
        self.assertEqual(len(filtered['overview']), 1)
        self.assertIn('Duplicate seat', filtered['skipped'][0]['reason'])

    def test_verified_snapshot_roundtrip_with_no_admitted_stocks(self):
        filtered = preview.validate_snapshot(fixture('200056', '测试B股'))
        frozen = json.loads(json.dumps(filtered, ensure_ascii=False))
        self.assertEqual(preview.validate_snapshot(frozen), filtered)
        self.assertTrue(preview.screen_snapshot(frozen)[0].empty)

    def test_wrong_date_on_one_security_isolated(self):
        bad = fixture('600111', '测试沪股')
        bad['overview'][0]['TRADE_DATE'] = '2026-10-08 00:00:00'
        filtered = preview.validate_snapshot(combine(fixture(), bad))
        self.assertEqual(preview.screen_snapshot(filtered)[0].code.tolist(), ['000420'])
        self.assertIn('date', filtered['skipped'][0]['reason'])

    def test_all_rows_wrong_date_is_system_failure(self):
        bad = fixture()
        for side in ('overview', 'buy', 'sell'):
            for row in bad[side]:
                row['TRADE_DATE'] = '2026-10-08 00:00:00'
        with self.assertRaisesRegex(RuntimeError, 'requested date'):
            preview.validate_snapshot(bad)

    def test_orphan_seat_stock_is_reported_without_leaking_to_strategy(self):
        one = fixture()
        orphan = fixture('600111', '测试沪股')
        one['buy'].extend(orphan['buy'])
        filtered = preview.validate_snapshot(one)
        self.assertEqual(preview.screen_snapshot(filtered)[0].code.tolist(), ['000420'])
        self.assertEqual(filtered['skipped'][0]['code'], '600111')
        self.assertTrue(all(r['SECURITY_CODE'] == '000420' for r in filtered['buy']))

    def test_unchanged_data_stable_despite_row_order(self):
        first = fixture()
        second = copy.deepcopy(first)
        second['buy'].reverse()
        with patch.object(preview, 'fetch_snapshot', side_effect=[first, second]):
            verified = preview.fetch_verified_snapshot('2026-10-09', pause=0)
            self.assertEqual(verified['buy'], second['buy'])

    def test_changing_disclosure_not_frozen(self):
        first = fixture()
        second = copy.deepcopy(first)
        second['buy'][2]['BUY'] += 1
        with patch.object(preview, 'fetch_snapshot', side_effect=[first, second]):
            with self.assertRaisesRegex(RuntimeError, 'still changing'):
                preview.fetch_verified_snapshot('2026-10-09', pause=0)

    def test_changing_skipped_code_does_not_bypass_consistency(self):
        first = combine(fixture(), fixture('200056', '测试B股'))
        second = copy.deepcopy(first)
        second['sell'][-1]['SELL'] += 1
        with patch.object(preview, 'fetch_snapshot', side_effect=[first, second]):
            with self.assertRaisesRegex(RuntimeError, 'still changing'):
                preview.fetch_verified_snapshot('2026-10-09', pause=0)

    def test_publish_preserves_valid_stock_and_skipped_report(self):
        bad = fixture('600111', '测试沪股')
        bad['sell'].pop()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            now = datetime.datetime(2026, 10, 9, 17, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
            filtered = preview.validate_snapshot(combine(fixture(), bad))
            with patch.object(preview, 'REPORT', root), patch.object(
                    preview, 'fetch_verified_snapshot', return_value=filtered):
                signals = preview.publish('2026-10-09', now)
            self.assertEqual(signals.code.tolist(), ['000420'])
            self.assertEqual(len(pd.read_csv(root/'skipped_stocks.csv', dtype={'code': str})), 1)
            self.assertEqual(json.loads((root/'screening_status.json').read_text())['signals'], 1)

    def test_failed_fetch_clears_stale_signals_and_reports_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root/'current_signals.csv').write_text('date,code\n2026-10-08,000001\n')
            now = datetime.datetime(2026, 10, 9, 17, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
            with patch.object(preview, 'REPORT', root), patch.object(
                    preview, 'fetch_verified_snapshot', side_effect=RuntimeError('provider offline')):
                with self.assertRaisesRegex(RuntimeError, 'provider offline'):
                    preview.publish('2026-10-09', now)
            status = json.loads((root/'screening_status.json').read_text())
            self.assertEqual(status['status'], 'waiting_for_disclosure')
            self.assertEqual(status['date'], '2026-10-09')
            self.assertTrue(pd.read_csv(root/'current_signals.csv').empty)


if __name__ == '__main__':
    unittest.main()
