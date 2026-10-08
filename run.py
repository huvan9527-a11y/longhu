"""Immutable daily snapshots; replay both independent forward paper accounts."""
import json, pathlib, datetime, urllib.parse, time, os
from zoneinfo import ZoneInfo
import pandas as pd, numpy as np
import prices, rules, engine
ROOT=pathlib.Path('data');REPORT=pathlib.Path('reports')
def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))
def fetch_report(report,date,institutions=False):
    rows=[];page=1;expected=None
    while True:
        filt=f"(TRADE_DATE='{date}')"+('(OPERATEDEPT_NAME="机构专用")' if institutions else '')
        params=dict(reportName=report,columns='ALL',pageSize=500,pageNumber=page,sortColumns='SECURITY_CODE',sortTypes='1',filter=filt)
        value=json.loads(prices.get('https://datacenter-web.eastmoney.com/api/data/v1/get?'+urllib.parse.urlencode(params)))
        result=value.get('result')
        if not value.get('success') or not result:raise RuntimeError('Disclosure not available: '+report+' '+date+' '+str(value)[:200])
        if expected is None:expected=result['count']
        if expected!=result['count']:raise RuntimeError('Disclosure changed during pagination')
        rows.extend(result.get('data') or [])
        if page>=result['pages']:break
        page+=1;time.sleep(1.3)
    if len(rows)!=expected:raise RuntimeError('Incomplete disclosure pagination')
    if any(r['TRADE_DATE'][:10]!=date for r in rows):raise RuntimeError('Incorrect disclosure date')
    return rows

def prepare(df):
    df=df.sort_values(['code','date']).reset_index(drop=True)
    if df.duplicated(['code','date']).any():raise ValueError('Duplicate daily price')
    if not np.isfinite(df[['prev_close','open','close','high','low','volume','amount']].to_numpy()).all():raise ValueError('Nonfinite price')
    df=df[(df.close>0)&(df.prev_close>0)].copy();g=df.groupby('code',sort=False)
    df['adj_close']=(df.close/df.prev_close).groupby(df.code).cumprod()*g.prev_close.transform('first')
    df['factor']=df.adj_close/df.close
    for key in ['open','high','low']:df['adj_'+key]=df[key]*df.factor
    df['volume_mean_prev5']=df.groupby('code').volume.transform(lambda x:x.shift(1).rolling(5,min_periods=5).mean())
    return df

def export_account(name,result):
    trades,equity,orders,events,summary=result;target=REPORT/name;target.mkdir(parents=True,exist_ok=True)
    old=target/'equity.csv'
    if old.exists():
        previous=pd.read_csv(old)
        if len(previous):
            check=equity.set_index('date').reindex(previous.date)
            if not np.allclose(previous.equity,check.equity,atol=.01,rtol=0):raise ValueError('Historical ledger changed: '+name)
    for filename,frame in [('trades',trades),('equity',equity),('orders',orders),('events',events)]:frame.to_csv(target/(filename+'.csv'),index=False,encoding='utf-8-sig')
    summary['account']=name;summary['initial_cash']=200000;summary['execution']='daily_bar_proxy'
    save(target/'summary.json',summary)
    pending=orders[orders.status=='no_next_session_in_window'] if len(orders) else pd.DataFrame()
    pending.to_csv(target/'next_open_orders.csv',index=False,encoding='utf-8-sig')
    return summary

def main():
    now=datetime.datetime.now(ZoneInfo('Asia/Shanghai'));today=now.date().isoformat();config=json.loads(pathlib.Path('config.json').read_text());start=config['start_date']
    if config['initial_cash']!=200000:raise ValueError('Engine initial capital must match config')
    ROOT.mkdir(exist_ok=True);prices.ROOT.mkdir(parents=True,exist_ok=True);REPORT.mkdir(exist_ok=True)
    param=f'sh000001,day,{(now.date()-datetime.timedelta(days=360)).isoformat()},{today},640,'
    calendar=json.loads(prices.get('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?'+urllib.parse.urlencode({'param':param})))['data']['sh000001']['day']
    sessions=sorted({x[0] for x in calendar if x[0]<=today})
    if not sessions:raise ValueError('Calendar unavailable')
    live=[d for d in sessions if d>=start]
    latest=sessions[-1]
    # No intraday execution or signals from incomplete daily data.
    if latest==today and now.hour<20:
        live=[d for d in live if d<today];sessions=[d for d in sessions if d<today];latest=sessions[-1]
    if not live:
        for name in ['original','reentry']:
            target=REPORT/name;target.mkdir(exist_ok=True)
            save(target/'summary.json',dict(account=name,initial_cash=200000,final_equity=200000,total_return=0,entries=0,status='waiting_for_first_session',start=start))
        (REPORT/'latest.md').write_text(f'# 龙虎榜双模拟仓\n\n启动日：{start}。行情最新日期：{latest}。尚无启动后的交易日。\n\n原方案：200,000.00元；重新入场方案：200,000.00元。\n')
        return
    warm=sessions[max(0,sessions.index(live[0])-6):sessions.index(live[0])]
    for date in warm+live:
        prices.package(date)
        if date not in live:continue
        snapshot=ROOT/'disclosures'/(date+'.json')
        if not snapshot.exists():
            overview=fetch_report('RPT_DAILYBILLBOARD_DETAILS',date)
            if not overview:raise RuntimeError('Daily billboard is empty; cannot verify publication')
            # Retrieve all seats, then filter institutions. Zero institution seats is valid;
            # an empty whole-market endpoint is not treated as a published negative signal.
            buy=fetch_report('RPT_BILLBOARD_DAILYDETAILSBUY',date)
            sell=fetch_report('RPT_BILLBOARD_DAILYDETAILSSELL',date)
            if not buy or not sell:raise RuntimeError('Seat disclosure incomplete')
            known={r['SECURITY_CODE'] for r in overview}
            if not known.issubset({r['SECURITY_CODE'] for r in buy}) or not known.issubset({r['SECURITY_CODE'] for r in sell}):raise RuntimeError('Missing disclosed stock seats')
            save(snapshot,dict(date=date,overview=overview,buy=buy,sell=sell))
    df=prepare(pd.concat([pd.read_csv(prices.ROOT/(d+'.csv'),dtype={'code':str}) for d in warm+live],ignore_index=True))
    snapshots=[json.loads((ROOT/'disclosures'/(d+'.json')).read_text()) for d in live]
    for side in ['BUY','SELL']:save(ROOT/(side+'_institutions.json'),[r for s in snapshots for r in s[side.lower()] if r.get('OPERATEDEPT_NAME','').strip()=='机构专用'])
    signals,audit=rules.screen(df,write=False)
    audit.to_csv(REPORT/'screening_audit.csv',index=False,encoding='utf-8-sig');signals.to_csv(REPORT/'signals.csv',index=False,encoding='utf-8-sig')
    summaries=[]
    for name,reentry in [('original',False),('reentry',True)]:summaries.append(export_account(name,engine.simulate(df,live,signals,reentry=reentry)))
    text=f'# 龙虎榜双模拟仓\n\n最新交易日：{live[-1]}；各20万元独立账户。\n\n|方案|资产|累计收益|开仓笔数|\n|---|---:|---:|---:|\n'
    for s in summaries:text+=f"|{s['account']}|{s['final_equity']:,.2f}|{s['total_return']:.2%}|{s['entries']}|\n"
    text+='\n今晚确认信号，下一交易日开盘尝试成交；成交记录由次日晚间日线模拟。日线不能保证盘中成交顺序。除权除息影响持仓时停止更新并报错，等待核验，避免生成虚假股数。\n'
    (REPORT/'latest.md').write_text(text)
    summary_path=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:pathlib.Path(summary_path).write_text(text)
if __name__=='__main__':main()
