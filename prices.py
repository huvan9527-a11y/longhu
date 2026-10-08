import urllib.request,urllib.parse,json,time,pathlib,io,zipfile,struct,csv
ROOT=pathlib.Path("data/prices")
def get(url):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'}),timeout=40) as r:return r.read()
        except Exception:
            if attempt==2:raise
            time.sleep(1+attempt)
def stock(m,c):return (m=='sh' and c.startswith('6')) or (m=='sz' and c.startswith('0'))
def package(day):
    dest=ROOT/(day+'.csv'); ymd=day.replace('-','')
    if dest.exists():return day,'cached'
    data=get('https://www.tdx.com.cn/products/data/data/g4day/'+ymd+'.zip')
    z=zipfile.ZipFile(io.BytesIO(data)); rows=[]; market_counts={}
    for market in ['sh','sz']:
        cod=z.read(market+ymd[2:]+'.cod'); md=z.read(market+ymd[2:]+'.md1'); n=0
        assert len(cod)%150==0 and len(md)%512==0 and len(cod)//150==len(md)//512
        for off in range(0,len(cod),150):
            rec=cod[off:off+150]; code=rec[:6].rstrip(b'\0 ').decode('ascii'); seq=struct.unpack('<H',rec[32:34])[0]
            b=md[seq*512:(seq+1)*512]; prev=struct.unpack('<d',b[4:12])[0]; o,h,l,c=struct.unpack('<4d',b[12:44])
            if c<=0:continue
            n+=1
            if not stock(market,code):continue
            name=rec[40:72].split(b'\0')[0].decode('gbk').strip(); vol=struct.unpack('<Q',b[56:64])[0]; amount=struct.unpack('<d',b[72:80])[0]
            rows.append([day,market,code,name,prev,o,h,l,c,vol,amount])
        market_counts[market]=n
    assert market_counts['sh']>10000 and market_counts['sz']>3000,market_counts
    with open(dest.with_suffix('.tmp'),'w',newline='') as f:
        w=csv.writer(f);w.writerow(['date','market','code','name','prev_close','open','high','low','close','volume','amount']);w.writerows(rows)
    dest.with_suffix('.tmp').rename(dest)
    return day,len(rows)
