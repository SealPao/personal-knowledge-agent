"""Run the optional bundled model on loopback, without Ollama."""
import atexit
import contextlib
import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

_process=None
_base=None
_key=None
_start_lock=threading.Lock()
_download_lock=threading.Lock()
MODEL='gemma-4-26B-A4B'
class StartCancelled(RuntimeError): pass
REVISION='d1c082be9cf3c8a514acf63b8761f4b41935842e'
FILES=(('gemma-4-26B_q4_0-it.gguf',14439363584,'3eca3b8f6d7baf218a7dd6bba5fb59a56ee25fe2d567b6f5f589b4f697eca51d'),
       ('gemma-4-26B-it-mmproj.gguf',1194828160,'a359953a076b877db30c31dbbb4c6d93b4a6e017ee5db5784247e4d4c0dd4f3b'))

@contextlib.contextmanager
def _download_file_lock(root):
    folder=Path(root)/'models'; folder.mkdir(parents=True,exist_ok=True)
    with (folder/'.download.lock').open('a+b') as lockfile:
        lockfile.seek(0)
        if os.name=='nt':
            import msvcrt
            try: msvcrt.locking(lockfile.fileno(),msvcrt.LK_NBLCK,1)
            except OSError: raise RuntimeError('另一個 APP 正在下載模型。') from None
            try: yield
            finally: lockfile.seek(0); msvcrt.locking(lockfile.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            try: fcntl.flock(lockfile.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError: raise RuntimeError('另一個 APP 正在下載模型。') from None
            try: yield
            finally: fcntl.flock(lockfile.fileno(),fcntl.LOCK_UN)

def download(progress=lambda current,total: None, cancelled=lambda: False, root=None):
    """Explicit, resumable download; verify the complete file before use."""
    if not _download_lock.acquire(blocking=False): raise RuntimeError('已有模型下載正在進行。')
    try:
        root=Path(root) if root else bundle_dir()
        with _download_file_lock(root): return _download(progress,cancelled,root)
    finally: _download_lock.release()

def _download(progress,cancelled,root):
    root=Path(root) if root else bundle_dir(); folder=root/'models'; folder.mkdir(parents=True,exist_ok=True)
    total=sum(x[1] for x in FILES); done=0
    needed=0
    for name,size,_ in FILES:
        target=folder/name; part=folder/(name+'.download')
        if not target.exists(): needed+=size-min(size,part.stat().st_size if part.exists() else 0)
    if shutil.disk_usage(folder).free < needed + 512*1024*1024: raise RuntimeError('磁碟空間不足，請釋出空間後重試。')
    for name,size,expected in FILES:
        target=folder/name
        if target.exists():
            if target.stat().st_size!=size: raise RuntimeError('現有模型檔大小不符，為避免覆寫請先移開該檔案。')
            digest=hashlib.sha256()
            with target.open('rb') as existing:
                for block in iter(lambda:existing.read(1024*1024),b''):
                    if cancelled(): raise RuntimeError('下載已取消。')
                    digest.update(block)
            if digest.hexdigest()!=expected: raise RuntimeError('現有模型檔驗證失敗，為避免覆寫請先移開該檔案。')
            done+=size; progress(done,total); continue
        part=folder/(name+'.download'); digest=hashlib.sha256(); count=part.stat().st_size if part.exists() else 0
        if count>size: raise RuntimeError('未完成的模型檔大小異常，請檢查並移開該檔案。')
        url='https://huggingface.co/google/gemma-4-26B-A4B-it-qat-q4_0-gguf/resolve/'+REVISION+'/'+name
        if count:
            with part.open('rb') as existing:
                for block in iter(lambda:existing.read(1024*1024),b''):
                    if cancelled(): raise RuntimeError('下載已取消，已保留進度。')
                    digest.update(block)
        progress(done+count,total)
        if count<size:
            request=urllib.request.Request(url,headers={'Range':f'bytes={count}-'} if count else {})
            with urllib.request.urlopen(request,timeout=60) as response:
                if count and response.status==206:
                    expected_range=f'bytes {count}-{size-1}/{size}'
                    if response.headers.get('Content-Range')!=expected_range: raise RuntimeError('下載伺服器的續傳回應異常，已保留進度。')
                elif count and response.status==200:
                    count=0; digest=hashlib.sha256()
                elif count:
                    raise RuntimeError('下載伺服器不支援續傳，已保留進度。')
                with part.open('ab' if count else 'wb') as output:
                    if cancelled(): raise RuntimeError('下載已取消，已保留進度。')
                    while True:
                        if cancelled(): raise RuntimeError('下載已取消，已保留進度。')
                        block=response.read(1024*1024)
                        if not block: break
                        count+=len(block)
                        if count>size: raise RuntimeError('模型下載大小異常。')
                        digest.update(block); output.write(block); progress(done+count,total)
        if count!=size: raise RuntimeError('下載尚未完成；下次可繼續。')
        if digest.hexdigest()!=expected:
            part.unlink(); raise RuntimeError('模型檔完整性驗證失敗，請重新下載。')
        os.replace(part,target); done+=size

def bundle_dir():
    return (Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent)/'local-ai'

def model_status(root=None):
    root=Path(root) if root else bundle_dir(); total=sum(size for _,size,_ in FILES); present=0; ready=True
    for name,size,_ in FILES:
        target=root/'models'/name; part=root/'models'/(name+'.download')
        if target.is_file() and target.stat().st_size==size: present+=size
        else:
            ready=False
            if part.is_file(): present+=min(size,part.stat().st_size)
    return {'ready':ready,'present':present,'total':total,'runtime':(root/'runtime'/'llama-server.exe').is_file()}

def running():
    return _process is not None and _process.poll() is None

def headers_for(url):
    if running() and _base and (url==_base or url.startswith(_base+'/')):
        return {'Authorization':'Bearer '+_key}
    return {}

def stop():
    global _process,_base,_key
    process=_process; _process=None; _base=None; _key=None
    if process and process.poll() is None:
        process.terminate()
        try: process.wait(timeout=8)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)

atexit.register(stop)

def start(root=None,timeout=180,gpu=True,cancelled=lambda: False):
    with _start_lock:
        return _start(root,timeout,gpu,cancelled)

def _start(root=None,timeout=180,gpu=True,cancelled=lambda: False):
    global _process,_base,_key
    if running(): return _base,MODEL
    if cancelled(): raise StartCancelled('已取消啟動本地 AI。')
    root=Path(root) if root else bundle_dir()
    exe=root/'runtime'/'llama-server.exe'; model=root/'models'/'gemma-4-26B_q4_0-it.gguf'; vision=root/'models'/'gemma-4-26B-it-mmproj.gguf'
    if not all(p.is_file() for p in (exe,model,vision)): raise RuntimeError('找不到本地模型或執行程式。請先下載完整的可選模型。')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    key=secrets.token_urlsafe(32); base=f'http://127.0.0.1:{port}/v1'
    cmd=[str(exe),'-m',str(model),'--mmproj',str(vision),'--host','127.0.0.1','--port',str(port),'--api-key',key,'--alias',MODEL,'--ctx-size','8192','--parallel','1','--n-gpu-layers','99' if gpu else '0','--reasoning','off','--no-webui']
    process=subprocess.Popen(cmd,cwd=str(exe.parent),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if cancelled(): raise StartCancelled('已取消啟動本地 AI。')
            if process.poll() is not None: raise RuntimeError('本地模型無法啟動；請確認可用記憶體與顯示卡支援。')
            try:
                request=urllib.request.Request(base+'/models',headers={'Authorization':'Bearer '+key})
                with opener.open(request,timeout=2) as response:
                    if response.status==200:
                        if cancelled(): raise StartCancelled('已取消啟動本地 AI。')
                        _process=process; _base=base; _key=key; return base,MODEL
            except (OSError,ValueError): pass
            time.sleep(.5)
        raise RuntimeError('本地模型啟動逾時，請確認可用記憶體。')
    except BaseException:
        process.terminate()
        try: process.wait(timeout=8)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        raise
