from rules import *
def simulate(df,dates,sig,fixed_budget=None,reentry=False):
    evaluation=[d for d in dates if d>='2026-01-01'];idx={d:i for i,d in enumerate(evaluation)}
    market={d:z.set_index('code',drop=False).to_dict('index') for d,z in df.groupby('date')};signals={d:z.to_dict('records') for d,z in sig.groupby('date')}
    cash=INITIAL;active={};trades=[];equity=[];orders=[];events=[];watches=[]
    def event(date,code,kind,**kwargs):events.append(dict(date=date,code=code,event=kind,**kwargs))
    def partial(code,date,r,price,phase):
        nonlocal cash
        p=active[code];current=p['units']*r['factor'];kcb=code.startswith(('688','689'))
        shares=math.floor(current/2) if kcb else math.floor(current/2/100)*100
        minimum=200 if kcb else 100
        if shares<minimum:
            event(date,code,'half_sale_below_minimum_lot');return
        units=shares/r['factor'];fill=price*(1-SLIP);value=units*fill;fees=fee(value,True);net=value-fees;cash+=net
        p['units']-=units;p.update(partial_date=date,partial_price=fill/r['factor'],partial_shares=shares,partial_value=value,partial_fees=fees,realized_net=net)
        # Solve for the price that leaves the entire trade at zero after ALL costs.
        newstop=(p['cost']-net)/(p['units']*(1-SLIP)*(1-COM));p['stop']=max(p['stop'],newstop);p['breakeven_stop']=p['stop']/r['factor']
        event(date,code,'half_profit_taken',phase=phase,shares=shares,fill_price=fill/r['factor'],value=value,fees=fees,new_stop=p['stop']/r['factor'],buy_date=p['buy_date'])
    def row(p,code,date,r,price,reason,phase,closed):
        fill=price*(1-SLIP) if closed else price;value=p['units']*fill;fees=fee(value,True) if closed else 0.
        total=p['realized_net']+value-fees;pnl=total-p['cost']
        return dict(code=code,name=p['name'],signal_date=p['signal_date'],buy_date=p['buy_date'],sell_date=date if closed else '',buy_price=p['raw_entry'],sell_price=fill/r['factor'] if closed else None,entry_shares=p['shares'],remaining_shares_at_exit=p['units']*r['factor'],buy_value=p['value'],buy_fees=p['fees'],sell_value=value+p['partial_value'],sell_fees=fees+p['partial_fees'],pnl=pnl,net_return=pnl/p['cost'],holding_sessions=idx[date]-p['entry_index']+1,exit_reason=reason,exit_phase=phase,status='closed' if closed else 'open',stop_locked_on=p.get('stop_locked_on',''),qualifying_amount=p['qualifying_amount'],disclosure_window=p['disclosure_window'],disclosure_reason=p['disclosure_reason'],corporate_action_proxy=p['ca'],partial_date=p['partial_date'],partial_price=p['partial_price'],partial_shares=p['partial_shares'],partial_value=p['partial_value'],partial_fees=p['partial_fees'],breakeven_stop=p['breakeven_stop'],first_sellable_date=p.get('first_sellable_date',''),is_reentry=bool(p.get('is_reentry',False)),reentry_parent_buy_date=p.get('reentry_parent_buy_date',''),reentry_parent_exit_date=p.get('reentry_parent_exit_date',''),reentry_trigger_date=p.get('reentry_trigger_date',''))
    def close(code,date,r,price,reason,phase):
        nonlocal cash
        p=active.pop(code);fill=price*(1-SLIP);value=p['units']*fill;fees=fee(value,True);cash+=value-fees
        if reentry and 'stop' in reason.lower() and not p.get('is_reentry',False):
            watches.append(dict(code=code,exit_index=idx[date],exit_date=date,exit_high=r['adj_high'],parent=p.copy(),consumed=False))
        trades.append(row(p,code,date,r,price,reason,phase,True));event(date,code,'final_exit',phase=phase,shares=p['units']*r['factor'],fill_price=fill/r['factor'],value=value,fees=fees,buy_date=p['buy_date'],reason=reason)
    def execute_stop(code,date,r,price,phase):
        p=active[code];lower=price_limit(r,False)
        if lower is not None and price/r['factor']<=lower+.005:
            p['pending']='stop_blocked_limit_down';event(date,code,'stop_blocked_limit_down');return
        close(code,date,r,price,'breakeven_stop' if p['partial_shares'] else 'intraday_10pct_stop',phase)
    for i,date in enumerate(evaluation):
        day=market[date];sold_open=set()
        for code in list(active):
            p=active[code];r=day.get(code)
            if not r or r['open']<=0 or r['volume']<=0:continue
            if abs(r['factor']/p['entry_factor']-1)>1e-6:raise ValueError('Held stock corporate action requires verification: '+code+' '+date)
            if p.get('pending') or r['adj_open']<=p['stop']:
                if at_limit(r,'down'):
                    p['pending']=p.get('pending') or 'gap_stop_blocked';event(date,code,'exit_blocked_at_open_limit_down');continue
                reason=p.get('pending') or ('gap_below_breakeven_stop' if p['partial_shares'] else 'gap_below_10pct_stop')
                close(code,date,r,r['adj_open'],reason,'open');sold_open.add(code);continue
            if not p.get('first_sellable_date'):
                p['first_sellable_date']=date
                if r['adj_open']>=p['entry_price']*1.05:partial(code,date,r,r['adj_open'],'open')
        previous=dates[dates.index(date)-1] if dates.index(date)>0 else None
        for signal in sorted(signals.get(previous,[]),key=lambda x:(bool(x.get('is_reentry',False)),-x['qualifying_amount'],x['code'])):
            code=signal['code'];r=day.get(code);why=None
            metadata=dict(is_reentry=bool(signal.get('is_reentry',False)),reentry_parent_buy_date=signal.get('reentry_parent_buy_date',''),reentry_parent_exit_date=signal.get('reentry_parent_exit_date',''))
            if code in active:why='already_held_no_add'
            elif code in sold_open:why='already_sold_today_no_reentry'
            elif not r or r['open']<=0 or r['volume']<=0:why='missing_or_suspended'
            elif at_limit(r,'up'):why='open_at_limit_up_unfilled'
            elif at_limit(r,'down'):why='open_at_limit_down_unfilled'
            if why:orders.append(dict(signal_date=previous,order_date=date,code=code,status=why,**metadata));continue
            assets=cash+sum(p['units']*(day.get(c,{}).get('adj_open') or p['last_close']) for c,p in active.items());budget=min(cash,assets/3) if fixed_budget is None else min(fixed_budget,assets/3);raw=r['open']*(1+SLIP)
            if fixed_budget is not None and cash<budget:
                orders.append(dict(signal_date=previous,order_date=date,code=code,status='cash_below_fixed_target_skip',**metadata));continue
            kcb=code.startswith(('688','689'));step=1 if kcb else 100;minimum=200 if kcb else 100
            shares=math.floor(budget/(raw*(1+COM))/step)*step;shares=min(shares,100000 if kcb else 1000000)
            while shares>=minimum and shares*raw+fee(shares*raw)>budget+1e-7:shares-=step
            if shares<minimum:orders.append(dict(signal_date=previous,order_date=date,code=code,status='cash_below_minimum_order',**metadata));continue
            value=shares*raw;fees=fee(value);cost=value+fees;assert cost<=assets/3+1e-7 and cost<=cash+1e-7;cash-=cost;entry=r['adj_open']*(1+SLIP)
            active[code]=dict(signal,signal_date=previous,buy_date=date,entry_index=i,shares=shares,units=shares/r['factor'],raw_entry=raw,entry_price=entry,stop=entry*.9,cost=cost,value=value,fees=fees,last_close=r['adj_close'],ca=False,entry_factor=r['factor'],partial_date='',partial_price=None,partial_shares=0,partial_value=0.,partial_fees=0.,realized_net=0.,breakeven_stop=None)
            orders.append(dict(signal_date=previous,order_date=date,code=code,status='filled_daily_bar_proxy',account_assets_before_order=assets,investment=cost,investment_ratio=cost/assets,**metadata))
        for code in list(active):
            p=active[code];r=day.get(code)
            if not r or r['open']<=0 or r['volume']<=0:
                if i-p['entry_index']>=9:p['pending']=p.get('pending') or 'day10_exit_delayed_suspension'
                continue
            p['last_close']=r['adj_close'];p['last_factor']=r['factor']
            if abs(r['factor']/p['entry_factor']-1)>1e-6:raise ValueError('Held stock corporate action requires verification: '+code+' '+date)
            if i==p['entry_index']:
                if r['adj_low']<=p['stop']:
                    p['pending']='buy_day_stop_locked_Tplus1';p['stop_locked_on']=date;event(date,code,'buy_day_10pct_stop_triggered_locked_Tplus1')
                continue
            if p.get('pending'):continue
            if r['adj_low']<=p['stop']:
                execute_stop(code,date,r,p['stop'],'intraday_daily_bar_proxy');continue
            if p.get('first_sellable_date')==date and not p['partial_shares'] and r['adj_high']>=p['entry_price']*1.05:
                partial(code,date,r,p['entry_price']*1.05,'intraday_daily_bar_proxy')
                if p['partial_shares'] and r['adj_low']<=p['stop']:
                    event(date,code,'same_bar_tp_and_new_stop_conservative_order');execute_stop(code,date,r,p['stop'],'intraday_daily_bar_proxy');continue
            if i-p['entry_index']>=9:
                lower=price_limit(r,False)
                if lower is not None and r['close']<=lower+.005:
                    p['pending']='day10_exit_blocked_limit_down';event(date,code,'day10_close_exit_blocked')
                else:close(code,date,r,r['adj_close'],'day10_close_exit','close_auction_proxy')
        # Reentry candidates are evaluated only after today's close, never using next-day prices.
        if reentry:
            for watch in watches:
                age=i-watch['exit_index'];code=watch['code'];r=day.get(code)
                if watch['consumed'] or age<=0:continue
                if age>5:watch['consumed']=True;continue
                if code in active:
                    watch['consumed']=True;event(date,code,'reentry_watch_cancelled_existing_position');continue
                if not r or r['open']<=0 or r['volume']<=0:continue
                mean=r.get('volume_mean_prev5',float('nan'))
                if not np.isfinite(mean) or mean<=0:continue
                if r['adj_close']>watch['exit_high'] and r['volume']>mean:
                    parent=watch['parent'];new_signal={k:parent[k] for k in ['code','name','qualifying_amount','disclosure_window','disclosure_reason']}
                    new_signal.update(date=date,is_reentry=True,reentry_parent_buy_date=parent['buy_date'],reentry_parent_exit_date=watch['exit_date'],reentry_trigger_date=date)
                    if any(z['code']==code for z in signals.get(date,[])):
                        event(date,code,'reentry_duplicate_prefer_existing_signal')
                    else:
                        signals.setdefault(date,[]).append(new_signal)
                        event(date,code,'reentry_signal_confirmed',parent_buy_date=parent['buy_date'],parent_exit_date=watch['exit_date'],stop_day_adj_high=watch['exit_high'],trigger_adj_close=r['adj_close'],trigger_volume=r['volume'],mean_volume_prev5=mean)
                    watch['consumed']=True
        asset=cash+sum(p['units']*p['last_close'] for p in active.values());assert cash>=-1e-6
        equity.append(dict(date=date,equity=asset,cash=cash,positions=len(active),exposure=(asset-cash)/asset,nav=asset/INITIAL))
    for signal in signals.get(evaluation[-1],[]):orders.append(dict(signal_date=evaluation[-1],order_date='',code=signal['code'],status='no_next_session_in_window'))
    for code,p in active.items():
        r=dict(factor=p.get('last_factor',p['entry_factor']));trades.append(row(p,code,evaluation[-1],r,p['last_close'],p.get('pending','end_mark_to_market'),'not_sold',False))
    tr=pd.DataFrame(trades);eq=pd.DataFrame(equity);order=pd.DataFrame(orders);ev=pd.DataFrame(events)
    if len(tr):tr=tr.sort_values(['buy_date','code']).reset_index(drop=True);tr.insert(0,'trade_id',np.arange(1,len(tr)+1))
    eq['drawdown']=eq.equity/eq.equity.cummax().clip(lower=INITIAL)-1
    assert abs(eq.iloc[-1].equity-INITIAL-(tr.pnl.sum() if len(tr) else 0))<.01
    closed=tr[tr.status=='closed'] if len(tr) else tr;positive=closed[closed.pnl>0].pnl.sum() if len(closed) else 0;negative=-closed[closed.pnl<0].pnl.sum() if len(closed) else 0
    summary=dict(start=evaluation[0],end=evaluation[-1],sessions=len(evaluation),signals=len(sig),entries=len(tr),closed_trades=len(closed),open_trades=len(tr)-len(closed),total_return=eq.iloc[-1].nav-1,max_drawdown=eq.drawdown.min(),win_rate=float((closed.pnl>0.01).mean()) if len(closed) else None,mean_trade_return=float(closed.net_return.mean()) if len(closed) else None,profit_factor=float(positive/negative) if negative else None,average_exposure=eq.exposure.mean(),final_equity=eq.iloc[-1].equity,corporate_action_trades=int(tr.corporate_action_proxy.sum()) if len(tr) else 0,partial_profit_trades=int((tr.partial_shares>0).sum()) if len(tr) else 0)
    summary.update(reentry_entries=int(tr.is_reentry.sum()) if len(tr) else 0,reentry_signals=int((ev.event=='reentry_signal_confirmed').sum()) if len(ev) else 0)
    return tr,eq,order,ev,summary
