"""Public source reading and optional screenshots, using a fresh browser context."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin
from .sources import _public_stream

class TextPage(HTMLParser):
    def __init__(self): super().__init__(); self.hidden=0; self.parts=[]; self.title=''; self.in_title=False
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'): self.hidden+=1
        if tag=='title': self.in_title=True
        if tag in ('p','div','section','h1','h2','h3','li','br'): self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'): self.hidden=max(0,self.hidden-1)
        if tag=='title': self.in_title=False
    def handle_data(self,data):
        if self.in_title: self.title+=data
        if not self.hidden: self.parts.append(data)

def read_page(url,output_dir,screenshot=False):
    target=Path(output_dir); target.mkdir(parents=True,exist_ok=True)
    for _ in range(6):
        with _public_stream(url, timeout=30) as response:
            if response.is_redirect: url=urljoin(url,response.headers['location']); continue
            response.raise_for_status(); data=b''
            for chunk in response.iter_bytes():
                data+=chunk
                if len(data)>5_000_000: raise ValueError('Public page exceeds 5 MB; import the source PDF instead')
            try:
                encoding = response.encoding or 'utf-8'
            except LookupError:
                encoding = 'utf-8'
            try:
                html = data.decode(encoding, errors='replace')
            except LookupError:
                html = data.decode('utf-8', errors='replace')
            parser=TextPage(); parser.feed(html); text='\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())
            (target/'page.txt').write_text(text)
            chunk_size=20000
            passages=[{'id':f'page-{index+1}','page':None,'section':f'Page text, part {index+1}',
                       'text':text[start:start+chunk_size],'source_url':url}
                      for index,start in enumerate(range(0,len(text),chunk_size))]
            result={'url':url,'title':parser.title.strip(),'text':text[:40000],
                    'text_length':len(text),'text_truncated':len(text)>40000,
                    'passages':passages,'text_path':str(target/'page.txt'),
                    'reading_scope':'public_page_text','trusted_instructions':False}
            if screenshot:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(channel='chrome',headless=True)
                    context=browser.new_context(viewport={'width':1440,'height':1000},
                                                java_script_enabled=False, service_workers='block')
                    resource_bytes=0
                    def restrict(route):
                        nonlocal resource_bytes
                        request=route.request
                        if request.url.startswith(('data:','blob:')):
                            route.continue_(); return
                        if request.method != 'GET':
                            route.abort(); return
                        try:
                            # The main document has already passed the pinned
                            # transport. Serve the same bytes, not a second DNS
                            # resolution with possibly different content.
                            if request.is_navigation_request() and request.url == url:
                                route.fulfill(status=200, headers={'content-type':response.headers.get('content-type','text/html')}, body=data)
                                return
                            with _public_stream(request.url, timeout=12,
                                                headers={'User-Agent':'FOREST-research/1.0',
                                                         'Accept':request.headers.get('accept','*/*')}) as resource:
                                payload=bytearray()
                                for part in resource.iter_bytes():
                                    payload.extend(part)
                                    if len(payload)>5_000_000 or resource_bytes+len(payload)>20_000_000:
                                        raise ValueError('Screenshot resource budget exceeded')
                                resource_bytes+=len(payload)
                                # httpx decodes compressed bodies; never forward
                                # their original compression or length metadata.
                                headers={k:v for k,v in resource.headers.items()
                                         if k.lower() not in ('content-encoding','content-length',
                                                              'transfer-encoding','connection')}
                                route.fulfill(status=resource.status_code, headers=headers,
                                              body=bytes(payload))
                        except Exception:
                            route.abort()
                    context.route('**/*',restrict); page=context.new_page(); page.goto(url,wait_until='domcontentloaded',timeout=30000); page.screenshot(path=str(target/'page.png'),full_page=True); browser.close()
                result['screenshot_path']=str(target/'page.png')
            return result
    raise ValueError('Too many public-page redirects')
