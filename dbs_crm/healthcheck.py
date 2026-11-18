"""Internal health probe. Never prints credentials or application records."""
import os
import urllib.request
from urllib.parse import urlparse

def main():
    host=urlparse(os.environ['DBS_PUBLIC_ORIGIN']).netloc
    request=urllib.request.Request('http://127.0.0.1:8765/health',headers={
        'Host':host,'X-DBS-Gateway':os.environ['DBS_GATEWAY_TOKEN']})
    with urllib.request.urlopen(request,timeout=4) as response:
        if response.status!=200: raise SystemExit(1)

if __name__=='__main__': main()
