"""Fresh, provisional post-close screening independent of the daily price package."""
import collections
import datetime
import json
import math
import pathlib
import re
import time
from zoneinfo import ZoneInfo

import pandas as pd

import rules
from run import fetch_report

REPORT = pathlib.Path('reports')
SKIPPED_COLUMNS = ['date', 'code', 'name', 'reason', 'stage']


def disclosure_key(row):
    values = [row.get('SECURITY_CODE'), row.get('TRADE_ID'),
              row.get('CHANGE_TYPE'), row.get('EXPLANATION')]
    if any(value is None or str(value).strip() == '' for value in values):
        raise ValueError('Missing disclosure identity')
    return tuple(str(value).strip() for value in values)


def _code(row):
    return str(row.get('SECURITY_CODE') or '').strip()


def _name(row):
    return str(row.get('SECURITY_NAME_ABBR') or '').strip()


def _in_scope(code):
    # Preserve the repository's existing two-market, 0/6-prefix universe.
    return len(code) == 6 and code.isdigit() and code.startswith(('0', '6'))


def _is_st(name):
    # Names provided by Eastmoney are contemporaneous disclosure names.
    return bool(re.search(r'(?i)(?:\*\s*ST|ST)', name))


def _skip(date, code, name, reason, stage):
    return dict(date=date, code=code, name=name, reason=str(reason), stage=stage)


def _date(value):
    return str(value or '')[:10]


def _price_check(row):
    close, gain = row.get('CLOSE_PRICE'), row.get('CHANGE_RATE')
    if not isinstance(close, (int, float)) or not isinstance(gain, (int, float)):
        raise ValueError('Missing daily close or gain')
    if not math.isfinite(close) or close <= 0 or not math.isfinite(gain) or gain <= -100:
        raise ValueError('Invalid daily close or gain')


def _identity_check(row, date):
    if _date(row.get('TRADE_DATE')) != date:
        raise ValueError('Incorrect disclosure date')
    return disclosure_key(row)


def validate_snapshot(snapshot):
    """Return an independently verified, scope-filtered snapshot with per-code audit.

    Only ordinary per-security quality problems are isolated. Collection-wide
    failures and contradictory source structure remain fatal. Discard all seat
    rows of an excluded code; never use a partly verified disclosure reason.
    """
    date = snapshot['date']
    if snapshot.get('_validated') is True:
        counts = snapshot.get('_source_counts', {})
        if any(not isinstance(counts.get(side), int) or counts[side] <= 0
               for side in ('overview', 'buy', 'sell')):
            raise RuntimeError('Verified snapshot lacks source availability provenance')
        if not isinstance(snapshot.get('skipped'), list):
            raise RuntimeError('Verified snapshot lacks skipped-record audit')
        if not all(isinstance(snapshot.get(side), list) for side in ('overview', 'buy', 'sell')):
            raise RuntimeError('Invalid persisted verified snapshot structure')
        if any(_code(row) == item.get('code') for side in ('overview', 'buy', 'sell')
               for row in snapshot[side] for item in snapshot['skipped']):
            raise RuntimeError('Persisted snapshot contains excluded stock rows')
        return snapshot
    for side in ('overview', 'buy', 'sell'):
        if not isinstance(snapshot.get(side), list) or not snapshot[side]:
            raise RuntimeError('Disclosure source empty/unavailable: ' + side)
        if any(not isinstance(row, dict) for row in snapshot[side]):
            raise RuntimeError('Invalid disclosure source structure: ' + side)

    source_counts = {side: len(snapshot[side]) for side in ('overview', 'buy', 'sell')}
    # A whole report on an unexpected trade date is a source-level failure.
    for side in ('overview', 'buy', 'sell'):
        if not any(_date(row.get('TRADE_DATE')) == date for row in snapshot[side]):
            raise RuntimeError('Disclosure source has no rows for requested date: ' + side)
    raw = snapshot['overview']
    by_code = collections.defaultdict(list)
    for row in raw:
        by_code[_code(row)].append(row)
    skipped = []
    admitted = {}
    for code, rows in sorted(by_code.items()):
        names = {_name(row) for row in rows if _name(row)}
        name = next(iter(names)) if len(names) == 1 else ''
        if not _in_scope(code):
            skipped.append(_skip(date, code, name, 'code_outside_scope', 'scope'))
        elif not names or len(names) != 1:
            skipped.append(_skip(date, code, name, 'Missing or conflicting security name', 'identity'))
        elif _is_st(name):
            skipped.append(_skip(date, code, name, 'ST security excluded', 'scope'))
        else:
            admitted[code] = rows

    # Preserve global source availability validation even when all candidates
    # are outside the strategy universe; this is not a failed-data workaround.
    for side in ('buy', 'sell'):
        if not any(_code(r) in by_code for r in snapshot[side]):
            raise RuntimeError('Seat source contains no corresponding disclosures: ' + side)

    seat_groups = {side: collections.defaultdict(list) for side in ('buy', 'sell')}
    orphan_codes = set()
    for side in ('buy', 'sell'):
        for row in snapshot[side]:
            code = _code(row)
            if code in admitted:
                seat_groups[side][code].append(row)
            elif code not in by_code and _in_scope(code):
                orphan_codes.add(code)
    for code in sorted(orphan_codes):
        skipped.append(_skip(date, code, '', 'Seat details without disclosure overview', 'identity'))
    valid_overview = []
    valid_seats = {'buy': [], 'sell': []}
    for code, rows in admitted.items():
        name = _name(rows[0])
        try:
            overview = {}
            prices = set()
            for row in rows:
                key = _identity_check(row, date)
                if key in overview:
                    raise ValueError('Duplicate disclosure overview: ' + str(key))
                _price_check(row)
                prices.add((row['CLOSE_PRICE'], row['CHANGE_RATE']))
                overview[key] = row
            if len(prices) != 1:
                raise ValueError('Conflicting daily close/gain across disclosure reasons')
            checked_sides = {}
            for side in ('buy', 'sell'):
                groups = collections.defaultdict(list)
                for row in seat_groups[side].get(code, []):
                    key = _identity_check(row, date)
                    groups[key].append(row)
                if set(groups) != set(overview):
                    raise ValueError('Missing or unexpected disclosure reasons: ' + side)
                verified_rows = []
                for key, detail in groups.items():
                    row = overview[key]
                    mask = row.get(side.upper() + '_SEAT_NEW')
                    if mask is None:
                        mask = row.get(side.upper() + '_SEAT')
                    mask = str(mask) if mask is not None else ''
                    if not mask or any(c not in '123' for c in mask):
                        raise ValueError('Unverified seat count: ' + side + ' ' + str(key))
                    if len(detail) != len(mask):
                        raise ValueError('Incomplete seat rows: ' + side + ' ' + str(key))
                    fingerprints = set()
                    for seat in detail:
                        dept = seat.get('OPERATEDEPT_NAME')
                        if not isinstance(dept, str) or not dept.strip():
                            raise ValueError('Missing seat identity: ' + side + ' ' + str(key))
                        # Exchange disclosures differ: Shanghai buy-side SELL/NET
                        # and sell-side BUY/NET may be undisclosed. Never
                        # fabricate zero amounts; validate only decision inputs.
                        required = (('BUY', 'SELL') if code.startswith('0') and side == 'buy'
                                    else ('BUY',) if side == 'buy'
                                    else ('SELL',))
                        for field in ('BUY', 'SELL', 'NET'):
                            value = seat.get(field)
                            if field in required or value is not None:
                                if (isinstance(value, bool) or
                                        not isinstance(value, (int, float)) or
                                        not math.isfinite(value) or value < 0 and field != 'NET'):
                                    raise ValueError('Invalid seat ' + field + ': ' + side + ' ' + str(key))
                        fingerprint = (seat.get('OPERATEDEPT_CODE'), dept,
                                       seat.get('BUY'), seat.get('SELL'), seat.get('NET'))
                        if fingerprint in fingerprints:
                            raise ValueError('Duplicate seat row: ' + side + ' ' + str(key))
                        fingerprints.add(fingerprint)
                    verified_rows.extend(detail)
                checked_sides[side] = verified_rows
        except (ValueError, KeyError, TypeError) as exc:
            msg = str(exc)
            stage = ('price' if any(part in msg.lower() for part in ('close', 'gain'))
                     else 'seats' if any(part in msg.lower() for part in ('seat', 'disclosure reasons'))
                     else 'identity')
            skipped.append(_skip(date, code, name, msg, stage))
            continue
        valid_overview.extend(rows)
        for side in ('buy', 'sell'):
            valid_seats[side].extend(checked_sides[side])

    result = dict(date=date, overview=valid_overview,
                  buy=valid_seats['buy'], sell=valid_seats['sell'], skipped=skipped,
                  _validated=True, _source_counts=source_counts)
    return result


def signature(snapshot):
    fields = {
        'overview': ('SECURITY_CODE', 'TRADE_DATE', 'SECURITY_NAME_ABBR',
                     'TRADE_ID', 'CHANGE_TYPE', 'EXPLANATION',
                     'CLOSE_PRICE', 'CHANGE_RATE', 'BUY_SEAT_NEW', 'SELL_SEAT_NEW',
                     'BUY_SEAT', 'SELL_SEAT'),
        'buy': ('SECURITY_CODE', 'TRADE_DATE', 'TRADE_ID', 'CHANGE_TYPE', 'EXPLANATION',
                'OPERATEDEPT_NAME', 'OPERATEDEPT_CODE', 'BUY', 'SELL', 'NET'),
        'sell': ('SECURITY_CODE', 'TRADE_DATE', 'TRADE_ID', 'CHANGE_TYPE', 'EXPLANATION',
                 'OPERATEDEPT_NAME', 'OPERATEDEPT_CODE', 'BUY', 'SELL', 'NET'),
    }
    return {side: sorted(json.dumps([str(row.get(k)) for k in keys],
                                   ensure_ascii=False) for row in snapshot[side])
            for side, keys in fields.items()}


def fetch_snapshot(date):
    return dict(date=date,
                overview=fetch_report('RPT_DAILYBILLBOARD_DETAILS', date),
                buy=fetch_report('RPT_BILLBOARD_DAILYDETAILSBUY', date),
                sell=fetch_report('RPT_BILLBOARD_DAILYDETAILSSELL', date))


def fetch_verified_snapshot(date, pause=15):
    first = fetch_snapshot(date)
    time.sleep(pause)
    second = fetch_snapshot(date)
    # Raw, not post-filtered snapshots. A skipped code changing is still a
    # meaningful source mutation and must never evade stability protection.
    if signature(first) != signature(second):
        raise RuntimeError('Disclosure still changing; retry on the next scheduled run')
    return validate_snapshot(second)


def screen_snapshot(snapshot):
    # Persisted verified snapshots retain source-availability provenance.
    snapshot = validate_snapshot(snapshot)
    prices = {}
    for row in snapshot['overview']:
        code = row['SECURITY_CODE']
        close, gain = row['CLOSE_PRICE'], row['CHANGE_RATE']
        prices[code] = dict(date=snapshot['date'], code=code,
                            name=row['SECURITY_NAME_ABBR'], close=close,
                            prev_close=close/(1+gain/100), open=close, volume=1)
        # open/volume are positive sentinels for screening only; never execute on them.
    df = pd.DataFrame(prices.values(), columns=['date', 'code', 'name', 'close',
                                              'prev_close', 'open', 'volume'])
    seats = {side: [r for r in snapshot[side]
                   if r.get('OPERATEDEPT_NAME', '').strip() == '机构专用']
             for side in ('buy', 'sell')}
    return rules.screen(df, write=False, buy=seats['buy'], sell=seats['sell'])


def publish(date, now):
    REPORT.mkdir(exist_ok=True)
    status = dict(date=date, checked_at=now.isoformat(), status='checking',
                  price_source='Eastmoney disclosure close and daily gain',
                  execution='screening_only_no_orders')
    skipped = []
    signals = pd.DataFrame(columns=['date', 'code', 'qualifying_amount'])
    try:
        snapshot = fetch_verified_snapshot(date)
        skipped = snapshot.get('skipped', [])
        signals, audit = screen_snapshot(snapshot)
        signals.to_csv(REPORT/'current_signals.csv', index=False, encoding='utf-8-sig')
        if audit.empty and len(audit.columns) == 0:
            audit = pd.DataFrame(columns=['date', 'code', 'reason'])
        audit.to_csv(REPORT/'screening_audit_latest.csv', index=False, encoding='utf-8-sig')
        status.update(status='provisional', signals=len(signals),
                      disclosure_groups=len(snapshot['overview']),
                      skipped_stocks=len(skipped),
                      note='两次原始抓取稳定；异常证券已隔离，不代表全市场已完成发布。')
        lines = ['# 当日龙虎榜选股', '', '数据日期：' + date,
                 '检查时间：' + now.isoformat(), '',
                 '盘后初筛，持续复查；模拟账本晚间单独更新。',
                 f'逐股过滤或隔离：{len(skipped)} 只。详情见 skipped_stocks.csv。', '',
                 '|代码|名称|符合条件的机构买入额（万元）|',
                 '|---|---|---:|']
        for r in signals.to_dict('records'):
            lines.append(f"|{r['code']}|{r['name']}|{r['qualifying_amount']/10000:.2f}|")
        if signals.empty:
            lines.extend(['', ('所有股票均被过滤或隔离；无可验证候选。' if
                               not snapshot['overview'] else
                               '本次已验证披露数据中无符合信号。')])
    except Exception as error:
        status.update(status='waiting_for_disclosure', error=str(error))
        # Source-wide failure: never leave prior valid signals looking current.
        pd.DataFrame(columns=['date', 'code', 'qualifying_amount']).to_csv(
            REPORT/'current_signals.csv', index=False, encoding='utf-8-sig')
        pd.DataFrame(columns=['date', 'code', 'reason']).to_csv(
            REPORT/'screening_audit_latest.csv', index=False, encoding='utf-8-sig')
        lines = ['# 当日龙虎榜选股', '', '数据日期：' + date,
                 '检查时间：' + now.isoformat(), '',
                 '数据待齐或接口失败；本次结果不能视为无信号。', '', str(error)]
    pd.DataFrame(skipped, columns=SKIPPED_COLUMNS).to_csv(
        REPORT/'skipped_stocks.csv', index=False, encoding='utf-8-sig')
    (REPORT/'screening_status.json').write_text(
        json.dumps(status, ensure_ascii=False, indent=2))
    (REPORT/'latest_signals.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps(status, ensure_ascii=False))
    if status['status'] == 'waiting_for_disclosure':
        raise RuntimeError(status['error'])
    return signals


if __name__ == '__main__':
    import os
    now = datetime.datetime.now(ZoneInfo('Asia/Shanghai'))
    try:
        publish(now.date().isoformat(), now)
    finally:
        path = os.environ.get('GITHUB_STEP_SUMMARY')
        if path and (REPORT/'latest_signals.md').exists():
            pathlib.Path(path).write_text((REPORT/'latest_signals.md').read_text())
