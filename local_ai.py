"""Run the optional bundled model on loopback, without Ollama."""
import atexit
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
MODEL='gemma-4-26B-A4B'
REVISION='d1c082be9cf3c8a514acf63b8761f4b41935842e'
FILES=(('gemma-4-26B_q4_0-it.gguf',14439363584,'3eca3b8f6d7baf218a7dd6bba5fb59a56ee25fe2d567b6f5f589b4f697eca51d'),
       ('gemma-4-26B-it-mmproj.gguf',1194828160,'a359953a076b877db30c31dbbb4c6d93b4a6e017ee5db5784247e4d4c0dd4f3b'))

def download(progress=lambda current,total: None, cancelled=lambda: False, root=None):
    """Explicit download, pinned revision and hashes; never reads user documents."""
    root=Path(root) if root else bundle_dir(); folder=root/'models'; folder.mkdir(parents=True,exist_ok=True)
    total=sum(x[1] for x in FILES); done=0
    needed=sum(size for name,size,_ in FILES if not (folder/name).exists())
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
        part=folder/(name+'.download'); digest=hashlib.sha256(); count=0
        if part.exists(): raise RuntimeError('發現未完成的下載檔，請先檢查並移開。')
        url='https://huggingface.co/google/gemma-4-26B-A4B-it-qat-q4_0-gguf/resolve/'+REVISION+'/'+name
        created=False
        try:
            with urllib.request.urlopen(url,timeout=60) as response,part.open('xb') as output:
                created=True
                while True:
                    if cancelled(): raise RuntimeError('下載已取消。')
                    block=response.read(1024*1024)
                    if not block: break
                    count+=len(block)
                    if count>size: raise RuntimeError('模型下載大小異常。')
                    digest.update(block); output.write(block); progress(done+count,total)
            if count!=size or digest.hexdigest()!=expected: raise RuntimeError('模型檔完整性驗證失敗。')
            os.replace(part,target); done+=size
        finally:
            if created and part.exists(): part.unlink()

def bundle_dir():
    return (Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent)/'local-ai'

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

def start(root=None,timeout=180,gpu=True):
    with _start_lock:
        return _start(root,timeout,gpu)

def _start(root=None,timeout=180,gpu=True):
    global _process,_base,_key
    if running(): return _base,MODEL
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
            if process.poll() is not None: raise RuntimeError('本地模型無法啟動；請確認可用記憶體與顯示卡支援。')
            try:
                request=urllib.request.Request(base+'/models',headers={'Authorization':'Bearer '+key})
                with opener.open(request,timeout=2) as response:
                    if response.status==200:
                        _process=process; _base=base; _key=key; return base,MODEL
            except (OSError,ValueError): pass
            time.sleep(.5)
        raise RuntimeError('本地模型啟動逾時，請確認可用記憶體。')
    except BaseException:
        process.terminate()
        try: process.wait(timeout=8)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        raise
