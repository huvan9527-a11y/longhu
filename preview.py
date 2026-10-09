"""Fresh, provisional post-close screening independent of the daily price package."""
import collections
import datetime
import json
import math
import pathlib
import time
from zoneinfo import ZoneInfo

import pandas as pd

import rules
from run import fetch_report

REPORT = pathlib.Path('reports')


def disclosure_key(row):
    values = [row.get('SECURITY_CODE'), row.get('TRADE_ID'),
              row.get('CHANGE_TYPE'), row.get('EXPLANATION')]
    if any(value is None or str(value).strip() == '' for value in values):
        raise ValueError('Missing disclosure identity')
    return tuple(str(value).strip() for value in values)


def validate_snapshot(snapshot):
    if any(row.get('TRADE_DATE', '')[:10] != snapshot['date']
           for row in snapshot['overview']):
        raise ValueError('Incorrect overview disclosure date')
    overview = {disclosure_key(row): row for row in snapshot['overview']}
    if not overview or len(overview) != len(snapshot['overview']):
        raise ValueError('Empty or duplicate disclosure overview')
    for side in ('buy', 'sell'):
        groups = collections.defaultdict(list)
        for row in snapshot[side]:
            if row.get('TRADE_DATE', '')[:10] != snapshot['date']:
                raise ValueError('Incorrect seat disclosure date')
            groups[disclosure_key(row)].append(row)
        if set(groups) != set(overview):
            raise ValueError('Missing or unexpected disclosure reasons: ' + side)
        for key, rows in groups.items():
            row = overview[key]
            mask = row.get(side.upper() + '_SEAT_NEW')
            if mask is None:
                mask = row.get(side.upper() + '_SEAT')
            text = str(mask) if mask is not None else ''
            # Eastmoney encodes one digit per disclosed seat in this field.
            if not text or any(c not in '123' for c in text):
                raise ValueError('Unverified seat count: ' + side + ' ' + str(key))
            if len(rows) != len(text):
                raise ValueError('Incomplete seat rows: ' + side + ' ' + str(key))
    return snapshot


def signature(snapshot):
    fields = {
        'overview': ('SECURITY_CODE', 'TRADE_ID', 'CHANGE_TYPE', 'EXPLANATION',
                     'CLOSE_PRICE', 'CHANGE_RATE', 'BUY_SEAT_NEW', 'SELL_SEAT_NEW',
                     'BUY_SEAT', 'SELL_SEAT'),
        'buy': ('SECURITY_CODE', 'TRADE_ID', 'CHANGE_TYPE', 'EXPLANATION',
                'OPERATEDEPT_NAME', 'OPERATEDEPT_CODE', 'BUY', 'SELL', 'NET'),
        'sell': ('SECURITY_CODE', 'TRADE_ID', 'CHANGE_TYPE', 'EXPLANATION',
                 'OPERATEDEPT_NAME', 'OPERATEDEPT_CODE', 'BUY', 'SELL', 'NET'),
    }
    return {side: sorted(json.dumps([str(row.get(k)) for k in keys],
                                   ensure_ascii=False) for row in snapshot[side])
            for side, keys in fields.items()}


def fetch_snapshot(date):
    snapshot = dict(date=date,
                    overview=fetch_report('RPT_DAILYBILLBOARD_DETAILS', date),
                    buy=fetch_report('RPT_BILLBOARD_DAILYDETAILSBUY', date),
                    sell=fetch_report('RPT_BILLBOARD_DAILYDETAILSSELL', date))
    return validate_snapshot(snapshot)


def fetch_verified_snapshot(date, pause=15):
    first = fetch_snapshot(date)
    time.sleep(pause)
    second = fetch_snapshot(date)
    if signature(first) != signature(second):
        raise RuntimeError('Disclosure still changing; retry on the next scheduled run')
    return second


def screen_snapshot(snapshot):
    prices = {}
    for row in snapshot['overview']:
        code = row['SECURITY_CODE']
        if not code.startswith(('0', '6')):
            continue
        close, gain = row.get('CLOSE_PRICE'), row.get('CHANGE_RATE')
        if not isinstance(close, (int, float)) or not isinstance(gain, (int, float)):
            raise ValueError('Missing daily close or gain: ' + code)
        if not math.isfinite(close) or close <= 0 or not math.isfinite(gain) or gain <= -100:
            raise ValueError('Invalid daily close or gain: ' + code)
        record = dict(date=snapshot['date'], code=code, name=row['SECURITY_NAME_ABBR'],
                      close=close, prev_close=close/(1+gain/100), open=close,
                      volume=1)
        # open/volume are positive sentinels for screening only; never execute on them.
        if code in prices and prices[code] != record:
            raise ValueError('Conflicting daily close/gain across disclosure reasons: ' + code)
        prices[code] = record
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
    try:
        snapshot = fetch_verified_snapshot(date)
        signals, audit = screen_snapshot(snapshot)
        signals.to_csv(REPORT/'current_signals.csv', index=False, encoding='utf-8-sig')
        audit.to_csv(REPORT/'screening_audit_latest.csv', index=False, encoding='utf-8-sig')
        status.update(status='provisional', signals=len(signals),
                      disclosure_groups=len(snapshot['overview']),
                      note='两次抓取稳定且席位齐全；不代表全市场已完成发布，后续继续复查。')
        lines = ['# 当日龙虎榜选股', '', '数据日期：' + date,
                 '检查时间：' + now.isoformat(), '',
                 '盘后初筛，持续复查；模拟账本晚间单独更新。', '',
                 '|代码|名称|符合条件的机构买入额（万元）|',
                 '|---|---|---:|']
        for r in signals.to_dict('records'):
            lines.append(f"|{r['code']}|{r['name']}|{r['qualifying_amount']/10000:.2f}|")
        if signals.empty:
            lines.append('')
            lines.append('本次已披露数据中无符合信号。')
    except Exception as error:
        status.update(status='waiting_for_disclosure', error=str(error))
        # Never leave yesterday's or a failed previous attempt's CSV looking current.
        pd.DataFrame(columns=['date', 'code', 'qualifying_amount']).to_csv(
            REPORT/'current_signals.csv', index=False, encoding='utf-8-sig')
        pd.DataFrame(columns=['date', 'code', 'reason']).to_csv(
            REPORT/'screening_audit_latest.csv', index=False, encoding='utf-8-sig')
        lines = ['# 当日龙虎榜选股', '', '数据日期：' + date,
                 '检查时间：' + now.isoformat(), '',
                 '数据待齐或接口失败；本次结果不能视为无信号。', '', str(error)]
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
