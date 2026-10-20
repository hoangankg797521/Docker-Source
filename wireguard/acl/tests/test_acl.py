import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from acl import Authority, Conflict, Invalid, candidate, validate, route_coverage

class FakeFirewall:
    def __init__(self): self.actual=None;self.fail=False
    def fingerprint(self): return self.actual
    def apply(self,state):
        if self.fail: self.fail=False;raise RuntimeError('injected failure')
        import hashlib
        self.actual=hashlib.sha256(candidate(state).encode()).hexdigest();return self.actual

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.fw=FakeFirewall();self.a=Authority(self.temp.name+'/acl.db',self.fw)
        self.a.sync([dict(id='a',name='Test A',ip='10.255.66.2')],'admin')
    def tearDown(self): self.temp.cleanup()
    def body(self,**kw): return dict(mode='custom',rules=[dict(destination='192.168.50.50/32',protocol='tcp',ports='443',enabled=True)],revision=1,**kw)
    def test_new_deny(self): self.assertEqual(self.a.get('a')['policy']['mode'],'deny_all')
    def test_revision(self):
        self.a.save('a',self.body(),'one')
        with self.assertRaises(Conflict): self.a.save('a',self.body(),'two')
        self.assertEqual(self.a.get('a')['policy']['revision'],2)
    def test_failure_rollback(self):
        before=self.fw.actual;self.fw.fail=True
        with self.assertRaises(RuntimeError):self.a.save('a',self.body(),'admin')
        self.assertEqual(self.fw.actual,before);self.assertEqual(self.a.get('a')['policy']['revision'],1)
        self.assertEqual(self.a.audit('a')[0]['result'],'failed')
    def test_drift_recovery(self):
        self.fw.actual=None;self.assertEqual(self.a.status()['status'],'drift');self.a.reconcile();self.assertEqual(self.a.status()['status'],'applied')
    def test_persistence(self):
        self.a.save('a',self.body(),'admin');b=Authority(self.temp.name+'/acl.db',self.fw);self.assertEqual(b.get('a')['policy']['revision'],2)
    def test_invalid(self):
        for patch in [dict(destination='0.0.0.0/0'),dict(destination='::/0'),dict(destination='192.168.50.1/23'),dict(ports='0'),dict(ports='65536'),dict(ports='90-80'),dict(protocol='icmp'),dict(protocol='tcp; accept')]:
            b=self.body();b['rules'][0].update(patch)
            with self.assertRaises(Invalid,msg=str(patch)):validate(b)
    def test_duplicate_overlap(self):
        b=self.body();b['rules']*=2
        with self.assertRaises(Invalid):validate(b)
        b=self.body();b['rules'].append(dict(destination='192.168.50.0/23',protocol='any',ports='',enabled=True));self.assertTrue(validate(b)[1])
    def test_input_forward_default(self):
        self.a.save('a',self.body(),'admin');s,_=self.a.load();text=candidate(s)
        self.assertIn('hook input priority -10',text);self.assertIn('hook forward priority -10',text);self.assertIn('tcp dport 443',text);self.assertIn('unknown-client-or-ipv6-deny',text)
    def test_identity(self):
        with self.assertRaises(Invalid):self.a.sync([dict(id='b',name='bad',ip='10.255.77.2')],'admin')
        with self.assertRaises(KeyError):self.a.save('missing',self.body(),'admin')
    def test_route_coverage(self):
        policy=dict(mode='custom',rules=[dict(destination='192.168.50.0/23',enabled=True)])
        self.assertEqual(route_coverage(policy,dict(routes=['192.168.50.0/24']))['destinations'][0]['coverage'],'partial')
        self.assertEqual(route_coverage(policy,dict(routes=['192.168.50.0/24','192.168.51.0/24']))['destinations'][0]['coverage'],'covered')
        self.assertEqual(route_coverage(policy,dict(routes=['10.0.0.0/16']))['destinations'][0]['coverage'],'missing')
    def test_legacy(self):
        b=Authority(self.temp.name+'/legacy.db',FakeFirewall(),legacy_ids=['old']);b.sync([dict(id='old',name='Old',ip='10.255.66.2')],'migration');self.assertTrue(b.get('old')['policy']['legacy'])
        b.save('old',self.body(),'admin');self.assertFalse(b.get('old')['policy']['legacy'])

if __name__=='__main__':unittest.main(verbosity=2)
