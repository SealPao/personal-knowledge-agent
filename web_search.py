"""Optional search snippets for the local assistant; never fetch result pages."""
from html.parser import HTMLParser
import ipaddress
import re
import urllib.parse
import urllib.request

SEARCH_URL='https://lite.duckduckgo.com/lite/'
KIND='網路搜尋摘要（未驗證）'


def safe_public_url(url):
    try:
        p=urllib.parse.urlsplit(url)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or len(url)>2048: return False
        host=p.hostname.lower().rstrip('.')
        if host=='localhost' or host.endswith(('.localhost','.local','.internal')): return False
        try:
            if not ipaddress.ip_address(host).is_global: return False
        except ValueError: pass
        return True
    except (ValueError,TypeError): return False


def _destination(href):
    url=urllib.parse.urljoin('https://duckduckgo.com',href)
    parsed=urllib.parse.urlsplit(url)
    if parsed.hostname in ('duckduckgo.com','www.duckduckgo.com') and parsed.path=='/l/':
        url=urllib.parse.parse_qs(parsed.query).get('uddg',[''])[0]
    return url if safe_public_url(url) else ''

class ResultsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.results=[]; self.mode=None; self.buffer=[]; self.current=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs); classes=attrs.get('class','').split()
        if tag=='a' and 'result-link' in classes:
            self.current={'url':_destination(attrs.get('href','')),'title':'','snippet':''}; self.mode='title'; self.buffer=[]
        elif tag=='td' and 'result-snippet' in classes and self.current:
            self.mode='snippet'; self.buffer=[]
    def handle_data(self,data):
        if self.mode: self.buffer.append(data)
    def handle_endtag(self,tag):
        if self.mode=='title' and tag=='a':
            self.current['title']=' '.join(''.join(self.buffer).split())[:200]; self.mode=None
        elif self.mode=='snippet' and tag=='td':
            self.current['snippet']=' '.join(''.join(self.buffer).split())[:800]; self.mode=None
            if self.current['url'] and self.current['title'] and self.current['snippet']: self.results.append(self.current)
            self.current=None

class _SearchRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        p=urllib.parse.urlsplit(newurl)
        if p.scheme!='https' or p.hostname!='lite.duckduckgo.com': raise RuntimeError('搜尋服務導向非預期的網址，已停止。')
        return super().redirect_request(request,fp,code,msg,headers,newurl)


def search(question,opener=None):
    question=question.strip()
    if not question or len(question)>2000: raise ValueError('網路搜尋問題不可為空或超過 2000 字。')
    url=SEARCH_URL+'?'+urllib.parse.urlencode({'q':question})
    request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'text/html'})
    if opener is None: opener=urllib.request.build_opener(_SearchRedirect()).open
    try:
        with opener(request,timeout=15) as response:
            status=getattr(response,'status',200); data=response.read(1_000_001)
    except (OSError,ValueError) as e: raise RuntimeError('無法連接 DuckDuckGo 網路搜尋，請檢查連線。') from e
    if status!=200 or len(data)>1_000_000: raise RuntimeError('網路搜尋服務未回傳可用結果，可能要求驗證或暫時限制存取。')
    page=data.decode('utf8','replace'); parser=ResultsParser(); parser.feed(page)
    if not parser.results: raise RuntimeError('網路搜尋未回傳可用摘要，可能要求驗證或沒有結果。')
    return parser.results[:3]


def to_sources(results,start=1):
    return [{'id':start+i,'path':r['url'],'url':r['url'],'title':r['title'],'locator':'搜尋結果摘要',
             'excerpt':r['snippet'],'kind':KIND} for i,r in enumerate(results)]
