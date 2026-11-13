"""Single-file backup and isolated restore; never writes into the source tree."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
import core
import import_history

FORMAT='pka-zip'; VERSION=1; MAX_FILES=10000; MAX_TOTAL=50*1024**3

def _check_regular(path):
    st=os.stat(path,follow_symlinks=False); attrs=getattr(st,'st_file_attributes',0)
    if not stat.S_ISREG(st.st_mode) or attrs & (core.PLACEHOLDER|core.REPARSE):
        raise ValueError('遇到雲端佔位檔、連結或非一般檔案：'+str(path))
    return st

def _relative(text):
    if not isinstance(text,str) or not text or '\\' in text or ':' in text or '\x00' in text: raise ValueError('封裝內路徑不安全')
    p=PurePosixPath(text)
    if p.is_absolute() or any(x in ('','.', '..') or x.endswith((' ','.')) for x in p.parts): raise ValueError('封裝內路徑不安全')
    if any(re.match(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)',x,re.I) for x in p.parts): raise ValueError('封裝內包含 Windows 保留名稱')
    return p

def _source_inventory(root):
    files=set(); directories=[]
    def onerror(error): raise ValueError('無法列舉來源資料夾：'+str(error))
    for parent,dirs,names in os.walk(root,followlinks=False,onerror=onerror):
        for name in dirs:
            p=Path(parent)/name; st=os.stat(p,follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode) or getattr(st,'st_file_attributes',0)&core.REPARSE: raise ValueError('來源含連結資料夾，無法完整備份：'+str(p))
            directories.append(p.relative_to(root).as_posix())
        for name in names:
            p=Path(parent)/name; _check_regular(p); files.add(os.path.normcase(os.path.abspath(p)))
    return files,directories

def create_package(db_path,destination,progress=lambda x:None,cancelled=lambda:False):
    with import_history.operation_lock(db_path):
        return _create_package(db_path,destination,progress,cancelled)

def _create_package(db_path,destination,progress=lambda x:None,cancelled=lambda:False):
    db_path=Path(db_path).resolve(); destination=Path(destination).resolve()
    if destination.suffix.lower()!='.pka': raise ValueError('備份檔名必須以 .pka 結尾')
    if destination.exists(): raise ValueError('備份檔已存在，請選擇新檔名')
    if not destination.parent.is_dir(): raise ValueError('備份目的資料夾不存在')
    if not db_path.is_file(): raise ValueError('找不到知識庫資料庫')
    source=sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True)
    try:
        rootrow=source.execute("SELECT value FROM settings WHERE key='root'").fetchone()
        if not rootrow: raise ValueError('尚未設定來源資料夾')
        root=Path(rootrow[0]).resolve()
        if core.contained(destination,root): raise ValueError('備份不能寫在原始資料夾內')
        with tempfile.TemporaryDirectory(prefix='pka-export-',dir=destination.parent) as td:
            snapshot=Path(td)/'database.sqlite3'; output=Path(td)/'backup.pka'
            snap=sqlite3.connect(str(snapshot))
            try: source.backup(snap)
            finally: snap.close()
            snap=sqlite3.connect(str(snapshot))
            try:
                if snap.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('資料庫完整性檢查失敗')
                staging=snap.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='import_staging'").fetchone()
                if staging and snap.execute('SELECT 1 FROM import_staging LIMIT 1').fetchone():
                    raise ValueError('有中斷的匯入暫存；請先復原或清理，再建立完整備份。')
                rows=snap.execute('SELECT id,path,sha256,status FROM documents ORDER BY id').fetchall()
                schema_version=snap.execute('PRAGMA user_version').fetchone()[0]
            finally: snap.close()
            if len(rows)>MAX_FILES: raise ValueError('檔案數超過封裝上限')
            actual,dirs=_source_inventory(root); listed=set(); entries=[]; total=0
            with zipfile.ZipFile(output,'w',allowZip64=True) as z:
                for index,(docid,path,expected,status) in enumerate(rows,1):
                    if cancelled(): raise ValueError('已取消備份')
                    p=Path(path)
                    if core.contained(p,root): category='source'; rel=p.relative_to(root).as_posix(); listed.add(os.path.normcase(os.path.abspath(p)))
                    elif core.contained(p,db_path.parent/'cards'): category='cards'; rel=p.relative_to(db_path.parent/'cards').as_posix()
                    elif core.contained(p,db_path.parent/'imported'): category='imported'; rel=p.relative_to(db_path.parent/'imported').as_posix()
                    else: raise ValueError('文件位於未管理的位置，無法完整備份：'+str(p))
                    _relative(rel); before=_check_regular(p); total+=before.st_size
                    if total>MAX_TOTAL: raise ValueError('檔案總容量超過 50 GiB 限制')
                    obj=f'objects/{docid}'; digest=hashlib.sha256(); written=0
                    with open(p,'rb') as f,z.open(obj,'w',force_zip64=True) as out:
                        while True:
                            if cancelled(): raise ValueError('已取消備份')
                            block=f.read(1024*1024)
                            if not block: break
                            digest.update(block); written+=len(block); out.write(block)
                    after=_check_regular(p); sha=digest.hexdigest()
                    if written!=before.st_size or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns): raise ValueError('備份期間文件改變：'+str(p))
                    if status=='indexed' and expected and sha!=expected: raise ValueError('文件內容與索引版本不同，請先重新掃描：'+str(p))
                    entries.append({'id':docid,'category':category,'path':rel,'bytes':written,'sha256':sha})
                    progress(f'已備份 {index} / {len(rows)}')
                if listed!=actual: raise ValueError('來源檔案清單與索引不同，請先重新掃描')
                h=hashlib.sha256()
                with open(snapshot,'rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
                z.write(snapshot,'database.sqlite3')
                manifest={'format':FORMAT,'version':VERSION,'database_schema_version':schema_version,'database_sha256':h.hexdigest(),'documents':entries,'source_directories':dirs}
                z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
            # Publish without overwriting a file created after validation.
            if os.name=='nt': os.rename(output,destination)
            else: os.link(output,destination)
        return len(entries),total
    finally: source.close()

def restore_package(package_path,parent,source_root=None,progress=lambda x:None,cancelled=lambda:False):
    parent=Path(parent).resolve()
    if not parent.is_dir(): raise ValueError('還原目的資料夾不存在')
    final=parent/('PKA-restored-'+uuid.uuid4().hex[:12]); stage=parent/('.pka-staging-'+uuid.uuid4().hex)
    if source_root and (core.contained(stage,source_root) or core.contained(final,source_root)): raise ValueError('還原不能寫進原始資料夾')
    stage.mkdir()
    try:
        with zipfile.ZipFile(package_path,'r') as z:
            infos=z.infolist(); names=[x.filename for x in infos]
            if len(infos)>MAX_FILES+2 or len(names)!=len(set(names)): raise ValueError('封裝檔案數過多或有重複項目')
            manifest_info=z.getinfo('manifest.json'); db_info=z.getinfo('database.sqlite3')
            if manifest_info.file_size>16*1024*1024 or db_info.file_size>4*1024**3: raise ValueError('封裝中繼資料過大')
            manifest=json.loads(z.read(manifest_info).decode('utf-8'))
            if manifest.get('format')!=FORMAT or manifest.get('version')!=VERSION: raise ValueError('不支援此封裝版本')
            entries=manifest.get('documents'); dirs=manifest.get('source_directories')
            if not isinstance(entries,list) or not isinstance(dirs,list) or len(entries)>MAX_FILES or len(dirs)>MAX_FILES: raise ValueError('封裝清單格式錯誤')
            expected={'manifest.json','database.sqlite3'}; targets=set(); ids=set(); total=0
            for item in entries:
                if not isinstance(item,dict) or type(item.get('id')) is not int or item['id']<1: raise ValueError('文件 ID 格式錯誤')
                ident=item['id']; category=item.get('category'); rel=_relative(item.get('path'))
                if category not in ('source','cards','imported') or ident in ids: raise ValueError('封裝文件種類或 ID 錯誤')
                key=(category,str(rel).casefold())
                if key in targets: raise ValueError('封裝含衝突路徑')
                ids.add(ident); targets.add(key); obj=f'objects/{ident}'; expected.add(obj)
                info=z.getinfo(obj); size=item.get('bytes')
                if type(size) is not int or size<0 or size!=info.file_size: raise ValueError('封裝檔案大小不符')
                total+=size
                if total>MAX_TOTAL: raise ValueError('封裝容量超過限制')
                if not re.fullmatch(r'[0-9a-f]{64}',str(item.get('sha256',''))): raise ValueError('封裝雜湊格式錯誤')
            if set(names)!=expected: raise ValueError('封裝包含未宣告或遺漏的項目')
            source=stage/'source'; data=stage/'data'; source.mkdir(); data.mkdir()
            for d in dirs:
                relative=_relative(d); dest=source.joinpath(*relative.parts)
                if not core.contained(dest,source): raise ValueError('資料夾路徑越界')
                dest.mkdir(parents=True,exist_ok=True)
            dbdest=data/'knowledge.sqlite3'; dbhash=hashlib.sha256()
            with z.open('database.sqlite3') as src,open(dbdest,'xb') as dst:
                for block in iter(lambda:src.read(1024*1024),b''): dbhash.update(block); dst.write(block)
            if dbhash.hexdigest()!=manifest.get('database_sha256'): raise ValueError('資料庫雜湊驗證失敗')
            mappings=[]
            for index,item in enumerate(entries,1):
                if cancelled(): raise ValueError('已取消還原')
                rel=_relative(item['path']); cat=item['category']; base=source if cat=='source' else data/cat
                target=base.joinpath(*rel.parts)
                if not core.contained(target,base): raise ValueError('還原路徑越界')
                target.parent.mkdir(parents=True,exist_ok=True); digest=hashlib.sha256()
                with z.open(f'objects/{item["id"]}') as src,open(target,'xb') as dst:
                    for block in iter(lambda:src.read(1024*1024),b''): digest.update(block); dst.write(block)
                if digest.hexdigest()!=item['sha256']: raise ValueError('文件雜湊驗證失敗')
                finalbase=final/'source' if cat=='source' else final/'data'/cat
                mappings.append((str(finalbase.joinpath(*rel.parts)),target.stat().st_size,target.stat().st_mtime_ns,item['sha256'],item['id']))
                progress(f'已驗證 {index} / {len(entries)}')
        db=sqlite3.connect(str(dbdest))
        try:
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('還原資料庫完整性檢查失敗')
            if type(manifest.get('database_schema_version')) is not int or db.execute('PRAGMA user_version').fetchone()[0]!=manifest['database_schema_version']: raise ValueError('資料庫版本與封裝不符')
            rows=db.execute('SELECT id FROM documents').fetchall()
            if {x[0] for x in rows}!=ids: raise ValueError('資料庫與文件清單不一致')
            with db:
                db.executemany('UPDATE documents SET path=?,size=?,mtime_ns=?,sha256=? WHERE id=?',mappings)
                db.execute("UPDATE settings SET value=? WHERE key='root'",(str(final/'source'),))
        finally: db.close()
        os.rename(stage,final)
        return final,len(entries),total
    except Exception:
        if stage.exists() and not stage.is_symlink() and stage.resolve().parent==parent: shutil.rmtree(stage)
        raise
