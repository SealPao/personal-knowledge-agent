"""Choose an independent database; no secrets in the selection file."""
import json
import os
import sqlite3
import sys
from pathlib import Path
import core

def library_parent(base): return Path(base).resolve()/'data'/'libraries'
def external_parent(base): return Path(base).resolve()/'data'/'external-libraries'

def _external_database(base,path):
    original=Path(path).absolute(); p=original.resolve()
    if p.name!='knowledge.sqlite3' or p.parent.parent!=external_parent(base): raise ValueError('外部來源索引位置無效')
    if any(x.is_symlink() or getattr(os.stat(x,follow_symlinks=False),'st_file_attributes',0)&core.REPARSE for x in (original.parent,original)):
        raise ValueError('外部來源索引不能使用連結')
    return p

def _rebase_external(base,path):
    p=_external_database(base,path); db=sqlite3.connect(str(p))
    try:
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('知識庫完整性檢查失敗')
        settings=dict(db.execute("SELECT key,value FROM settings WHERE key IN ('root','external_data_dir')"))
        if 'root' not in settings or 'external_data_dir' not in settings: raise ValueError('外部來源索引缺少位置資訊')
        old=Path(settings['external_data_dir']); root=Path(settings['root'])
        if not old.is_absolute() or not root.is_absolute(): raise ValueError('索引路徑無效')
        if os.path.normcase(str(old))==os.path.normcase(str(p.parent)): return p
        changes=[]
        for ident,path in db.execute('SELECT id,path FROM documents').fetchall():
            original=Path(path); target=None
            for folder in ('cards','imported'):
                try: rel=original.relative_to(old/folder)
                except ValueError: continue
                target=p.parent/folder/rel
                if not core.contained(target,p.parent/folder) or not target.is_file() or target.is_symlink(): raise ValueError('找不到工作副本：'+str(target))
                changes.append((str(target),ident)); break
            if target is None:
                try: original.relative_to(root)
                except ValueError: raise ValueError('索引含有未管理的路徑：'+str(original)) from None
        with db:
            db.executemany('UPDATE documents SET path=? WHERE id=?',changes)
            db.execute("UPDATE settings SET value=? WHERE key='external_data_dir'",(str(p.parent),))
        return p
    finally: db.close()

def _library_database(base, path):
    original=Path(path).absolute(); p=original.resolve(); parent=library_parent(base)
    if p.name!='knowledge.sqlite3' or p.parent.name!='data' or p.parent.parent.parent!=parent:
        raise ValueError('可攜知識庫位置無效')
    if any(x.is_symlink() or getattr(os.stat(x,follow_symlinks=False),'st_file_attributes',0)&core.REPARSE for x in (original.parent.parent,original.parent,original)):
        raise ValueError('可攜知識庫不能使用連結')
    return p

def _rebase_library(base, path):
    """Rebase only an app-owned imported library, never an external source."""
    p=_library_database(base,path); new_root=p.parent.parent/'source'
    if not new_root.is_dir() or new_root.is_symlink() or getattr(os.stat(new_root,follow_symlinks=False),'st_file_attributes',0)&core.REPARSE: raise ValueError('找不到可攜知識庫文件')
    db=sqlite3.connect(str(p))
    try:
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('知識庫完整性檢查失敗')
        settings=dict(db.execute("SELECT key,value FROM settings WHERE key IN ('root','portable_data_dir')"))
        if 'root' not in settings or 'portable_data_dir' not in settings: raise ValueError('知識庫缺少可攜資訊')
        old_root=Path(settings['root']); old_data=Path(settings['portable_data_dir'])
        if not old_root.is_absolute() or not old_data.is_absolute(): raise ValueError('知識庫路徑資訊無效')
        if os.path.normcase(str(old_root))==os.path.normcase(str(new_root)) and os.path.normcase(str(old_data))==os.path.normcase(str(p.parent)): return p
        changes=[]
        for ident,path in db.execute('SELECT id,path FROM documents').fetchall():
            old=Path(path); target=None
            for previous,current in ((old_root,new_root),(old_data/'cards',p.parent/'cards'),(old_data/'imported',p.parent/'imported')):
                try: relative=old.relative_to(previous)
                except ValueError: continue
                target=current/relative; break
            if target is None or not core.contained(target,current): raise ValueError('文件不屬於可攜知識庫：'+str(old))
            if not target.is_file() or target.is_symlink(): raise ValueError('可攜知識庫缺少文件：'+str(target))
            changes.append((str(target),ident))
        with db:
            db.executemany('UPDATE documents SET path=? WHERE id=?',changes)
            db.execute("UPDATE settings SET value=? WHERE key='root'",(str(new_root),))
            db.execute("UPDATE settings SET value=? WHERE key='portable_data_dir'",(str(p.parent),))
        return p
    finally: db.close()

def application_base():
    program=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
    marker=program/'pka-install.json'
    if marker.is_file():
        try:
            data=json.loads(marker.read_text(encoding='utf-8')); path=Path(data['data_home'])
            if not path.is_absolute(): raise ValueError('data_home must be absolute')
            return path.resolve()
        except Exception as e: raise RuntimeError('安裝設定無法讀取：'+str(e)) from e
    return program

def select_path(base): return Path(base)/'data'/'active-database.json'
def default_database(base): return Path(base)/'data'/'knowledge.sqlite3'
def known_libraries(base):
    """Find app-owned indexes without opening or altering source documents."""
    base=Path(base).resolve(); candidates=[('原始索引',default_database(base))]
    for group,kind,tail in (('libraries','可攜副本',('data','knowledge.sqlite3')),('external-libraries','外部來源',('knowledge.sqlite3',))):
        parent=base/'data'/group
        if not parent.is_dir() or parent.is_symlink(): continue
        for child in parent.iterdir():
            if child.is_dir() and not child.is_symlink() and not getattr(os.stat(child,follow_symlinks=False),'st_file_attributes',0)&core.REPARSE:
                candidates.append((kind,child.joinpath(*tail)))
    results=[]
    for kind,path in candidates:
        if not path.is_file() or path.is_symlink(): continue
        try:
            db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
            try:
                row=db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
                if not row: continue
                count=db.execute('SELECT count(*) FROM documents').fetchone()[0]
            finally: db.close()
            root=path.parent.parent/'source' if kind=='可攜副本' else Path(row[0])
            results.append({'kind':kind,'database':path.resolve(),'root':str(root),'count':count,'available':root.is_dir()})
        except (OSError,sqlite3.Error): continue
    return results
def protected_roots(base,active=None):
    result=[]
    for p in (default_database(base),Path(active) if active else None):
        if not p or not p.is_file(): continue
        db=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            row=db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
            if row and row[0] not in result: result.append(row[0])
        finally: db.close()
    for item in known_libraries(base):
        if item['root'] not in result: result.append(item['root'])
    return result
def validate(path,allow_missing_root=False):
    p=Path(path).resolve()
    if not p.is_file() or p.name!='knowledge.sqlite3': raise ValueError('找不到還原的知識庫資料庫')
    db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
    try:
        root=db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
        if not root or not Path(root[0]).is_absolute() or (not allow_missing_root and not Path(root[0]).is_dir()) or core.contained(p,root[0]): raise ValueError('資料庫或來源資料夾位置不安全')
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('資料庫完整性檢查失敗')
    finally: db.close()
    return p
def active_database(base):
    p=select_path(base)
    if not p.exists(): return default_database(base)
    try:
        with p.open('r',encoding='utf-8') as f: data=json.load(f)
        if 'relative_database' in data:
            rel=Path(data['relative_database'])
            if rel.is_absolute() or '..' in rel.parts: raise ValueError('可攜知識庫路徑不安全')
            return validate(_rebase_library(base,Path(base)/rel))
        if 'external_database' in data:
            rel=Path(data['external_database'])
            if rel.is_absolute() or '..' in rel.parts: raise ValueError('外部索引路徑不安全')
            return validate(_rebase_external(base,Path(base)/rel),allow_missing_root=True)
        return validate(data['database'],allow_missing_root=True)
    except Exception as e: raise RuntimeError('已選擇的知識庫無法開啟：'+str(e)) from e
def activate(base,db_path):
    db=Path(db_path).resolve()
    if core.contained(db,library_parent(base)):
        # Inactive imported copies may also have moved with the APP.
        check=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)
        try: marked=check.execute("SELECT 1 FROM settings WHERE key='portable_data_dir'").fetchone()
        finally: check.close()
        if marked: db=_rebase_library(base,db)
    elif core.contained(db,external_parent(base)):
        db=_external_database(base,db)
        check=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)
        try: marked=check.execute("SELECT 1 FROM settings WHERE key='external_data_dir'").fetchone()
        finally: check.close()
        if marked: db=_rebase_external(base,db)
    db=validate(db); p=select_path(base)
    connection=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)
    try: root=connection.execute("SELECT value FROM settings WHERE key='root'").fetchone()[0]
    finally: connection.close()
    if any(core.contained(p,r) or core.contained(db,r) for r in protected_roots(base,db)):
        raise ValueError('APP 設定與資料庫不能寫入來源資料夾')
    selection={'database':str(db)}
    if core.contained(db,library_parent(base)):
        db=_library_database(base,db)
        if Path(root).resolve()!=(db.parent.parent/'source').resolve(): raise ValueError('可攜知識庫來源位置無效')
        connection=sqlite3.connect(str(db))
        try:
            with connection: connection.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('portable_data_dir',?)",(str(db.parent),))
        finally: connection.close()
        selection={'relative_database':str(db.relative_to(Path(base).resolve()))}
    elif core.contained(db,external_parent(base)):
        db=_external_database(base,db)
        connection=sqlite3.connect(str(db))
        try:
            with connection: connection.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('external_data_dir',?)",(str(db.parent),))
        finally: connection.close()
        selection={'external_database':str(db.relative_to(Path(base).resolve()))}
    p.parent.mkdir(parents=True,exist_ok=True)
    temp=p.with_suffix('.tmp'); temp.write_text(json.dumps(selection,ensure_ascii=False),encoding='utf-8'); os.replace(temp,p)
