"""Same-origin browser events and bounded public requests behind HTTPS proxy."""
import os
from urllib.parse import urlsplit


class PublicOrigin:
    def __init__(self,app):
        self.application=app
        self.allowed={'localhost','127.0.0.1'}
        for name in ('RENDER_EXTERNAL_HOSTNAME','PUBLIC_HOST'):
            value=os.environ.get(name,'').strip().lower()
            if value:self.allowed.add(value)

    async def __call__(self,scope,receive,send):
        if scope['type'] in ('http','websocket'):
            headers=dict(scope.get('headers',[]))
            host=headers.get(b'host',b'').decode('latin1').lower()
            origin=headers.get(b'origin',b'').decode('latin1')
            try:
                hostname=urlsplit('//'+host).hostname
                origin_ok=not origin or (urlsplit(origin).scheme in ('http','https') and urlsplit(origin).netloc.lower()==host)
                length=int(headers.get(b'content-length',b'0'))
            except (ValueError,UnicodeError):hostname=None;origin_ok=False;length=0
            allowed=hostname in self.allowed and origin_ok and 0<=length<=1024*1024
            if scope['type']=='websocket' and not origin:allowed=False
            if scope.get('method') in ('POST','PUT','DELETE','PATCH') and not origin:allowed=False
            if not allowed:
                if scope['type']=='websocket':await send({'type':'websocket.close','code':1008})
                else:
                    await send({'type':'http.response.start','status':403,'headers':[]})
                    await send({'type':'http.response.body','body':b'Request not allowed'})
                return
        await self.application(scope,receive,send)
