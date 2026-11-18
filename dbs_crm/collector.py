"""Official Receita collector: discover, stream ZIPs, filter, atomically publish.
Public share identifier is published by Receita; no private credentials are used.
"""
import base64
import csv
import hashlib
import io
import json
import re
import shutil
import tempfile
import threading
import time
import socket
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import zipfile
from datetime import timedelta
from pathlib import Path
from . import store
from .cache import RegistryCache

HOST='https://arquivos.receitafederal.gov.br'
PUBLIC_SHARE='gn672Ad4CF8N6TK'
ROOT='/Dados/Cadastros/CNPJ'
SOURCE=HOST+'/index.php/s/'+PUBLIC_SHARE+'?dir='+ROOT
LAYOUT='https://www.gov.br/receitafederal/dados/cnpj-metadados.pdf'
DAV=HOST+'/public.php/webdav'
LOCK=threading.Lock()

def normalized(s):
    return ''.join(c for c in unicodedata.normalize('NFD',s.upper().strip()) if unicodedata.category(c)!='Mn')

class OfficialSource:
    def request(self,path,method='GET'):
        headers={'User-Agent':'DBS-CRM-local/0.1 (public CNPJ data)',
                 'Authorization':'Basic '+base64.b64encode((PUBLIC_SHARE+':').encode()).decode()}
        if method=='PROPFIND':
            headers['Depth']='1'
        url=DAV+urllib.parse.quote(path,safe='/')
        for attempt in range(3):
            try:
                response=urllib.request.urlopen(urllib.request.Request(url,headers=headers,method=method),timeout=120)
                break
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                if isinstance(exc, urllib.error.HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                    raise
                if attempt == 2:
                    raise ConnectionError('Não foi possível conectar à Receita após 3 tentativas. A fonte está indisponível ou a conexão expirou; nenhum cadastro foi alterado.') from exc
                time.sleep(2 ** (attempt + 1))
        if urllib.parse.urlparse(response.url).hostname!='arquivos.receitafederal.gov.br':
            response.close(); raise ValueError('Redirecionamento para fora da fonte oficial.')
        return response

    def listing(self,path):
        with self.request(path,'PROPFIND') as response:
            root=ET.fromstring(response.read(8*1024*1024))
        items=[]
        for node in root.findall('{DAV:}response'):
            href=urllib.parse.unquote(node.findtext('{DAV:}href','')).rstrip('/')
            name=href.rsplit('/',1)[-1]
            if name==path.rstrip('/').rsplit('/',1)[-1]: continue
            prop=node.find('.//{DAV:}prop')
            if prop is None: continue
            items.append({'name':name,'size':int(prop.findtext('{DAV:}getcontentlength','0') or 0),
                          'etag':prop.findtext('{DAV:}getetag',''),
                          'directory':prop.find('.//{DAV:}collection') is not None})
        return items

    def discover(self):
        months=sorted((i['name'] for i in self.listing(ROOT) if i['directory'] and re.fullmatch(r'20\d{2}-\d{2}',i['name'])),reverse=True)
        for month in months:
            files=self.listing(ROOT+'/'+month)
            names={i['name'] for i in files if not i['directory'] and i['size']>0}
            required={'Municipios.zip','Cnaes.zip'}|{f'{kind}{i}.zip' for kind in ['Empresas','Estabelecimentos'] for i in range(10)}
            if required<=names:
                selected=sorted((i for i in files if i['name'] in required),key=lambda i:i['name'])
                fingerprint=hashlib.sha256(json.dumps(selected,sort_keys=True).encode()).hexdigest()
                return {'month':month,'files':selected,'bytes':sum(i['size'] for i in selected),'fingerprint':fingerprint,'source':SOURCE}
        raise ValueError('Nenhuma publicação completa encontrada. A estrutura da fonte pode ter mudado.')

    def download(self,month,item,directory):
        target=Path(directory)/item['name']
        if shutil.disk_usage(directory).free < max(item['size']*2,1024**3):
            raise ValueError('Espaço livre insuficiente para processar o próximo arquivo.')
        partial=target.with_suffix('.part')
        try:
            with self.request(ROOT+'/'+month+'/'+item['name']) as response, partial.open('wb') as out:
                received=0
                report=getattr(self,'download_progress',None)
                if report: report(item,0)
                while True:
                    chunk=response.read(1024*1024)
                    if not chunk: break
                    out.write(chunk); received+=len(chunk)
                    if report: report(item,received)
            if item['size'] and partial.stat().st_size!=item['size']:
                raise ValueError('Download incompleto: '+item['name'])
            if not zipfile.is_zipfile(partial):
                raise ValueError('A fonte não retornou um ZIP válido: '+item['name'])
            partial.replace(target)
            return target
        finally:
            partial.unlink(missing_ok=True)

def rows(path, diagnostics=None):
    """Remove NUL padding before Python 3.9's CSV parser, without dropping rows.

    Keep physical line endings and let csv.reader handle quoted multiline fields.
    Report source context on errors and counts, never personal field contents.
    """
    diagnostics = diagnostics if diagnostics is not None else {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir(): continue
            with archive.open(info) as binary:
                with io.TextIOWrapper(binary,encoding='latin-1',newline='') as text:
                    def clean_lines():
                        for line in text:
                            count = line.count('\x00')
                            if count:
                                diagnostics['nul_bytes'] = diagnostics.get('nul_bytes', 0) + count
                                line = line.replace('\x00', '')
                            yield line
                    reader = csv.reader(clean_lines(), delimiter=';')
                    try:
                        yield from reader
                    except csv.Error as exc:
                        raise ValueError(f'{Path(path).name} / {info.filename}, linha {reader.line_num}: {exc}') from exc

def parse_establishment(row, codes, cnaes, cfg, month, asof, existing):
    if len(row)!=30: raise ValueError('Layout de estabelecimentos inesperado: esperado 30 campos.')
    cnpj=''.join(row[:3])
    city=codes.get(row[20],'')
    pairs=cfg.get('_pairs') or store.location_pairs(cfg)
    wanted_ufs=cfg.get('_ufs') or {uf for _,uf in pairs}
    # Most of the national file is outside the pilot. Avoid parsing every date.
    if cnpj not in existing and (row[19] not in wanted_ufs or row[5].zfill(2)!='02' or (normalized(city), row[19]) not in pairs):
        return None
    opened=row[10]
    if not re.fullmatch(r'\d{8}',opened):
        if cnpj in existing: raise ValueError('Data de abertura inválida em registro existente.')
        return None
    try:
        opening=store.date(int(opened[:4]),int(opened[4:6]),int(opened[6:]))
    except ValueError:
        if cnpj in existing: raise ValueError('Data de abertura inválida em registro existente.')
        return None
    city=codes.get(row[20],'')
    qualifies=((normalized(city), row[19]) in pairs and row[5].zfill(2)=='02' and asof-timedelta(days=cfg['days'])<=opening<=asof)
    if not qualifies and cnpj not in existing: return None
    if len(cnpj)!=14: raise ValueError('CNPJ com tamanho inválido.')
    return dict(cnpj=cnpj,legal_name='',trade_name=row[4],city=city,uf=row[19],opened=opening.isoformat(),status=row[5].zfill(2),cnae=row[11],activity=cnaes.get(row[11],row[11]),address=' '.join(v for v in row[13:18] if v),postal_code=row[18],phone=(row[21]+row[22]) if row[22] else '',email=row[27],source_month=month)

def process_release(source, release, cfg, existing, directory, progress, asof=None, diagnostics=None):
    """Keep only selected municipalities and existing leads in memory, never national data."""
    asof=asof or store.today()
    cfg=dict(cfg)
    cfg['_pairs']=store.location_pairs(cfg)
    cfg['_ufs']={uf for _,uf in cfg['_pairs']}
    diagnostics = diagnostics if diagnostics is not None else {}
    month=release['month']; items={f['name']:f for f in release['files']}
    def load(name):
        progress('Baixando e lendo '+name)
        return source.download(month,items[name],directory)
    domains=[]
    for name in ['Municipios.zip','Cnaes.zip']:
        path=load(name)
        domains.append(dict((r[0],r[1]) for r in rows(path, diagnostics) if len(r)==2))
        path.unlink()
    codes,cnaes=domains
    known={normalized(v) for v in codes.values()}
    unknown=[r['city'] for r in store.locations(cfg) if normalized(r['city']) not in known]
    if unknown:
        raise ValueError('Município não encontrado na tabela da Receita: '+', '.join(unknown))
    selected={}
    for i in range(10):
        path=load(f'Estabelecimentos{i}.zip')
        progress(f'Lendo Estabelecimentos{i}.zip; {len(selected)} cadastros selecionados até agora')
        for row in rows(path, diagnostics):
            result=parse_establishment(row,codes,cnaes,cfg,month,asof,existing)
            if result: selected[result['cnpj']]=result
        path.unlink()
    roots={cnpj[:8] for cnpj in selected}
    names={}
    for i in range(10):
        path=load(f'Empresas{i}.zip')
        progress(f'Lendo Empresas{i}.zip; {len(names)} razões sociais localizadas até agora')
        for row in rows(path, diagnostics):
            if len(row)!=7: raise ValueError('Layout de empresas inesperado: esperado 7 campos.')
            if row[0] in roots: names[row[0]]=row[1]
        path.unlink()
    for cnpj,result in selected.items():
        if cnpj[:8] not in names: raise ValueError('Razão social não encontrada; publicação não aplicada.')
        result['legal_name']=names[cnpj[:8]]
    # Missing companies are retained, flagged for review rather than falsely declared inactive.
    return list(selected.values()), set(existing)-set(selected)

class Collector:
    def __init__(self,path,data_dir):
        self.path=path; self.data_dir=Path(data_dir); self.source=OfficialSource()
        self.catalog=None
        self.transfer=None
        self.cache=RegistryCache(self.data_dir)
    def status(self):
        with store.connect(self.path) as db:
            runs=[dict(r) for r in db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 12')]
        cached=self.cache.latest({r['uf'] for r in store.locations(store.settings(self.path))})
        return {'cached':cached[0] if cached else None,'running':LOCK.locked(),'catalog':self.catalog,'runs':runs,'source':SOURCE,'layout':LAYOUT,'transfer':self.transfer}
    def start(self,import_data=False,refresh=False):
        if not LOCK.acquire(blocking=False): raise ValueError('Uma coleta já está em andamento.')
        self.transfer=None
        thread=threading.Thread(target=self._run,args=(import_data,refresh),daemon=True)
        thread.start()
    def _run(self,import_data,refresh=False):
        run_id=None
        last_progress='Verificando a fonte oficial'
        try:
            with store.connect(self.path) as db:
                run_id=db.execute("INSERT INTO runs(started,state,message) VALUES(?,'executando','Verificando a fonte oficial')",(store.now(),)).lastrowid
            def progress(message):
                nonlocal last_progress
                last_progress=message
                with store.connect(self.path) as db: db.execute('UPDATE runs SET message=? WHERE id=?',(message,run_id))
            cfg=store.settings(self.path)
            ufs={'GO'}|{r['uf'] for r in store.locations(cfg)}
            with store.connect(self.path) as db:
                existing={r[0] for r in db.execute('SELECT cnpj FROM companies')}
                ufs.update(r[0] for r in db.execute('SELECT DISTINCT uf FROM companies'))
            snapshot=self.cache.latest(ufs) if import_data else None
            if snapshot and not refresh:
                release=snapshot[0]['release']
            else:
                release=self.source.discover()
            self.catalog=release
            with store.connect(self.path) as db: db.execute('UPDATE runs SET month=? WHERE id=?',(release['month'],run_id))
            imported=0
            if import_data:
                if not snapshot or snapshot[0]['release']['fingerprint']!=release['fingerprint']:
                    diagnostics = {}
                    archive_dir=self.cache.directory/'archives'/release['fingerprint']
                    download_total=sum(item['size'] for item in release['files']
                                       if not (archive_dir/item['name']).exists()
                                       or (archive_dir/item['name']).stat().st_size!=item['size'])
                    completed_bytes=0
                    current_file=None
                    previous_received=0
                    download_seconds=0.0
                    last_tick=time.monotonic()
                    def download_progress(item,received):
                        nonlocal completed_bytes,current_file,previous_received,download_seconds,last_tick
                        now=time.monotonic()
                        if current_file!=item['name']:
                            completed_bytes+=previous_received
                            current_file=item['name']
                            previous_received=0
                        elif received>previous_received:
                            download_seconds+=now-last_tick
                        last_tick=now
                        previous_received=received
                        downloaded=completed_bytes+received
                        speed=downloaded/download_seconds if download_seconds>=2 else 0
                        self.transfer={'file':current_file,'file_bytes':item['size'],'file_received':received,
                                       'received':downloaded,'total':max(download_total,downloaded),
                                       'speed':speed,'eta':max(0,(download_total-downloaded)/speed) if speed else None,
                                       'phase':'download' if received<item['size'] else 'processing'}
                    self.source.download_progress=download_progress
                    snapshot=self.cache.build(self.source,release,ufs,existing,progress,diagnostics)
                progress('Consultando cidades e período na base local, sem novo download')
                data,missing=self.cache.select(snapshot,cfg,existing)
                if self.transfer: self.transfer={**self.transfer,'phase':'saving'}
                with store.connect(self.path) as db:
                    store.upsert(db,data)
                    for cnpj in missing:
                        db.execute("UPDATE companies SET status='REVISAR',updated_at=? WHERE cnpj=?",(store.now(),cnpj))
                imported=len(data)
                message=f'{imported} cadastros processados a partir da base local {release["month"]}; {len(missing)} ausentes sinalizados para revisão. Cidades e períodos podem ser consultados novamente sem baixar a publicação.'
            else:
                message='Publicação completa disponível. Verificação concluída sem baixar a base nacional.'
            if self.transfer: self.transfer={**self.transfer,'phase':'complete'}
            with store.connect(self.path) as db:
                db.execute("UPDATE runs SET state='concluído',ended=?,message=?,imported=? WHERE id=?",(store.now(),message,imported,run_id))
        except Exception as exc:
            if self.transfer: self.transfer={**self.transfer,'phase':'failed','eta':None}
            if run_id:
                with store.connect(self.path) as db: db.execute("UPDATE runs SET state='falha',ended=?,message=? WHERE id=?",(store.now(),f'{last_progress}. {exc}'[:800],run_id))
        finally:
            self.source.download_progress=None
            LOCK.release()
