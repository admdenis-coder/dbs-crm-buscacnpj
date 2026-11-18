"""Persistent local CRM. Registry updates never overwrite sales fields."""
import json
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

STAGES = ['Nova', 'Em contato', 'Interessada', 'Viabilidade técnica', 'Proposta', 'Ganha', 'Perdida']
REGISTRY = ['cnpj', 'legal_name', 'trade_name', 'city', 'uf', 'opened', 'status', 'cnae', 'activity', 'address', 'postal_code', 'phone', 'email', 'source_month']
DEFAULTS = {'city': 'RIO VERDE', 'uf': 'GO', 'days': 180, 'automatic': False}

UFS = set('AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO'.split())

def normalize_city(value):
    return ''.join(c for c in unicodedata.normalize('NFD', value.upper().strip()) if unicodedata.category(c) != 'Mn')

def locations(cfg):
    return cfg.get('locations') or [{'city': cfg['city'], 'uf': cfg['uf']}]

def location_pairs(cfg):
    return {(normalize_city(r['city']), r['uf']) for r in locations(cfg)}

def now():
    return datetime.now(ZoneInfo('America/Sao_Paulo')).isoformat(timespec='seconds')

def today():
    return datetime.now(ZoneInfo('America/Sao_Paulo')).date()

@contextmanager
def connect(path):
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def init(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS companies(
          cnpj TEXT PRIMARY KEY, legal_name TEXT NOT NULL, trade_name TEXT DEFAULT '',
          city TEXT, uf TEXT, opened TEXT, status TEXT, cnae TEXT, activity TEXT,
          address TEXT, phone TEXT, email TEXT, source_month TEXT,
          stage TEXT NOT NULL DEFAULT 'Nova', owner TEXT NOT NULL DEFAULT '',
          next_action TEXT NOT NULL DEFAULT '', coverage TEXT NOT NULL DEFAULT 'A consultar',
          campaign TEXT NOT NULL DEFAULT '', loss_reason TEXT NOT NULL DEFAULT '',
          contact_validated INTEGER NOT NULL DEFAULT 0, is_customer INTEGER NOT NULL DEFAULT 0,
          do_not_contact INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, cnpj TEXT NOT NULL REFERENCES companies(cnpj),
          text TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started TEXT NOT NULL, ended TEXT,
          state TEXT NOT NULL, message TEXT NOT NULL, month TEXT, imported INTEGER DEFAULT 0);
        ''')
        columns={r[1] for r in db.execute('PRAGMA table_info(companies)')}
        if 'postal_code' not in columns:
            db.execute("ALTER TABLE companies ADD COLUMN postal_code TEXT NOT NULL DEFAULT ''")
        db.execute('INSERT OR IGNORE INTO settings VALUES(1,?)', (json.dumps(DEFAULTS),))

def settings(path):
    with connect(path) as db:
        cfg=json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
        cfg['locations']=locations(cfg)
        return cfg

def save_settings(path, value):
    if not isinstance(value.get('automatic'), bool):
        raise ValueError('Configuração automática inválida.')
    if type(value.get('days')) is not int or not 1 <= value['days'] <= 730:
        raise ValueError('Escolha entre 1 e 730 dias.')
    selected=value.get('locations')
    if selected is None and 'city' in value and 'uf' in value:
        selected=[{'city':value['city'],'uf':value['uf']}]
    if not isinstance(selected,list) or not 1 <= len(selected) <= 100:
        raise ValueError('Adicione entre 1 e 100 cidades à coleta.')
    clean=[]
    for entry in selected:
        if not isinstance(entry,dict) or not isinstance(entry.get('city'),str) or not isinstance(entry.get('uf'),str):
            raise ValueError('Cidade e estado inválidos.')
        city=normalize_city(' '.join(entry['city'].split())); uf=entry['uf'].upper().strip()
        if uf not in UFS or not 2 <= len(city) <= 100 or not any(c.isalpha() for c in city) or any(not(c.isalpha() or c in " '-") for c in city):
            raise ValueError('Informe o nome completo da cidade e uma UF válida.')
        row={'city':city,'uf':uf}
        if row not in clean: clean.append(row)
    clean.sort(key=lambda r:(r['uf'],r['city']))
    cfg={'locations':clean,'city':clean[0]['city'],'uf':clean[0]['uf'],'days':value['days'],'automatic':value['automatic']}
    with connect(path) as db:
        db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(cfg),))


def upsert(db, rows):
    assignments = ','.join(f'{k}=excluded.{k}' for k in REGISTRY if k != 'cnpj')
    sql = f"INSERT INTO companies ({','.join(REGISTRY)},updated_at) VALUES ({','.join('?' for _ in range(len(REGISTRY)+1))}) ON CONFLICT(cnpj) DO UPDATE SET {assignments},updated_at=excluded.updated_at"
    db.executemany(sql, [[r.get(k,'') for k in REGISTRY] + [now()] for r in rows])

def list_companies(path, query='', stage='', activity='', prospect_only=False, city='', uf='', cfg=None):
    cfg = cfg or settings(path)
    with connect(path) as db:
        db.create_function('NORMALIZE_CITY',1,lambda v:normalize_city(v or ''))
        clauses, args = [], []
        if city:
            clauses.append('NORMALIZE_CITY(city)=?'); args.append(normalize_city(city))
        if uf:
            clauses.append('uf=?'); args.append(uf)
        if query:
            clauses.append('(legal_name LIKE ? OR trade_name LIKE ? OR cnpj LIKE ?)')
            args += ['%'+query+'%']*3
        if stage:
            clauses.append('stage=?'); args.append(stage)
        if activity:
            clauses.append('activity=?'); args.append(activity)
        if prospect_only:
            pairs=sorted(location_pairs(cfg))
            clauses.append('('+' OR '.join('(NORMALIZE_CITY(city)=? AND uf=?)' for _ in pairs)+')')
            args += [v for pair in pairs for v in pair]
            clauses += ["status='02'", 'opened>=?', 'opened<=?', 'is_customer=0', 'do_not_contact=0']
            args += [(today()-timedelta(days=cfg['days'])).isoformat(), today().isoformat()]
        where = ' WHERE '+' AND '.join(clauses) if clauses else ''
        return [dict(r) for r in db.execute('SELECT * FROM companies'+where+' ORDER BY opened DESC,cnpj', args)]

def detail(path, cnpj):
    with connect(path) as db:
        row = db.execute('SELECT * FROM companies WHERE cnpj=?', (cnpj,)).fetchone()
        if row is None:
            raise KeyError('Empresa não encontrada.')
        result = dict(row)
        result['notes'] = [dict(r) for r in db.execute('SELECT * FROM notes WHERE cnpj=? ORDER BY id DESC', (cnpj,))]
        return result

def update(path, cnpj, payload):
    allowed = {'stage','owner','next_action','coverage','campaign','loss_reason','contact_validated','is_customer','do_not_contact'}
    if not payload or set(payload)-allowed:
        raise ValueError('Campos de edição inválidos.')
    before = detail(path, cnpj)
    after = {**before, **payload}
    if after['stage'] not in STAGES:
        raise ValueError('Etapa inválida.')
    if after['coverage'] not in ['A consultar','Disponível','Indisponível']:
        raise ValueError('Cobertura inválida.')
    if after['stage']=='Ganha' and after['coverage']!='Disponível':
        raise ValueError('Confirme a cobertura antes de marcar como ganha.')
    if after['stage']=='Perdida' and not after['loss_reason'].strip():
        raise ValueError('Informe o motivo da perda.')
    for key,value in payload.items():
        if key in ['contact_validated','is_customer','do_not_contact']:
            if type(value) is not bool:
                raise ValueError('Indicador inválido.')
        elif not isinstance(value,str) or len(value)>500:
            raise ValueError('Texto inválido ou muito longo.')
    if after['next_action']:
        try: date.fromisoformat(after['next_action'])
        except ValueError: raise ValueError('Data de retorno inválida.')
    labels = {'stage':'Etapa','owner':'Responsável','next_action':'Retorno','coverage':'Cobertura','campaign':'Campanha','loss_reason':'Motivo da perda','contact_validated':'Contato validado','is_customer':'Cliente DBS','do_not_contact':'Não contatar'}
    changes = [f'{labels[k]}: {before[k]} → {v}' for k,v in payload.items() if before[k] != v]
    with connect(path) as db:
        db.execute('UPDATE companies SET '+','.join(k+'=?' for k in payload)+',updated_at=? WHERE cnpj=?', list(payload.values())+[now(),cnpj])
        if changes:
            db.execute('INSERT INTO notes(cnpj,text,created_at) VALUES(?,?,?)', (cnpj,'Atualização comercial — '+'; '.join(changes),now()))
    return detail(path,cnpj)

def add_note(path, cnpj, text):
    if not isinstance(text,str) or not text.strip() or len(text)>5000:
        raise ValueError('Escreva uma anotação de até 5.000 caracteres.')
    detail(path,cnpj)
    with connect(path) as db:
        db.execute('INSERT INTO notes(cnpj,text,created_at) VALUES(?,?,?)',(cnpj,text.strip(),now()))

def seed_demo(path):
    init(path)
    with connect(path) as db:
        if db.execute('SELECT count(*) FROM companies').fetchone()[0]: return
        samples=[('Café Horizonte','Alimentação','5611203','Nova',12),('Loja Cerrado','Comércio','4781400','Em contato',35),('Escritório Aurora','Serviços','6920601','Proposta',70),('Oficina Ipê','Serviços','4520001','Interessada',100)]
        for i,(name,activity,cnae,stage,days) in enumerate(samples):
            row={k:'' for k in REGISTRY}
            row.update(cnpj=f'DEMO-{i+1:04}',legal_name=name+' · fictícia',trade_name=name,city='RIO VERDE',uf='GO',opened=(today()-timedelta(days=days)).isoformat(),status='02',cnae=cnae,activity=activity,address='Endereço demonstrativo · Rio Verde–GO',source_month='Demonstração')
            upsert(db,[row])
            db.execute('UPDATE companies SET stage=?,owner=? WHERE cnpj=?',(stage,'Equipe DBS',row['cnpj']))
