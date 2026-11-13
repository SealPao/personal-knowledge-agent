"""Per-user installation without placing or deleting the knowledge base in the program folder."""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
import core,workspace

def _copy_tree(source,dest):
    if not source.exists(): return
    for parent,dirs,files in os.walk(source,followlinks=False):
        for name in dirs:
            p=Path(parent)/name; st=os.stat(p,follow_symlinks=False)
            if p.is_symlink() or getattr(st,'st_file_attributes',0)&core.REPARSE: raise ValueError('工作資料包含連結，無法安全移轉')
        relative=Path(parent).relative_to(source); out=dest/relative; out.mkdir(parents=True,exist_ok=True)
        for name in files:
            p=Path(parent)/name; st=os.stat(p,follow_symlinks=False)
            if p.is_symlink() or getattr(st,'st_file_attributes',0)&(core.REPARSE|core.PLACEHOLDER): raise ValueError('工作資料包含連結或雲端佔位檔')
            shutil.copy2(p,out/name)
            after=os.stat(p,follow_symlinks=False)
            if (st.st_size,st.st_mtime_ns)!=(after.st_size,after.st_mtime_ns): raise ValueError('移轉期間工作文件變更')

def _mkdir_guarded(root,*parts):
    current=root
    for part in parts:
        current=current/part; current.mkdir(exist_ok=True)
        if current.is_symlink() or getattr(os.stat(current,follow_symlinks=False),'st_file_attributes',0)&core.REPARSE:
            raise ValueError('資料目錄不可包含連結')
    return current

def install(executable,program_dir,data_home,model_path=None,prototype=None,protection_base=None):
    executable=Path(executable).resolve(); program=Path(program_dir).resolve(); home=Path(data_home).resolve(); target=home/'data'
    if not executable.is_file(): raise ValueError('安裝來源缺少應用程式')
    if core.contained(home,program) or core.contained(program,home): raise ValueError('程式與資料位置必須分開')
    src_db=None; source_base=None; roots=[]
    if protection_base:
        roots.extend(workspace.protected_roots(protection_base))
    if prototype:
        source_base=Path(prototype).resolve(); src_db=workspace.validate(workspace.active_database(source_base))
        roots.extend(workspace.protected_roots(source_base,src_db))
    if any(core.contained(program,r) or core.contained(home,r) for r in roots): raise ValueError('安裝位置不可位於原始知識庫')
    existing=target.exists()
    if existing and (target.is_symlink() or getattr(os.stat(target,follow_symlinks=False),'st_file_attributes',0)&core.REPARSE): raise ValueError('資料目錄不可為連結')
    if existing and not (target/'knowledge.sqlite3').is_file() and any(target.iterdir()):
        raise ValueError('資料目錄已有其他內容，為避免覆蓋已停止安裝')
    if existing and (target/'knowledge.sqlite3').is_file():
        current=workspace.validate(target/'knowledge.sqlite3')
        roots.extend(workspace.protected_roots(home,current))
        if any(core.contained(program,r) or core.contained(home,r) for r in roots): raise ValueError('安裝位置不可位於原始知識庫')
    program.mkdir(parents=True,exist_ok=True); home.mkdir(parents=True,exist_ok=True)
    if any(p.is_symlink() or getattr(os.stat(p,follow_symlinks=False),'st_file_attributes',0)&core.REPARSE for p in (program,home)): raise ValueError('安裝資料夾不可為連結')
    marker=program/'pka-install.json'
    if (program/'PersonalKnowledgeAgent.exe').exists() and not marker.exists():
        raise ValueError('目的資料夾已含同名程式，且不是此安裝程式建立；為避免覆蓋已停止安裝')
    if marker.exists():
        old=json.loads(marker.read_text(encoding='utf-8'))
        if Path(old.get('data_home','')).resolve()!=home: raise ValueError('現有安裝指向不同資料夾，請選擇原本的位置')
    migrated=False
    if src_db and not existing:
        with tempfile.TemporaryDirectory(prefix='pka-migrate-',dir=home) as td:
            staged=Path(td)/'data'; staged.mkdir(); db_out=staged/'knowledge.sqlite3'
            source=sqlite3.connect(src_db.as_uri()+'?mode=ro',uri=True); snapshot=sqlite3.connect(str(db_out))
            try: source.backup(snapshot)
            finally: source.close(); snapshot.close()
            for name in ('cards','imported','ocr'): _copy_tree(src_db.parent/name,staged/name)
            for name in ('ai-settings.json','preferences.json'):
                p=src_db.parent/name
                if p.is_file() and not p.is_symlink(): shutil.copy2(p,staged/name)
            conn=sqlite3.connect(str(db_out))
            try:
                rows=conn.execute('SELECT id,path,sha256,status FROM documents').fetchall(); changes=[]
                root=conn.execute("SELECT value FROM settings WHERE key='root'").fetchone()[0]
                for ident,path,sha,status in rows:
                    p=Path(path)
                    for name in ('cards','imported'):
                        src=src_db.parent/name
                        if core.contained(p,src):
                            relative=p.relative_to(src); staged_file=staged/name/relative
                            if not staged_file.is_file(): raise ValueError('移轉副本缺少檔案：'+str(p))
                            if sha and status=='indexed':
                                h=hashlib.sha256()
                                with staged_file.open('rb') as f:
                                    for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
                                if h.hexdigest()!=sha: raise ValueError('工作文件與索引不一致，請先重新掃描')
                            st=staged_file.stat(); changes.append((str(target/name/relative),st.st_size,st.st_mtime_ns,ident)); break
                    else:
                        if not core.contained(p,root): raise ValueError('資料庫中有未管理的外部路徑')
                with conn: conn.executemany('UPDATE documents SET path=?,size=?,mtime_ns=? WHERE id=?',changes)
                if conn.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('移轉的資料庫未通過檢查')
            finally: conn.close()
            os.rename(staged,target); migrated=True
    elif not existing: target.mkdir()
    if model_path:
        model=Path(model_path)
        if model.is_file():
            dest=_mkdir_guarded(target,'ocr','tessdata')/'chi_sim.traineddata'
            if not dest.exists(): shutil.copyfile(model,dest)
            license_file=model.parent/'LICENSE.txt'; license_dest=dest.parent/'LICENSE.txt'
            if license_file.is_file() and not license_dest.exists(): shutil.copyfile(license_file,license_dest)
    app=program/'PersonalKnowledgeAgent.exe'; stage=program/('PersonalKnowledgeAgent.'+uuid.uuid4().hex+'.tmp')
    try:
        shutil.copyfile(executable,stage)
        if app.exists(): shutil.copy2(app,program/('PersonalKnowledgeAgent.backup-'+uuid.uuid4().hex[:8]+'.exe'))
        os.replace(stage,app)
        temp=program/('pka-install.'+uuid.uuid4().hex+'.tmp'); temp.write_text(json.dumps({'data_home':str(home)},ensure_ascii=False),encoding='utf-8'); os.replace(temp,marker)
    finally:
        if stage.exists(): stage.unlink()
    return {'program':str(app),'data':str(target),'migrated':migrated,'existing':existing}
