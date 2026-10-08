import unittest
import pandas as pd
import engine as s
from rules import group_qualification

def fixture(changes=None):
 dates=pd.bdate_range('2026-10-09',periods=15).strftime('%Y-%m-%d').tolist();rows=[]
 for i,d in enumerate(dates):
  r=dict(date=d,code='000001',name='测试股',open=10.,close=10.,high=10.,low=10.,prev_close=10.,volume=1000000,factor=1.,volume_mean_prev5=1000000.)
  r.update((changes or {}).get(i,{}))
  for k in ['open','close','high','low']:r['adj_'+k]=r[k]
  rows.append(r)
 sig=pd.DataFrame([dict(date=dates[0],code='000001',name='测试股',qualifying_amount=25000000,disclosure_window='one_day',disclosure_reason='测试')])
 return pd.DataFrame(rows),dates,sig
class StrategyTests(unittest.TestCase):
 def test_cap_and_deadline(self):
  df,dates,sig=fixture();tr,eq,o,_,_=s.simulate(df,dates,sig)
  self.assertEqual(tr.iloc[0].holding_sessions,10);self.assertEqual(tr.iloc[0].sell_date,dates[10]);self.assertLessEqual(o.iloc[0].investment,200000/3)
 def test_Tplus1_lock(self):
  df,dates,sig=fixture({1:dict(low=8.8),2:dict(open=10.5,high=10.5,low=10.5,close=10.5)})
  tr,*_=s.simulate(df,dates,sig);self.assertEqual(tr.iloc[0].sell_date,dates[2]);self.assertEqual(tr.iloc[0].partial_shares,0)
 def test_whole_trade_breakeven(self):
  df,dates,sig=fixture({2:dict(high=10.7,low=10.,close=10.5),3:dict(low=9.2)})
  tr,*_=s.simulate(df,dates,sig);self.assertGreater(tr.iloc[0].partial_shares,0);self.assertAlmostEqual(tr.iloc[0].pnl,0,places=6)
 def test_no_later_profit_take(self):
  df,dates,sig=fixture({3:dict(high=11.)});tr,*_=s.simulate(df,dates,sig);self.assertEqual(tr.iloc[0].partial_shares,0)
 def test_reentry(self):
  df,dates,sig=fixture({2:dict(low=9.,high=10.),3:dict(close=10.2,high=10.3,volume=2000000),4:dict(open=10.2,close=10.2,high=10.3,low=10.1),5:dict(low=8.9,high=10.2),6:dict(close=10.4,high=10.5,volume=4000000)})
  tr,*_=s.simulate(df,dates,sig,reentry=True);self.assertEqual(len(tr),2);self.assertEqual(tr.is_reentry.sum(),1);self.assertEqual(tr[tr.is_reentry].iloc[0].buy_date,dates[4])
 def test_empty_signals(self):
  df,dates,_=fixture();sig=pd.DataFrame(columns=['date','code','qualifying_amount']);tr,eq,*_=s.simulate(df,dates,sig,reentry=True);self.assertEqual(eq.iloc[-1].equity,200000);self.assertEqual(len(tr),0)
 def test_individual_threshold(self):
  rows=[dict(OPERATEDEPT_NAME='机构专用',BUY=15000000,SELL=0,NET=15000000,OPERATEDEPT_CODE=str(i)) for i in range(2)]
  self.assertFalse(group_qualification('000001',rows)[0]);self.assertTrue(group_qualification('600001',rows)[0]);rows[0]['BUY']=25000000;self.assertTrue(group_qualification('000001',rows)[0]);rows[0]['SELL']=None;self.assertFalse(group_qualification('000001',rows)[0])
 def test_corporate_action_blocks_fake_shares(self):
  df,dates,sig=fixture();df.loc[2,'factor']=1.4
  with self.assertRaisesRegex(ValueError,'corporate action'):s.simulate(df,dates,sig)
if __name__=='__main__':unittest.main()
