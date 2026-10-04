"""STAGING authenticated HTTP branch roles with a fake generator, no provider."""
import copy
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from research_model_routes import create_research_model_router
from tests import test_codex_auth_isolated as auth_fixture
from tests import test_research_branch_controller as core_fixture
from tests.test_research_model_jobs import output_for

NATIVE={'X-AgentsDock-Token':'synthetic-native-token'}


class BranchModelRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture=core_fixture.BranchControllerTests('runTest'); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups); self.fixture.enable()
        auth=auth_fixture.CodexAuthTests(); auth.setUp(); self.addCleanup(auth.doCleanups)
        self.auth=auth.ns; self.calls=[]
        async def generate(role,packet,on_event,**options):
            self.calls.append(copy.deepcopy(packet))
            return {'output':output_for(role,packet),'usage':{'total_tokens':17,'complete':True},
                    'provider':{'resolved_model':'fixture-model','model_resolution':'thread_start'}}
        app=FastAPI(); app.middleware('http')(self.auth['require_agent_token'])
        app.include_router(create_research_model_router(storage_root=self.fixture.root/'model-jobs',lab_root=self.fixture.root/'lab',
            authorize=self.auth['require_native_admin_control'],native_options=lambda:{'executable':'fake'},generate=generate,
            dependency_factory=lambda:self.fixture.deps))
        self.client=self.enterContext(TestClient(app))
        self.prefix='/api/research/lab/'+self.fixture.item['id']+'/model-jobs'

    def request(self,bid='b',key='planner'):
        item=self.fixture.view(); branch=self.fixture.branch(bid,item)
        return {'role':'planner','idempotency_key':key,'branch_id':bid,'expected_branch_revision':branch['revision'],
                'expected_authority_epoch':item['branch_set']['authority_epoch']}

    def create(self,body):
        result=self.client.post(self.prefix,headers=NATIVE,json=body)
        self.assertEqual(result.status_code,200,result.text)
        item=result.json()
        response=self.client.get(self.prefix+'/'+item['id']+'/wait',headers=NATIVE)
        self.assertEqual(response.status_code,200,response.text)
        return response.json()

    def test_auth_scoped_current_list_filter_and_foreign_campaign(self):
        self.assertEqual(self.client.post(self.prefix,json=self.request()).status_code,401)
        b=self.create(self.request())
        self.assertEqual(b['status'],'completed')
        self.assertEqual(b['scope_status'],'current')
        self.fixture.answer()
        a=self.create(self.request('a','a-plan'))
        page=self.client.get(self.prefix+'?branch_id=b&limit=1',headers=NATIVE).json()
        self.assertEqual([x['id'] for x in page['items']],[b['id']])
        self.assertEqual(page['quota']['used'],2)
        empty=self.client.get(self.prefix+'?branch_id=c',headers=NATIVE).json()
        self.assertEqual(empty['items'],[])
        foreign=self.client.get(self.prefix.replace(self.fixture.item['id'],'campaign_'+'0'*32)+'/'+a['id'],headers=NATIVE)
        self.assertEqual(foreign.status_code,404)

    def test_stale_pure_replay_never_admits_or_invokes_a_second_generator(self):
        body=self.request(); job=self.create(body)
        self.fixture.invoke('control_branch','b',operation='pause',feedback='Pause this branch')
        response=self.client.post(self.prefix,headers=NATIVE,json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['id'],job['id'])
        self.assertEqual(len(self.calls),1)
        current=self.client.get(self.prefix+'/'+job['id'],headers=NATIVE).json()
        self.assertEqual(current['scope_status'],'stale')
        changed={**body,'idempotency_key':'new-after-pause','expected_branch_revision':self.fixture.branch('b')['revision']}
        self.assertEqual(self.client.post(self.prefix,headers=NATIVE,json=changed).status_code,409)
        self.assertEqual(len(self.calls),1)

    def test_precise_branch_request_boundary_and_legacy_root_requires_branch(self):
        bodies=[{**self.request(),'packet':{}},{**self.request(),'expected_revision':1},
                {**self.request(),'expected_branch_revision':True},{**self.request(),'branch_id':'../other'}]
        for value in bodies:
            self.assertEqual(self.client.post(self.prefix,headers=NATIVE,json=value).status_code,400)
        legacy={'role':'planner','idempotency_key':'root','expected_revision':self.fixture.item['revision']}
        self.assertEqual(self.client.post(self.prefix,headers=NATIVE,json=legacy).status_code,409)
        for query in ('?branch_id=../a','?branch_id=a&branch_id=b','?branch_id=b&limit=0','?scope=b'):
            self.assertEqual(self.client.get(self.prefix+query,headers=NATIVE).status_code,400)
        self.assertEqual(self.calls,[])


if __name__=='__main__':
    unittest.main()
