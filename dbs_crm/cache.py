"""Durable, indexed state-wide source snapshots, independent of sales filters."""
import hashlib
import os
import json
import sqlite3
import shutil
import zipfile
from pathlib import Path
from datetime import timedelta
from . import store

class RegistryCache:
    def __init__(self, directory):
        self.directory=Path(directory)/'registry-cache'
        self.directory.mkdir(parents=True,exist_ok=True)

    def latest(self, ufs):
        candidates=[]
        for path in self.directory.glob('*/manifest.json'):
            manifest=json.loads(path.read_text())
            if set(ufs)<=set(manifest['ufs']) and (path.parent/'registry.sqlite3').exists():
                candidates.append((manifest['release']['month'],path.stat().st_mtime,manifest,path.parent))
        if not candidates: return None
        _,_,manifest,folder=max(candidates,key=lambda v:v[:2])
        return manifest,folder

    def archive(self,source,release,item,folder):
        archive_dir=self.directory/'archives'/release['fingerprint']
        archive_dir.mkdir(parents=True,exist_ok=True)
        archive=archive_dir/item['name']
        if archive.exists() and (archive.stat().st_size!=item['size'] or not zipfile.is_zipfile(archive)):
            archive.unlink()
        if not archive.exists(): source.download(release['month'],item,archive_dir)
        target=folder/item['name']
        try: os.link(archive,target)
        except (AttributeError,OSError): shutil.copyfile(archive,target)
        return target

    def build(self,source,release,ufs,existing,progress,diagnostics):
        from .collector import rows, normalized
        identity=json.dumps([release['fingerprint'],sorted(ufs),1])
        folder=self.directory/hashlib.sha256(identity.encode()).hexdigest()
        folder.mkdir(exist_ok=True)
        if (folder/'manifest.json').exists(): return json.loads((folder/'manifest.json').read_text()),folder
        db=sqlite3.connect(folder/'registry.sqlite3')
        try:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS establishments(cnpj TEXT PRIMARY KEY,root TEXT,city TEXT,uf TEXT,status TEXT,opened TEXT,payload TEXT);
                CREATE INDEX IF NOT EXISTS location_date ON establishments(uf,city,status,opened);
                CREATE INDEX IF NOT EXISTS company_root ON establishments(root);
                CREATE TABLE IF NOT EXISTS names(root TEXT PRIMARY KEY,name TEXT);
                CREATE TABLE IF NOT EXISTS domains(name TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE IF NOT EXISTS completed(name TEXT PRIMARY KEY);
            ''')
            done={r[0] for r in db.execute('SELECT name FROM completed')}
            items={r['name']:r for r in release['files']}
            order=['Municipios.zip','Cnaes.zip']+[f'Estabelecimentos{i}.zip' for i in range(10)]+[f'Empresas{i}.zip' for i in range(10)]
            roots=None
            for name in order:
                if name in done:
                    progress('Reutilizando arquivo já gravado na base local: '+name)
                    continue
                progress('Baixando para a base local: '+name)
                target=folder/name
                if not target.exists(): target=self.archive(source,release,items[name],folder)
                progress('Guardando na base local: '+name)
                try:
                    with db:
                        if name in ('Municipios.zip','Cnaes.zip'):
                            values={r[0]:r[1] for r in rows(target,diagnostics) if len(r)==2}
                            db.execute('INSERT OR REPLACE INTO domains VALUES(?,?)',(name,json.dumps(values)))
                        elif name.startswith('Estabelecimentos'):
                            codes=json.loads(db.execute("SELECT payload FROM domains WHERE name='Municipios.zip'").fetchone()[0])
                            batch=[]
                            for r in rows(target,diagnostics):
                                if len(r)!=30: raise ValueError('Layout de estabelecimentos inesperado: esperado 30 campos.')
                                cnpj=''.join(r[:3])
                                if r[19] not in ufs and cnpj not in existing: continue
                                batch.append((cnpj,r[0],normalized(codes.get(r[20],'')),r[19],r[5].zfill(2),r[10],json.dumps(r,ensure_ascii=False)))
                                if len(batch)>=2000:
                                    db.executemany('INSERT OR REPLACE INTO establishments VALUES(?,?,?,?,?,?,?)',batch);batch=[]
                            db.executemany('INSERT OR REPLACE INTO establishments VALUES(?,?,?,?,?,?,?)',batch)
                        else:
                            if roots is None: roots={r[0] for r in db.execute('SELECT DISTINCT root FROM establishments')}
                            batch=[]
                            for r in rows(target,diagnostics):
                                if len(r)!=7: raise ValueError('Layout de empresas inesperado: esperado 7 campos.')
                                if r[0] in roots: batch.append((r[0],r[1]))
                                if len(batch)>=2000:
                                    db.executemany('INSERT OR REPLACE INTO names VALUES(?,?)',batch);batch=[]
                            db.executemany('INSERT OR REPLACE INTO names VALUES(?,?)',batch)
                        db.execute('INSERT INTO completed VALUES(?)',(name,))
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                target.unlink(missing_ok=True)
            missing=db.execute('SELECT 1 FROM establishments e LEFT JOIN names n ON n.root=e.root WHERE n.root IS NULL LIMIT 1').fetchone()
            if missing: raise ValueError('Razão social não encontrada; base local não publicada.')
        finally: db.close()
        manifest={'release':release,'ufs':sorted(ufs),'created_at':store.now()}
        temporary=folder/'manifest.tmp';temporary.write_text(json.dumps(manifest));temporary.replace(folder/'manifest.json')
        return manifest,folder

    def select(self,snapshot,cfg,existing,asof=None):
        from .collector import parse_establishment
        manifest,folder=snapshot
        asof=asof or store.today()
        db=sqlite3.connect(folder/'registry.sqlite3')
        try:
            domains={name:json.loads(value) for name,value in db.execute('SELECT name,payload FROM domains')}
            codes=domains['Municipios.zip'];cnaes=domains['Cnaes.zip']
            selected={}
            def accept(records):
                for payload,name in records:
                    result=parse_establishment(json.loads(payload),codes,cnaes,cfg,manifest['release']['month'],asof,existing)
                    if result:
                        if name is None: raise ValueError('Razão social ausente na base local.')
                        result['legal_name']=name;selected[result['cnpj']]=result
            query='SELECT e.payload,n.name FROM establishments e LEFT JOIN names n ON n.root=e.root '
            start=(asof-timedelta(days=cfg['days'])).strftime('%Y%m%d')
            for city,uf in store.location_pairs(cfg):
                accept(db.execute(query+"WHERE e.city=? AND e.uf=? AND e.status='02' AND e.opened BETWEEN ? AND ?",(city,uf,start,asof.strftime('%Y%m%d'))))
            for cnpj in existing:
                if cnpj not in selected: accept(db.execute(query+'WHERE e.cnpj=?',(cnpj,)))
            return list(selected.values()),set(existing)-set(selected)
        finally: db.close()
