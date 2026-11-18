import csv
import io
import json
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
import urllib.request
import urllib.error
import zipfile
from datetime import date, timedelta
from pathlib import Path
from dbs_crm import store
from dbs_crm.collector import parse_establishment, process_release, Collector, LOCK, rows, OfficialSource
from dbs_crm.server import create_server, safe_csv

def record(cnpj='12345678000195'):
    r={k:'' for k in store.REGISTRY}
    r.update(cnpj=cnpj,legal_name='Empresa de teste',city='RIO VERDE',uf='GO',opened=store.today().isoformat(),status='02',source_month='2026-11',activity='Teste')
    return r

def establishment(cnpj='12345678000195',opened='20261101',status='02',city='9571',uf='GO'):
    r=['']*30
    for k,v in {0:cnpj[:8],1:cnpj[8:12],2:cnpj[12:],4:'Fantasia',5:status,10:opened,11:'5611203',19:uf,20:city,21:'64',22:'33330000',27:'contato@example.test'}.items():r[k]=v
    return r

class StoreTests(unittest.TestCase):
    def test_multiple_cities_normalization_filters_and_history(self):
        store.save_settings(self.path,{'locations':[{'city':'Rio Verde','uf':'GO'},{'city':'Jataí','uf':'go'},{'city':'JATAI','uf':'GO'}],'days':180,'automatic':False})
        self.assertEqual(len(store.settings(self.path)['locations']),2)
        other=record('99999999000100');other.update(city='JATAÍ',postal_code='01234567',phone='6433330000',email='teste@example.test')
        with store.connect(self.path) as db:store.upsert(db,[other])
        self.assertEqual(len(store.list_companies(self.path,prospect_only=True)),2)
        filtered=store.list_companies(self.path,city='Jatai',uf='GO',prospect_only=True)
        self.assertEqual(filtered[0]['postal_code'],'01234567')
        store.add_note(self.path,other['cnpj'],'Preservar histórico')
        store.save_settings(self.path,{'locations':[{'city':'Rio Verde','uf':'GO'}],'days':180,'automatic':False})
        self.assertEqual(len(store.list_companies(self.path,prospect_only=True)),1)
        self.assertEqual(store.detail(self.path,other['cnpj'])['notes'][0]['text'],'Preservar histórico')
    def test_invalid_locations(self):
        for locations in [[],[{'city':'Rio Verde','uf':'ZZ'}],[{'city':'','uf':'GO'}],['Jatai']]:
            with self.assertRaises(ValueError):store.save_settings(self.path,{'locations':locations,'days':180,'automatic':False})
    def test_legacy_settings_and_idempotent_schema(self):
        self.assertEqual(store.settings(self.path)['locations'],[{'city':'RIO VERDE','uf':'GO'}])
        store.init(self.path);store.init(self.path)
        self.assertEqual(store.detail(self.path,'12345678000195')['postal_code'],'')
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'test.sqlite3';store.init(self.path)
        with store.connect(self.path) as db:store.upsert(db,[record()])
    def tearDown(self):self.temp.cleanup()
    def test_registry_upsert_preserves_sales(self):
        store.update(self.path,'12345678000195',{'stage':'Proposta','owner':'Vendedora','next_action':store.today().isoformat()})
        store.add_note(self.path,'12345678000195','Conversa inicial')
        r=record();r['legal_name']='Nova razão';r['status']='08'
        with store.connect(self.path) as db:store.upsert(db,[r])
        saved=store.detail(self.path,r['cnpj'])
        self.assertEqual(saved['stage'],'Proposta');self.assertEqual(saved['owner'],'Vendedora')
        self.assertEqual(saved['notes'][0]['text'],'Conversa inicial')
        self.assertEqual(len(store.list_companies(self.path)),1)
        self.assertEqual(store.list_companies(self.path,prospect_only=True),[])
    def test_history_does_not_disappear_when_aged_out(self):
        r=record();r['opened']=(store.today()-timedelta(days=181)).isoformat()
        with store.connect(self.path) as db:store.upsert(db,[r])
        self.assertEqual(len(store.list_companies(self.path)),1)
        self.assertEqual(len(store.list_companies(self.path,prospect_only=True)),0)
    def test_customer_and_blocked_excluded(self):
        for field in ['is_customer','do_not_contact']:
            store.update(self.path,'12345678000195',{field:True})
            self.assertEqual(store.list_companies(self.path,prospect_only=True),[])
            store.update(self.path,'12345678000195',{field:False})
    def test_validation(self):
        for payload in [{'stage':'Inválida'},{'stage':'Ganha'},{'stage':'Perdida'},{'next_action':'not-a-date'},{'owner':500},{'city':'GOIANIA'}]:
            with self.assertRaises(ValueError):store.update(self.path,'12345678000195',payload)
    def test_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with store.connect(self.path) as db:
                store.upsert(db,[record('99999999000100')]);raise RuntimeError()
        self.assertEqual(len(store.list_companies(self.path)),1)

class ParserTests(unittest.TestCase):
    def test_multiple_municipalities_state_and_postal_code(self):
        cfg={'locations':[{'city':'RIO VERDE','uf':'GO'},{'city':'JATAI','uf':'GO'}],'days':180}
        codes={'9571':'RIO VERDE','9999':'JATAÍ'}
        row=establishment(city='9999');row[18]='01234567'
        result=parse_establishment(row,codes,{},cfg,'2026-11',date(2026,11,17),set())
        self.assertEqual(result['postal_code'],'01234567')
        row[19]='MT'
        self.assertIsNone(parse_establishment(row,codes,{},cfg,'2026-11',date(2026,11,17),set()))
    def test_transient_connection_retries(self):
        response=MagicMock(); response.url='https://arquivos.receitafederal.gov.br/public.php/webdav/test'
        with patch('dbs_crm.collector.urllib.request.urlopen',side_effect=[urllib.error.URLError('timeout'),response]) as request, patch('dbs_crm.collector.time.sleep'):
            self.assertIs(OfficialSource().request('/test'),response)
            self.assertEqual(request.call_count,2)
    def test_connection_failure_has_bounded_retries(self):
        with patch('dbs_crm.collector.urllib.request.urlopen',side_effect=urllib.error.URLError('timeout')) as request, patch('dbs_crm.collector.time.sleep'):
            with self.assertRaisesRegex(ConnectionError,'3 tentativas'):OfficialSource().request('/test')
            self.assertEqual(request.call_count,3)
    def test_missing_file_is_not_retried(self):
        error=urllib.error.HTTPError('https://arquivos.receitafederal.gov.br/',404,'Not found',{},None)
        with patch('dbs_crm.collector.urllib.request.urlopen',side_effect=error) as request:
            with self.assertRaises(urllib.error.HTTPError):OfficialSource().request('/test')
            self.assertEqual(request.call_count,1)
    def test_nul_in_zip_preserves_quotes_multiline_accents_and_records(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'Empresas0.zip'
            content='"12345678";"Razão\x00; com ""aspas""\r\ne acento ç";"2062";"49";"1000,00";"01";""\r\n"87654321";"Outra";"2062";"49";"0,00";"01";"\x00"\r\n'
            with zipfile.ZipFile(path,'w') as z:z.writestr('EMPRESAS',content.encode('latin-1'))
            diagnostics={}; records=list(rows(path,diagnostics))
            self.assertEqual(len(records),2)
            self.assertTrue(all(len(r)==7 for r in records))
            self.assertEqual(records[0][1],'Razão; com "aspas"\r\ne acento ç')
            self.assertEqual(records[1][-1],'')
            self.assertEqual(diagnostics['nul_bytes'],2)
    def parse(self,row,existing=()):
        return parse_establishment(row,{'9571':'RIO VERDE','9999':'OUTRA'},{'5611203':'Restaurantes'},store.DEFAULTS,'2026-11',date(2026,11,17),existing)
    def test_dates_city_status_boundaries(self):
        self.assertIsNotNone(self.parse(establishment(opened='20260521')))
        for kwargs in [{'opened':'20260520'},{'opened':'20261118'},{'status':'08'},{'city':'9999'},{'uf':'MT'}]:
            self.assertIsNone(self.parse(establishment(**kwargs)))
    def test_existing_outside_filter_updates(self):
        row=self.parse(establishment(status='08',city='9999'),{'12345678000195'})
        self.assertEqual(row['status'],'08');self.assertEqual(row['city'],'OUTRA')
    def test_mapping(self):
        r=self.parse(establishment());self.assertEqual(r['phone'],'6433330000');self.assertEqual(r['activity'],'Restaurantes')
    def test_layout_error(self):
        with self.assertRaises(ValueError):self.parse(['bad'])
    def test_complete_release(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);origin=root/'origin';origin.mkdir();work=root/'work';work.mkdir()
            files=[]
            def write(name,records):
                data=io.StringIO();csv.writer(data,delimiter=';').writerows(records)
                with zipfile.ZipFile(origin/name,'w') as z:z.writestr('DATA',data.getvalue().encode('latin-1'))
                files.append({'name':name,'size':(origin/name).stat().st_size})
            write('Municipios.zip',[['9571','RIO VERDE']]);write('Cnaes.zip',[['5611203','Restaurantes']])
            for i in range(10):
                row=establishment(); row[4]='Fantasia\x00'
                write(f'Estabelecimentos{i}.zip',[row] if i==0 else [])
                write(f'Empresas{i}.zip',[['12345678','Razão\x00 oficial','2062','49','1000,00','01','']] if i==0 else [])
            class Source:
                def download(self,month,item,directory):
                    dst=Path(directory)/item['name'];shutil.copyfile(origin/item['name'],dst);return dst
            results,missing=process_release(Source(),{'month':'2026-11','files':files},store.DEFAULTS,{'99999999000100'},work,lambda _:None,date(2026,11,17))
            self.assertEqual(results[0]['legal_name'],'Razão oficial');self.assertEqual(missing,{'99999999000100'})
            self.assertEqual(list(work.iterdir()),[])

class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.server=create_server(0,cls.temp.name)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()
    def get(self,path):
        with urllib.request.urlopen(self.base+path) as r:return json.load(r)
    def test_demo_is_separate(self):
        self.assertEqual(self.get('/api/state')['companies'],[])
        self.assertEqual(len(self.get('/api/state?mode=demo')['companies']),4)
    def test_write_requires_token(self):
        request=urllib.request.Request(self.base+'/api/settings',data=b'{}',headers={'Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code,403)
    def test_bad_origin(self):
        request=urllib.request.Request(self.base+'/api/state',headers={'Origin':'https://example.test'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code,403)
    def test_persist_note(self):
        token=self.get('/api/state')['token']
        request=urllib.request.Request(self.base+'/api/companies/DEMO-0001/notes?mode=demo',data=json.dumps({'text':'Teste de persistência'}).encode(),headers={'Content-Type':'application/json','X-DBS-Token':token})
        with urllib.request.urlopen(request) as r:self.assertEqual(r.status,200)
        self.assertEqual(self.get('/api/companies/DEMO-0001?mode=demo')['notes'][0]['text'],'Teste de persistência')
    def test_csv_formula(self):
        self.assertEqual(safe_csv('=CMD()'),"'=CMD()")
        self.assertEqual(safe_csv('  @SUM(1)'),"'  @SUM(1)")
        self.assertEqual(safe_csv('Empresa'),'Empresa')

if __name__=='__main__':unittest.main()

class DownloadProgressTests(unittest.TestCase):
    def test_reports_bytes_and_validates_download(self):
        payload=io.BytesIO()
        with zipfile.ZipFile(payload,'w') as archive:
            archive.writestr('sample.csv','x'*(2*1024*1024))
        raw=payload.getvalue()
        events=[]
        source=OfficialSource()
        source.download_progress=lambda item,count: events.append(count)
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(source,'request',return_value=io.BytesIO(raw)):
                target=source.download('2026-11',{'name':'sample.zip','size':len(raw)},folder)
            self.assertTrue(zipfile.is_zipfile(target))
            self.assertEqual(events[0],0)
            self.assertEqual(events[-1],len(raw))
            self.assertEqual(events,sorted(events))
            self.assertGreater(len(events),2)
            self.assertFalse(Path(folder,'sample.part').exists())

class RegistryCacheTests(unittest.TestCase):
    def test_state_snapshot_reuses_cities_periods_and_survives_restart(self):
        from dbs_crm.cache import RegistryCache
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);origin=root/'origin';origin.mkdir();files=[]
            def write(name,records):
                text=io.StringIO();csv.writer(text,delimiter=';').writerows(records)
                with zipfile.ZipFile(origin/name,'w') as z:z.writestr('DATA',text.getvalue().encode('latin-1'))
                files.append({'name':name,'size':(origin/name).stat().st_size})
            write('Municipios.zip',[['9571','RIO VERDE'],['1234','JATAI']]);write('Cnaes.zip',[['5611203','Restaurantes']])
            records=[establishment(),establishment('22345678000195',city='1234'),establishment('32345678000195',city='1234',opened='20260701'),establishment('42345678000195',city='1234',status='08')]
            for i in range(10):
                write(f'Estabelecimentos{i}.zip',records if i==0 else [])
                write(f'Empresas{i}.zip',[[r[0],'Empresa '+r[0],'2062','49','1000,00','01',''] for r in records] if i==0 else [])
            class Source:
                calls=[]
                def download(self,month,item,directory):
                    self.calls.append(item['name'])
                    if item['name']=='Estabelecimentos1.zip' and self.fail: raise ConnectionError('Interrupção simulada')
                    dst=Path(directory)/item['name'];shutil.copyfile(origin/item['name'],dst);return dst
            source=Source();source.fail=True
            release={'month':'2026-11','files':files,'fingerprint':'fixture','bytes':sum(f['size'] for f in files)}
            cache=RegistryCache(root)
            with self.assertRaises(ConnectionError):cache.build(source,release,{'GO'},set(),lambda _:None,{})
            self.assertIsNone(cache.latest({'GO'}))
            source.fail=False
            snap=cache.build(source,release,{'GO'},set(),lambda _:None,{})
            self.assertEqual(source.calls.count('Estabelecimentos0.zip'),1)
            cache=RegistryCache(root);snap=cache.latest({'GO'})
            cfg={'locations':[{'city':'Rio Verde','uf':'GO'}],'days':30}
            data,_=cache.select(snap,cfg,set(),date(2026,11,17));self.assertEqual(len(data),1)
            calls=len(source.calls)
            cfg['locations']=[{'city':'Jataí','uf':'GO'}]
            data,_=cache.select(snap,cfg,set(),date(2026,11,17));self.assertEqual(len(data),1)
            cfg['days']=180
            data,_=cache.select(snap,cfg,set(),date(2026,11,17));self.assertEqual(len(data),2)
            self.assertEqual(len(source.calls),calls)
            self.assertIsNone(cache.latest({'MT'}))
            # Expanding state coverage reprocesses retained archives without downloading.
            cache.build(source,release,{'GO','MT'},set(),lambda _:None,{})
            self.assertEqual(len(source.calls),calls)
            # A failed newer publication does not replace the complete usable snapshot.
            source.fail=True
            with self.assertRaises(ConnectionError):
                cache.build(source,{**release,'month':'2026-12','fingerprint':'new'}, {'GO'},set(),lambda _:None,{})
            self.assertEqual(cache.latest({'GO'})[0]['release']['month'],'2026-11')
            # The collector must apply a cached snapshot without even discovering online.
            path=root/'crm.sqlite3';store.init(path);store.save_settings(path,{**cfg,'automatic':False})
            collector=Collector(path,root)
            with patch.object(collector.source,'discover',side_effect=AssertionError('Network forbidden')):
                LOCK.acquire();collector._run(True)
            self.assertEqual(len(store.list_companies(path)),2)
            with store.connect(path) as db:
                self.assertEqual(db.execute('SELECT state FROM runs ORDER BY id DESC LIMIT 1').fetchone()[0],'concluído')

class HostedAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.secret='test-only-gateway-secret-1234567890123456'
        cls.server=create_server(0,cls.temp.name,public_origin='https://crm.example.test',gateway_token=cls.secret)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()
    def request(self,path,headers=None,data=None):
        base={'Host':'crm.example.test','X-DBS-Gateway':self.secret}
        base.update(headers or {})
        return urllib.request.urlopen(urllib.request.Request(self.base+path,headers=base,data=data))
    def test_public_binding_requires_gateway(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError): create_server(0,folder,host='0.0.0.0')
            with self.assertRaises(ValueError): create_server(0,folder,public_origin='http://crm.example.test',gateway_token=self.secret)
            with self.assertRaises(ValueError): create_server(0,folder,public_origin='https://crm.example.test',gateway_token='short')
    def test_all_data_requires_gateway(self):
        for path in ['/','/api/state','/api/export','/api/companies/DEMO-0001?mode=demo','/health']:
            with self.assertRaises(urllib.error.HTTPError) as ctx:self.request(path,{'X-DBS-Gateway':''})
            self.assertEqual(ctx.exception.code,403)
    def test_host_and_origin_are_enforced(self):
        for headers in [{'Host':'evil.example'},{'Origin':'https://evil.example'},{'Sec-Fetch-Site':'cross-site'}]:
            with self.assertRaises(urllib.error.HTTPError) as ctx:self.request('/api/state',headers)
            self.assertEqual(ctx.exception.code,403)
    def test_authenticated_access_still_requires_csrf(self):
        with self.request('/api/state',{'Origin':'https://crm.example.test'}) as response:
            state=json.load(response)
        self.assertTrue(state['hosted'])
        with self.assertRaises(urllib.error.HTTPError) as ctx:self.request('/api/settings',data=b'{}')
        self.assertEqual(ctx.exception.code,403)
        with self.request('/health') as response:self.assertEqual(json.load(response),{'ok':True})
