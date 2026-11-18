"""Local pilot or private service behind the authenticated HTTPS gateway."""
import argparse
import csv
import io
import json
import os
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from . import store
from .collector import Collector

ROOT=Path(__file__).resolve().parent.parent
TOKEN=secrets.token_urlsafe(32)

def safe_csv(value):
    text=str(value or '')
    return "'"+text if text.lstrip().startswith(('=','+','-','@')) else text

def create_server(port=8765,data_dir=None,host='127.0.0.1',public_origin=None,gateway_token=None):
    public_origin=public_origin or os.environ.get('DBS_PUBLIC_ORIGIN','')
    gateway_token=gateway_token or os.environ.get('DBS_GATEWAY_TOKEN','')
    if public_origin:
        parsed=urlparse(public_origin)
        if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('DBS_PUBLIC_ORIGIN deve ser uma origem HTTPS, sem caminho.')
        if len(gateway_token)<32: raise ValueError('Configure um segredo de gateway com pelo menos 32 caracteres.')
    elif host not in ('127.0.0.1','localhost'):
        raise ValueError('Acesso externo exige HTTPS e gateway autenticado configurados.')
    data_dir=Path(data_dir or os.environ.get('DBS_DATA_DIR') or ROOT/'data'); data_dir.mkdir(parents=True,exist_ok=True)
    live=data_dir/'crm.sqlite3'; demo=data_dir/'demo.sqlite3'
    store.init(live); store.seed_demo(demo)
    # Reconcile runs interrupted by an application restart.
    with store.connect(live) as db:
        db.execute("UPDATE runs SET state='interrompido',ended=?,message='Aplicação encerrada antes de concluir; execute novamente.' WHERE state='executando'",(store.now(),))
    collector=Collector(live,data_dir)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def respond(self,data,status=200,content_type='application/json; charset=utf-8',extra=None):
            body=json.dumps(data,ensure_ascii=False).encode() if not isinstance(data,bytes) else data
            self.send_response(status); self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for k,v in (extra or {}).items(): self.send_header(k,v)
            self.end_headers(); self.wfile.write(body)
        def handle_request(self,write=False):
            try:
                host=self.headers.get('Host','')
                allowed_hosts=[urlparse(public_origin).netloc] if public_origin else [f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}']
                if public_origin and not secrets.compare_digest(self.headers.get('X-DBS-Gateway',''),gateway_token):
                    return self.respond({'error':'Acesso permitido somente pelo gateway autenticado.'},403)
                if host not in allowed_hosts:
                    return self.respond({'error':'Host não autorizado.'},403)
                if self.headers.get('Sec-Fetch-Site')=='cross-site':
                    return self.respond({'error':'Origem não autorizada.'},403)
                origin=self.headers.get('Origin')
                allowed_origins=[public_origin] if public_origin else [f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}']
                if origin and origin not in allowed_origins:
                    return self.respond({'error':'Origem não autorizada.'},403)
                url=urlparse(self.path); query=parse_qs(url.query)
                path=demo if query.get('mode',[''])[0]=='demo' else live
                if write:
                    if self.headers.get('X-DBS-Token')!=TOKEN:
                        return self.respond({'error':'Recarregue a página para continuar.'},403)
                    size=int(self.headers.get('Content-Length','0'))
                    if size>16000 or size<0: return self.respond({'error':'Solicitação muito grande.'},413)
                    payload=json.loads(self.rfile.read(size) or '{}')
                    if not isinstance(payload,dict): raise ValueError('Objeto esperado.')
                else: payload={}
                endpoint=url.path
                if endpoint=='/health' and not write: return self.respond({'ok':True})
                if endpoint=='/api/state' and not write:
                    companies=store.list_companies(path,query.get('q',[''])[0],query.get('stage',[''])[0],query.get('activity',[''])[0],query.get('prospects',[''])[0]=='1',city=query.get('city',[''])[0],uf=query.get('uf',[''])[0],cfg=store.settings(live))
                    return self.respond({'companies':companies,'settings':store.settings(live),'stages':store.STAGES,'collector':collector.status(),'token':TOKEN,'today':store.today().isoformat(),'hosted':bool(public_origin),'mode':'demo' if path==demo else 'live'})
                if endpoint.startswith('/api/companies/'):
                    tail=endpoint[len('/api/companies/'):].split('/')
                    cnpj=unquote(tail[0])
                    if write:
                        if len(tail)==2 and tail[1]=='notes': store.add_note(path,cnpj,payload.get('text'))
                        elif len(tail)==1: store.update(path,cnpj,payload)
                        else: return self.respond({'error':'Rota desconhecida.'},404)
                    return self.respond(store.detail(path,cnpj))
                if endpoint=='/api/settings' and write:
                    if path==demo: raise ValueError('Altere a coleta na base real.')
                    
                    if collector.status()['running']: raise ValueError('Aguarde a coleta terminar antes de alterar as cidades.')
                    store.save_settings(live,payload); return self.respond({'ok':True})
                if endpoint=='/api/collect' and write:
                    if path==demo: raise ValueError('A coleta oficial pertence à base real.')
                    if type(payload.get('import',False)) is not bool: raise ValueError('Ação inválida.')
                    if type(payload.get('refresh',False)) is not bool: raise ValueError('Ação inválida.')
                    collector.start(payload.get('import',False),payload.get('refresh',False)); return self.respond({'ok':True},202)
                if endpoint=='/api/export' and not write:
                    records=store.list_companies(path,activity=query.get('activity',[''])[0],prospect_only=True,city=query.get('city',[''])[0],uf=query.get('uf',[''])[0],cfg=store.settings(live))
                    out=io.StringIO(); writer=csv.writer(out,delimiter=';')
                    fields=['cnpj','legal_name','activity','opened','city','uf','address','postal_code','phone','email','stage','owner','campaign','contact_validated']
                    writer.writerow(fields)
                    writer.writerows([[safe_csv(r[k]) for k in fields] for r in records])
                    return self.respond(('\ufeff'+out.getvalue()).encode(),content_type='text/csv; charset=utf-8',extra={'Content-Disposition':'attachment; filename="dbs-publico.csv"'})
                static={'/':'index.html','/app.js':'app.js','/styles.css':'styles.css'}
                if endpoint in static and not write:
                    types={'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/styles.css':'text/css; charset=utf-8'}
                    return self.respond((ROOT/'web'/static[endpoint]).read_bytes(),content_type=types[endpoint])
                self.respond({'error':'Página não encontrada.'},404)
            except KeyError as exc: self.respond({'error':str(exc)},404)
            except (ValueError,TypeError) as exc: self.respond({'error':str(exc)},400)
            except Exception:
                self.respond({'error':'Falha interna. Verifique o serviço local.'},500)
        def do_GET(self): self.handle_request()
        def do_POST(self): self.handle_request(True)
    server=ThreadingHTTPServer((host,port),Handler)
    server.collector=collector; server.live_path=live
    return server

def scheduler(server,stop):
    last_attempt=0
    while not stop.wait(30):
        if store.settings(server.live_path)['automatic'] and time.time()-last_attempt>=86400:
            try: server.collector.start(True,refresh=True); last_attempt=time.time()
            except ValueError: pass

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--host',default='127.0.0.1')
    args=parser.parse_args(); server=create_server(args.port,host=args.host)
    stop=threading.Event(); threading.Thread(target=scheduler,args=(server,stop),daemon=True).start()
    print(f'DBS CRM disponível em http://127.0.0.1:{server.server_port}',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: stop.set(); server.server_close()
if __name__=='__main__': main()
