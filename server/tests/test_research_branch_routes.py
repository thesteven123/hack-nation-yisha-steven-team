"""Authenticated HTTP acceptance with real branch stores and local execution."""
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from research_dependencies import ResearchDependencies
from research_lab_routes import create_research_lab_router
from tests import test_codex_auth_isolated as auth_fixture
from tests.test_research_lab import source_request
from tests.test_research_branches import proposals

BASE = '/api/research/lab'
NATIVE = {'X-AgentsDock-Token': 'synthetic-native-token'}


class BranchRouteTests(unittest.TestCase):
    def setUp(self):
        fixture = auth_fixture.CodexAuthTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ns = fixture.ns
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dependencies = None
        def dependencies():
            if self.dependencies is None:
                self.dependencies = ResearchDependencies(self.root/'dependencies', self.root/'ideas', self.root/'lab')
            return self.dependencies
        app = FastAPI()
        app.middleware('http')(self.ns['require_agent_token'])
        app.include_router(create_research_lab_router(storage_root=self.root/'lab', idea_root=self.root/'ideas',
            authorize=self.ns['require_native_admin_control'], dependency_factory=dependencies))
        self.client = TestClient(app); self.addCleanup(self.client.close)
        self.sequence = 0

    def post(self, path, value, status=200):
        response = self.client.post(BASE+path, headers=NATIVE, json=value)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def create(self):
        item = self.post('', source_request())
        self.path = '/'+item['id']
        return item

    def enable(self, values=None):
        item = self.create()
        self.enable_request = {'expected_revision':item['revision'], 'idempotency_key':'enable', 'branches': values or proposals()}
        self.post(self.path+'/branches', self.enable_request)
        return self.view()

    def view(self):
        response = self.client.get(BASE+self.path+'/branches', headers=NATIVE)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def branch(item, name):
        return next(b for b in item['branch_set']['branches'] if b['id']==name)

    def action(self, bid, action, **fields):
        self.sequence += 1
        item = self.view()
        request = {'expected_branch_revision':self.branch(item,bid)['revision'],
            'expected_authority_epoch':item['branch_set']['authority_epoch'],
            'idempotency_key':'request-'+str(self.sequence), **fields}
        result = self.post(self.path+'/branches/'+bid+'/'+action,request)
        return result, request

    def test_native_authorization_precedes_body_and_storage_for_every_new_path(self):
        for suffix in ('/branches', '/branches/a/plan', '/branches/a/answers', '/branches/a/decision', '/branches/a/control', '/branches/a/run'):
            for headers, status in (({},401),({**NATIVE,'Origin':'https://example.invalid'},403),({**NATIVE,'Sec-Fetch-Mode':'cors'},403)):
                response=self.client.post(BASE+'/missing'+suffix,headers=headers,content='invalid')
                self.assertEqual(response.status_code,status)
                self.assertEqual(list(self.root.iterdir()),[])
        response=self.client.get(BASE+'/missing/branches',headers={})
        self.assertEqual(response.status_code,401)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_a_waits_b_runs_then_atomic_answers_unlock_a_and_fixed_c(self):
        item=self.enable()
        self.assertTrue(self.branch(item,'a')['questions'][0]['needs_answer'])
        self.assertFalse(self.branch(item,'a')['gate']['allowed'])
        self.assertTrue(self.branch(item,'b')['gate']['allowed'])
        self.assertFalse(self.branch(item,'c')['gate']['allowed'])
        self.action('b','run')
        self.assertEqual(self.view()['budget']['used_actions'],1)
        answered,_=self.action('a','answers',answers=[{'question_id':'q1','answer':'  Exact answer\n'}])
        self.assertEqual(answered['budget']['used_actions'],1)
        self.assertIsNone(self.branch(answered,'a')['current_plan'])
        self.action('a','plan')
        self.action('a','run')
        self.action('c','plan')
        c=self.branch(self.view(),'c')
        self.assertEqual(c['current_scope']['dependency_refs'][0]['branch_id'],'a')
        self.action('c','decision',selected_action_id=c['current_plan']['candidates'][0]['id'],feedback='Explicit local check')
        self.action('c','run')
        final=self.view()
        self.assertEqual(final['budget']['used_actions'],3)
        self.assertEqual([r['run']['branch_id'] for r in final['rounds']],['b','a','c'])
        self.assertTrue(all(r['run']['usage']['model_tokens']==0 for r in final['rounds']))
        self.assertEqual(self.branch(final,'a')['questions'][0]['answer']['answer'],'  Exact answer\n')

    def test_completed_key_replay_skips_current_admission_and_spends_no_more(self):
        self.enable()
        result, request=self.action('b','run')
        current=self.view()
        self.post(self.path+'/decision',{'expected_revision':current['revision'],'idempotency_key':'pause','kind':'defer','feedback':'Pause entire campaign'})
        with patch.object(self.dependencies,'branch_guard',side_effect=AssertionError('Replay must not use fresh admission')):
            replay=self.post(self.path+'/branches/b/run',request)
            self.assertEqual(replay,result)
            self.post(self.path+'/branches',self.enable_request)
        self.assertEqual(self.view()['budget']['used_actions'],1)
        altered={**request,'expected_branch_revision':request['expected_branch_revision']+1}
        error=self.post(self.path+'/branches/b/run',altered,409)
        self.assertEqual(error['detail']['code'],'idempotency_conflict')

    def test_legal_escaped_answer_batch_and_exact_transport_bound(self):
        values=proposals()
        values[0]['questions']=[{'id':'q'+str(i),'prompt':'Answer '+str(i),'required':True} for i in range(3)]
        item=self.enable(values)
        request={'expected_branch_revision':self.branch(item,'a')['revision'],
            'expected_authority_epoch':item['branch_set']['authority_epoch'],'idempotency_key':'escaped',
            'answers':[{'question_id':'q'+str(i),'answer':'x'+'\x01'*7990} for i in range(3)]}
        raw=json.dumps(request).encode()
        self.assertGreater(len(raw),128*1024)
        response=self.client.post(BASE+self.path+'/branches/a/answers',headers={**NATIVE,'Content-Type':'application/json'},content=raw)
        self.assertEqual(response.status_code,200,response.text)
        current=self.view()
        self.assertEqual(current['budget']['used_actions'],0)
        for q in self.branch(current,'a')['questions']:
            self.assertEqual(q['answer']['answer'],'x'+'\x01'*7990)
        response=self.client.post(BASE+self.path+'/branches/a/run',headers={**NATIVE,'Content-Type':'application/json'},content=b' '*(384*1024+1))
        self.assertEqual(response.status_code,413)
        self.assertEqual(self.view()['revision'],current['revision'])

    def test_legacy_get_is_read_only_and_branch_revision_cannot_alias_other_branch(self):
        item=self.create()
        with closing(sqlite3.connect(self.root/'lab/lab.sqlite3')) as db:
            before=db.execute('SELECT payload FROM campaigns WHERE id=?',(item['id'],)).fetchone()[0]
        self.assertNotIn('branch_set',self.view())
        with closing(sqlite3.connect(self.root/'lab/lab.sqlite3')) as db:
            after=db.execute('SELECT payload FROM campaigns WHERE id=?',(item['id'],)).fetchone()[0]
        self.assertEqual(before,after)
        self.post(self.path+'/branches',{'expected_revision':item['revision'],'idempotency_key':'enable','branches':proposals()})
        _,request=self.action('b','control',operation='pause',feedback='Only B')
        self.post(self.path+'/branches/b/control', {**request,'idempotency_key':'stale'},409)
        response=self.post(self.path+'/branches/not-found/run',{'expected_branch_revision':1,'expected_authority_epoch':1,'idempotency_key':'missing'},404)
        self.assertEqual(response['detail']['code'],'branch_not_found')


if __name__=='__main__':
    unittest.main()
