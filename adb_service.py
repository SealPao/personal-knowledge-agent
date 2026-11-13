"""Explicit, local ADB observation and user-reviewed device notes."""
import os
import re
import shutil
import subprocess
import sys
import hashlib
import io
from pathlib import Path
from PIL import Image
import core

MAX_IMAGE=20*1024*1024
PNG=b'\x89PNG\r\n\x1a\n'

def executable():
    base=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
    bundled=base/'adb'/'adb.exe'
    return str(bundled) if bundled.is_file() else shutil.which('adb')

def _run(args,timeout=25,runner=subprocess.run):
    exe=executable()
    if not exe: raise RuntimeError('找不到 ADB，請確認 APP 的 adb 資料夾完整')
    try: result=runner([exe,*args],capture_output=True,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,subprocess.SubprocessError) as e: raise RuntimeError('ADB 無法執行：'+str(e)) from None
    if result.returncode: raise RuntimeError('ADB 執行失敗，請檢查裝置連線、USB 偵錯與授權')
    return result.stdout

def devices(runner=subprocess.run):
    output=_run(['devices','-l'],runner=runner).decode('utf-8','replace')
    result=[]
    for line in output.splitlines():
        parts=line.split()
        if len(parts)>=2 and re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',parts[0]) and parts[1] in ('device','offline','unauthorized'):
            result.append((parts[0],parts[1],' '.join(parts[2:])))
    return result

def screenshot(serial,runner=subprocess.run):
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',serial): raise ValueError('裝置識別碼無效')
    data=_run(['-s',serial,'exec-out','screencap','-p'],timeout=30,runner=runner)
    if len(data)>MAX_IMAGE or len(data)<32 or not data.startswith(PNG): raise ValueError('裝置沒有回傳有效截圖，或截圖過大')
    return data

def image_info(png):
    with Image.open(io.BytesIO(png)) as im:
        if im.format!='PNG' or im.width*im.height>25_000_000 or min(im.size)<1: raise ValueError('裝置截圖無效或過大')
        rgb=im.convert('RGB'); digest=hashlib.sha256(rgb.tobytes()).hexdigest()
        return im.size,digest

def perform(serial,action,size,expected_digest,runner=subprocess.run):
    """Require fresh screen verification and accept only a restricted input action."""
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}',serial): raise ValueError('裝置識別碼無效')
    actual_size,digest=image_info(screenshot(serial,runner))
    if digest!=expected_digest or actual_size!=tuple(size): raise ValueError('裝置畫面已變更，請重新觀察後確認操作')
    w,h=size; kind=action['type']
    def pos(value,limit):
        if type(value) is not int or not 0<=value<=1000: raise ValueError('操作座標無效')
        return str(round(value*(limit-1)/1000))
    if kind=='tap': cmd=['tap',pos(action.get('x'),w),pos(action.get('y'),h)]
    elif kind=='swipe': cmd=['swipe',pos(action.get('x1'),w),pos(action.get('y1'),h),pos(action.get('x2'),w),pos(action.get('y2'),h),'500']
    elif kind=='back': cmd=['keyevent','4']
    elif kind=='home': cmd=['keyevent','3']
    else: raise ValueError('此版本不支援此裝置操作')
    try: _run(['-s',serial,'shell','input',*cmd],timeout=15,runner=runner)
    except RuntimeError as e: raise RuntimeError('操作結果不明，可能已送出；請檢查閱讀器畫面後再繼續：'+str(e)) from None

def save_session(db_path,title,note,images,reviewed=False,existing=None):
    if not images or len(images)>20: raise ValueError('紀錄必須包含 1 至 20 張畫面')
    for img in images:
        if len(img)>MAX_IMAGE: raise ValueError('截圖過大')
        image_info(img)
    label='使用者校對的 AI 裝置操作紀錄' if reviewed else 'AI 裝置觀察紀錄（尚未人工校對）'
    path=core.save_card(str(db_path),title,'來源：'+label+'；截圖保存在知識庫內。\n\n'+note.strip(),existing=existing)
    db=core.connect(str(db_path))
    try:
        row=db.execute('SELECT id FROM documents WHERE path=?',(path,)).fetchone()
        with db:
            db.execute('UPDATE documents SET detail=? WHERE id=?',(label,row[0]))
            if existing: db.execute('DELETE FROM device_captures WHERE document_id=?',(row[0],))
            db.executemany("INSERT INTO device_captures(document_id,png,created_at) VALUES(?,?,datetime('now','localtime'))",[(row[0],img) for img in images])
    finally: db.close()
    return path

def save_note(db_path,title,note,png):
    if len(png)>MAX_IMAGE or len(png)<32 or not png.startswith(PNG): raise ValueError('截圖資料無效')
    if not note.strip(): raise ValueError('請輸入已確認的操作步驟')
    path=core.save_card(str(db_path),title,'來源：使用者實際操作閱讀器的紀錄；截圖保存在知識庫內。\n\n'+note.strip())
    db=core.connect(str(db_path))
    try:
        doc=db.execute('SELECT id FROM documents WHERE path=?',(path,)).fetchone()
        if not doc: raise RuntimeError('無法找到已建立的紀錄')
        with db: db.execute("INSERT INTO device_captures(document_id,png,created_at) VALUES(?,?,datetime('now','localtime'))",(doc[0],png))
    finally: db.close()
    return path

def read_capture(db_path,path):
    captures=read_captures(db_path,path)
    return captures[-1] if captures else None

def read_captures(db_path,path):
    db=core.connect(str(db_path))
    try:
        rows=db.execute('SELECT c.png FROM device_captures c JOIN documents d ON d.id=c.document_id WHERE d.path=? ORDER BY c.id LIMIT 20',(str(path),)).fetchall()
        return [bytes(row[0]) for row in rows]
    finally: db.close()
