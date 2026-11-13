"""Conservative app-local OpenAI budget reservations; not an official account balance."""
import datetime
import decimal
import json
import sqlite3
import uuid
from pathlib import Path
import core
import ai_service

D=decimal.Decimal
NANO=D(1_000_000_000)
MILLION=D(1_000_000)
def _value(value,label):
    try: n=D(str(value))
    except decimal.InvalidOperation: raise ValueError(label+'必須是非負數字') from None
    if not n.is_finite() or n<0 or n>D('10000000'): raise ValueError(label+'必須是合理的非負數字')
    return n
def _usd_nano(value): return int((value*NANO).to_integral_value(rounding=decimal.ROUND_CEILING))
def _db(path):
    db=core.connect(str(path))
    db.execute('''CREATE TABLE IF NOT EXISTS api_usage(id TEXT PRIMARY KEY, created_day TEXT NOT NULL, model TEXT NOT NULL,
      status TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, reserved_nano INTEGER NOT NULL,
      final_nano INTEGER, input_price TEXT NOT NULL, output_price TEXT NOT NULL, excluded INTEGER NOT NULL DEFAULT 0)''')
    db.execute('''CREATE TABLE IF NOT EXISTS api_usage_tool(id TEXT PRIMARY KEY REFERENCES api_usage(id), kind TEXT NOT NULL,
      price_per_call TEXT NOT NULL, max_calls INTEGER NOT NULL, actual_calls INTEGER)''')
    return db
def get_settings(path):
    db=_db(path)
    try:
        row=db.execute("SELECT value FROM settings WHERE key='api_budget'").fetchone()
        if not row: return {'daily':'0','monthly':'0','prices':{}}
        settings=json.loads(row[0]); return settings if isinstance(settings,dict) else {'daily':'0','monthly':'0','prices':{}}
    finally: db.close()
def save_settings(path,daily,monthly,model,input_price,output_price,web_search_price=None):
    day=_value(daily,'每日預算'); month=_value(monthly,'每月預算')
    inp=_value(input_price,'輸入價格'); out=_value(output_price,'輸出價格')
    if not model.strip() or len(model)>150: raise ValueError('請填寫模型名稱')
    if (day>0 or month>0) and (inp<=0 or out<=0): raise ValueError('設定預算時，請填入此模型的輸入與輸出單價')
    db=_db(path)
    try:
        row=db.execute("SELECT value FROM settings WHERE key='api_budget'").fetchone()
        try: old=json.loads(row[0]) if row else {}
        except ValueError: old={}
        prices=old.get('prices',{}) if isinstance(old,dict) else {}
        if not isinstance(prices,dict): prices={}
        prices[model.strip()]={'input':str(inp),'output':str(out)}
        data={'daily':str(day),'monthly':str(month),'prices':prices}
        web=_value(web_search_price if web_search_price is not None else (old.get('web_search_price','0') if isinstance(old,dict) else '0'),'每次網路搜尋價格')
        data['web_search_price']=str(web)
        with db: db.execute("INSERT INTO settings(key,value) VALUES('api_budget',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(data),))
    finally: db.close()
def reserve(path,question,sources,model,max_tokens,now=None,extra_input_tokens=0,tool_kind=None,tool_calls=0):
    db=_db(path); now=now or datetime.datetime.now().astimezone(); day=now.date().isoformat(); month=day[:7]
    try:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute("SELECT value FROM settings WHERE key='api_budget'").fetchone()
        try: cfg=json.loads(row[0]) if row else {'daily':'0','monthly':'0','prices':{}}
        except ValueError: raise ValueError('預算設定無法讀取，已阻擋請求') from None
        if not isinstance(cfg,dict) or not isinstance(cfg.get('prices',{}),dict): raise ValueError('預算設定格式錯誤，已阻擋請求')
        daily=_value(cfg.get('daily','0'),'每日預算'); monthly=_value(cfg.get('monthly','0'),'每月預算')
        price=cfg.get('prices',{}).get(model,{})
        if not isinstance(price,dict): raise ValueError('模型價格設定格式錯誤')
        inp=_value(price.get('input','0'),'輸入價格'); out=_value(price.get('output','0'),'輸出價格')
        if (daily>0 or monthly>0) and (inp<=0 or out<=0): raise ValueError('此模型尚未設定單價；請至預算與用量設定')
        if tool_kind not in (None,'web_search') or type(tool_calls) is not int or (tool_kind=='web_search' and tool_calls!=1) or (tool_kind is None and tool_calls!=0): raise ValueError('工具預留設定無效')
        tool_price=_value(cfg.get('web_search_price','0'),'每次網路搜尋價格') if tool_kind else D(0)
        if tool_kind and (daily>0 or monthly>0) and tool_price<=0: raise ValueError('請先在預算與用量設定每次網路搜尋價格')
        if daily or monthly:
            unknown=db.execute("SELECT count(*) FROM api_usage WHERE excluded=0 AND status IN ('unknown','unpriced','pending') AND reserved_nano=0 AND substr(created_day,1,7)=?",(month,)).fetchone()[0]
            if unknown: raise ValueError('本月存在未定價或未確認用量，請先至預算與用量檢查')
        # Each input UTF-8 byte plus overhead is a conservative estimate, not an official token count.
        if type(extra_input_tokens) is not int or not 0<=extra_input_tokens<=1000000: raise ValueError('預留輸入 Token 數無效')
        ceiling=len((ai_service.INSTRUCTIONS+ai_service.make_input(question,sources)).encode('utf-8'))+256+extra_input_tokens
        reserved=_usd_nano((D(ceiling)*inp+D(int(max_tokens))*out)/MILLION+tool_price*tool_calls)
        daily_used=db.execute('SELECT COALESCE(SUM(COALESCE(final_nano,reserved_nano)),0) FROM api_usage WHERE excluded=0 AND created_day=?',(day,)).fetchone()[0]
        monthly_used=db.execute('SELECT COALESCE(SUM(COALESCE(final_nano,reserved_nano)),0) FROM api_usage WHERE excluded=0 AND substr(created_day,1,7)=?',(month,)).fetchone()[0]
        if daily and daily_used+reserved>_usd_nano(daily): raise ValueError('此請求的預留費用會超過 APP 每日預算')
        if monthly and monthly_used+reserved>_usd_nano(monthly): raise ValueError('此請求的預留費用會超過 APP 每月預算')
        ident=uuid.uuid4().hex
        db.execute('INSERT INTO api_usage(id,created_day,model,status,reserved_nano,input_price,output_price) VALUES(?,?,?,?,?,?,?)',(ident,day,model,'pending',reserved,str(inp),str(out)))
        if tool_kind: db.execute('INSERT INTO api_usage_tool(id,kind,price_per_call,max_calls) VALUES(?,?,?,?)',(ident,tool_kind,str(tool_price),tool_calls))
        db.commit(); return ident
    except Exception:
        db.rollback(); raise
    finally: db.close()
def finish(path,ident,usage,tool_calls=None):
    db=_db(path)
    try:
        inp=usage.get('input_tokens'); out=usage.get('output_tokens')
        if type(inp) is not int or inp<0 or type(out) is not int or out<0:
            with db: db.execute("UPDATE api_usage SET status='unknown' WHERE id=?",(ident,))
            return 'unknown'
        row=db.execute('SELECT input_price,output_price FROM api_usage WHERE id=?',(ident,)).fetchone()
        if not row: raise ValueError('用量紀錄不存在')
        tool=db.execute('SELECT price_per_call,max_calls FROM api_usage_tool WHERE id=?',(ident,)).fetchone()
        if tool and (type(tool_calls) is not int or not 0<=tool_calls<=tool[1]):
            with db: db.execute("UPDATE api_usage SET status='unknown',input_tokens=?,output_tokens=? WHERE id=?",(inp,out,ident))
            return 'unknown'
        if D(row[0])==0 or D(row[1])==0 or (tool and D(tool[0])==0):
            with db: db.execute("UPDATE api_usage SET status='unpriced',input_tokens=?,output_tokens=? WHERE id=?",(inp,out,ident))
            return 'unpriced'
        total=_usd_nano((D(inp)*D(row[0])+D(out)*D(row[1]))/MILLION+(D(tool[0])*tool_calls if tool else D(0)))
        with db:
            db.execute("UPDATE api_usage SET status='complete',input_tokens=?,output_tokens=?,final_nano=? WHERE id=?",(inp,out,total,ident))
            if tool: db.execute('UPDATE api_usage_tool SET actual_calls=? WHERE id=?',(tool_calls,ident))
        return 'complete'
    finally: db.close()
def uncertain(path,ident):
    db=_db(path)
    try:
        with db: db.execute("UPDATE api_usage SET status='unknown' WHERE id=? AND status='pending'",(ident,))
    finally: db.close()
def recent(path,limit=100):
    db=_db(path)
    try: return db.execute('SELECT created_day,model,status,input_tokens,output_tokens,COALESCE(final_nano,reserved_nano),excluded,id FROM api_usage ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()
    finally: db.close()
def totals(path,now=None):
    now=now or datetime.datetime.now().astimezone(); day=now.date().isoformat(); month=day[:7]
    db=_db(path)
    try:
        day_total=db.execute('SELECT COALESCE(SUM(COALESCE(final_nano,reserved_nano)),0) FROM api_usage WHERE excluded=0 AND created_day=?',(day,)).fetchone()[0]
        month_total=db.execute('SELECT COALESCE(SUM(COALESCE(final_nano,reserved_nano)),0) FROM api_usage WHERE excluded=0 AND substr(created_day,1,7)=?',(month,)).fetchone()[0]
        missing=db.execute("SELECT count(*) FROM api_usage WHERE excluded=0 AND status IN ('unpriced','unknown','pending') AND reserved_nano=0 AND substr(created_day,1,7)=?",(month,)).fetchone()[0]
        return day_total,month_total,missing
    finally: db.close()
def exclude(path,ident):
    db=_db(path)
    try:
        with db: db.execute("UPDATE api_usage SET excluded=1 WHERE id=? AND status IN ('pending','unknown','unpriced')",(ident,))
    finally: db.close()
