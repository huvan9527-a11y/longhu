"""User-confirmed institution-seat strategy; January 2026 onward, daily-bar execution proxy."""
import pathlib,json,math,hashlib,zipfile
import pandas as pd,numpy as np
ROOT=pathlib.Path('data');OUT=pathlib.Path('reports')
INITIAL=200_000.;SLIP=.001;COM=.00015
def fee(v,sell=False):return v*COM
def price_limit(r,up=True):
    name=r['name'];code=r['code']
    if name.startswith(('N','C')):return None
    pct=.2 if code.startswith(('688','689')) else (.05 if 'ST' in name.upper() and r['date']<'2026-07-06' else .1)
    return math.floor(r['prev_close']*(1+(pct if up else -pct))*100+.50000001)/100
def at_limit(r,side):
    p=price_limit(r,side=='up')
    if p is None:return False
    return r['open']>=p-.005 if side=='up' else r['open']<=p+.005
def group_qualification(code,rows):
    if not rows:return False,None,'no_institution_buyer'
    for r in rows:
        if r.get('OPERATEDEPT_NAME','').strip()!='机构专用':return False,None,'identity_unverified'
    valid=lambda v:isinstance(v,(int,float)) and math.isfinite(v) and v>=0
    if code.startswith('0'):
        good=[r for r in rows if valid(r.get('BUY')) and valid(r.get('SELL')) and r['BUY']>20_000_000 and r['SELL']==0]
        if not good and any(not valid(r.get('BUY')) or not valid(r.get('SELL')) for r in rows):return False,None,'missing_or_invalid_required_amount'
        return (True,max(r['BUY'] for r in good),'single_institution_buy_gt20m_sell_zero') if good else (False,None,'no_single_institution_gt20m_with_zero_sell')
    if code.startswith('6'):
        if any(not valid(r.get('BUY')) for r in rows):return False,None,'missing_or_invalid_required_buy_amount'
        fingerprints=[(r['BUY'],r.get('SELL'),r.get('NET'),r.get('OPERATEDEPT_CODE')) for r in rows]
        if len(set(fingerprints))!=len(fingerprints):return False,None,'indistinguishable_duplicate_institution_rows'
        total=sum(r['BUY'] for r in rows)
        return (True,total,'institution_buy_sum_gt20m') if total>20_000_000 else (False,None,'institution_buy_sum_not_gt20m')
    return False,None,'code_outside_scope'
def screen(df,cutoff=None,write=True,buy=None,sell=None):
    if buy is None:buy=json.load(open(ROOT/'BUY_institutions.json'))
    if sell is None:sell=json.load(open(ROOT/'SELL_institutions.json'))
    seller_days=set()
    for r in sell:
        if r.get('OPERATEDEPT_NAME','').strip()=='机构专用':seller_days.add((r['TRADE_DATE'][:10],r['SECURITY_CODE']))
    groups={}
    for r in buy:
        code=r['SECURITY_CODE'];date=r['TRADE_DATE'][:10]
        if not code.startswith(('0','6')) or cutoff and date>cutoff:continue
        if not r.get('TRADE_ID') or not r.get('CHANGE_TYPE') or not r.get('EXPLANATION'):continue
        k=(date,code,str(r['TRADE_ID']),str(r['CHANGE_TYPE']),r['EXPLANATION']);groups.setdefault(k,[]).append(r)
    prices=df.set_index(['date','code']).to_dict('index');audit=[];qualified=[]
    for k,rows in groups.items():
        date,code,tid,ctype,explain=k;r=prices.get((date,code));ok,amount,reason=group_qualification(code,rows)
        if (date,code) in seller_days:ok=False;reason='institution_in_disclosed_sell_seats'
        elif not r:ok=False;reason='price_missing'
        elif r['close']/r['prev_close']-1<.05-1e-12:ok=False;reason='daily_gain_below_5pct'
        elif r['open']<=0 or r['volume']<=0:ok=False;reason='signal_day_not_trading'
        rec=dict(date=date,code=code,name=r['name'] if r else '',trade_id=tid,disclosure_reason=explain,disclosure_window='multi_day' if any(w in explain for w in ['连续','三个','三日','3日']) else 'one_day',institution_buy_rows=len(rows),institution_buy_sum=None if any(x.get('BUY') is None for x in rows) else sum(x['BUY'] for x in rows),institution_sell_within_buy_rows=None if any(x.get('SELL') is None for x in rows) else sum(x['SELL'] for x in rows),qualifying_amount=amount,gain_pct=(r['close']/r['prev_close']-1) if r else None,qualifies=ok,reason=reason)
        audit.append(rec)
        if ok:qualified.append(rec)
    sig=pd.DataFrame(qualified)
    if sig.empty:return pd.DataFrame(columns=['date','code','qualifying_amount']),pd.DataFrame(audit)
    for col in ['institution_buy_sum','institution_sell_within_buy_rows','qualifying_amount','gain_pct']:
        sig[col]=pd.to_numeric(sig[col],errors='coerce').astype(float)
    # Evaluate each disclosed record independently; never sum different disclosure reasons.
    sig=sig.sort_values(['date','code','qualifying_amount','trade_id'],ascending=[True,True,False,True]).drop_duplicates(['date','code']).reset_index(drop=True)
    if write:
        sig.to_csv(OUT/'qualified_signals.csv',index=False,encoding='utf-8-sig');pd.DataFrame(audit).to_csv(OUT/'screening_audit.csv',index=False,encoding='utf-8-sig')
    return sig,pd.DataFrame(audit)

