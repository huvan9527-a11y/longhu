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


def fixture():
    identity = dict(SECURITY_CODE='000420', TRADE_DATE='2026-10-09 00:00:00',
                    TRADE_ID='123', CHANGE_TYPE='daily_gain',
                    EXPLANATION='日涨幅偏离值达到7%的前5只证券')
    overview = dict(identity, SECURITY_NAME_ABBR='吉林化纤', CLOSE_PRICE=3.81,
                    CHANGE_RATE=10.12, BUY_SEAT_NEW='11333', SELL_SEAT_NEW='11111')
    buyers = []
    for i, amount in enumerate([76087200, 65125700, 43561400, 20815400, 20153900]):
        buyers.append(dict(identity, OPERATEDEPT_NAME='机构专用' if i >= 2 else '普通席位',
                           OPERATEDEPT_CODE=str(i), BUY=amount, SELL=0, NET=amount))
    sellers = [dict(identity, OPERATEDEPT_NAME='普通席位', OPERATEDEPT_CODE=str(i),
                    BUY=0, SELL=1000000, NET=-1000000) for i in range(5)]
    return dict(date='2026-10-09', overview=[overview], buy=buyers, sell=sellers)


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

    def test_same_stock_missing_second_reason_rejected(self):
        snapshot = fixture()
        other = dict(snapshot['overview'][0], TRADE_ID='124', EXPLANATION='换手率')
        snapshot['overview'].append(other)
        with self.assertRaisesRegex(ValueError, 'disclosure reasons'):
            preview.validate_snapshot(snapshot)

    def test_partial_seats_rejected(self):
        snapshot = fixture()
        snapshot['sell'].pop()
        with self.assertRaisesRegex(ValueError, 'Incomplete seat'):
            preview.validate_snapshot(snapshot)

    def test_unchanged_data_stable_despite_row_order(self):
        first = fixture()
        second = copy.deepcopy(first)
        second['buy'].reverse()
        with patch.object(preview, 'fetch_snapshot', side_effect=[first, second]):
            self.assertEqual(preview.fetch_verified_snapshot('2026-10-09', pause=0), second)

    def test_changing_disclosure_not_frozen(self):
        first = fixture()
        second = copy.deepcopy(first)
        second['buy'][2]['BUY'] += 1
        with patch.object(preview, 'fetch_snapshot', side_effect=[first, second]):
            with self.assertRaisesRegex(RuntimeError, 'still changing'):
                preview.fetch_verified_snapshot('2026-10-09', pause=0)

    def test_failed_fetch_clears_stale_signals_and_reports_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root/'current_signals.csv').write_text('date,code\n2026-10-08,000001\n')
            now = datetime.datetime(2026, 10, 9, 17, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
            with patch.object(preview, 'REPORT', root), patch.object(
                    preview, 'fetch_verified_snapshot', side_effect=RuntimeError('partial seats')):
                with self.assertRaisesRegex(RuntimeError, 'partial seats'):
                    preview.publish('2026-10-09', now)
            status = json.loads((root/'screening_status.json').read_text())
            self.assertEqual(status['status'], 'waiting_for_disclosure')
            self.assertEqual(status['date'], '2026-10-09')
            self.assertTrue(pd.read_csv(root/'current_signals.csv').empty)


if __name__ == '__main__':
    unittest.main()
