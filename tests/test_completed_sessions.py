import datetime
import json
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
import run

class CompletedSessions(unittest.TestCase):
    def sessions(self, stamp):
        data={'data':{'sh000001':{'day':[['2026-10-23'],['2026-10-26'],['2026-10-27']]}}}
        with patch.object(run.prices,'get',return_value=json.dumps(data)):
            return run.completed_sessions(datetime.datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo('Asia/Shanghai')))

    def test_delayed_midnight_run_uses_previous_close(self):
        self.assertEqual(self.sessions('2026-10-27T00:04:00')[-1],'2026-10-26')

    def test_intraday_bar_is_not_a_completed_session(self):
        self.assertEqual(self.sessions('2026-10-27T10:30:00')[-1],'2026-10-26')

    def test_postclose_includes_today(self):
        self.assertEqual(self.sessions('2026-10-27T17:10:00')[-1],'2026-10-27')

    def test_weekend_and_monday_morning_use_friday(self):
        for stamp in ['2026-10-24T09:41:00','2026-10-26T07:15:00']:
            self.assertEqual(self.sessions(stamp)[-1],'2026-10-23')
