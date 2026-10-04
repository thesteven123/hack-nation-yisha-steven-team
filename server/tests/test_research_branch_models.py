"""STAGING branch native ownership with fake generator only."""
import asyncio
import copy
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from research_lab import ResearchLabStore, LabError
from research_dependencies import ResearchDependencies
from research_model_jobs import ResearchModelJobs, ModelJobError
from tests import test_research_branch_controller as core_fixture
from tests.test_research_model_jobs import output_for


class BranchModelTests(unittest.IsolatedAsyncioTestCase):
    setUp = core_fixture.BranchControllerTests.setUp
    enable = core_fixture.BranchControllerTests.enable
    view = core_fixture.BranchControllerTests.view
    branch = core_fixture.BranchControllerTests.branch
    invoke = core_fixture.BranchControllerTests.invoke
    answer = core_fixture.BranchControllerTests.answer

    async def asyncSetUp(self):
        self.enable()
        self.calls = []
        self.entered = asyncio.Event(); self.release = asyncio.Event()
        async def generate(role, packet, on_event, **options):
            self.calls.append(copy.deepcopy(packet)); self.entered.set()
            await self.release.wait()
            return {'output': output_for(role, packet), 'usage': {'total_tokens': 71, 'complete': True},
                    'provider': {'resolved_model': 'fixture-model', 'model_resolution': 'thread_start'}}
        @contextmanager
        def guard(cid, bid):
            try:
                with self.deps.branch_guard(cid) as dependencies:
                    with self.lab.branch_scope_guard(cid, bid, dependency_state=dependencies) as view:
                        yield view
            except LabError as exc:
                raise ModelJobError(exc.code, exc.message) from None
        self.models = ResearchModelJobs(self.root / 'models', generate=generate, branch_guard=guard)
        self.addAsyncCleanup(self.models.shutdown)

    def prepare(self, bid, key, role='planner'):
        item=self.view(); branch=self.branch(bid, item)
        request={'campaign_id':item['id'], 'branch_id':bid, 'role':role, 'idempotency_key':key,
                 'expected_branch_revision':branch['revision'], 'expected_authority_epoch':item['branch_set']['authority_epoch']}
        return self.models.prepare(request, resolve_snapshot=self.lab.get), request

    async def start(self, item):
        return await self.models.start(item['id'], executable='fake', current_revision=lambda cid:self.lab.get(cid)['revision'])

    async def test_unrelated_a_answer_keeps_b_role_current_and_uses_shared_quota(self):
        item, request=self.prepare('b','b-plan')
        await self.start(item); await self.entered.wait()
        self.invoke('answer_branch','a',answers=[{'question_id':'q1','answer':'Actual scoped answer'}])
        self.release.set()
        await self.models.jobs[item['id']]
        final=self.models.get(item['id'])
        self.assertEqual(final['status'],'completed')
        self.assertEqual(final['branch_id'],'b')
        self.assertEqual(final['usage']['total_tokens'],71)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.models.prepare(request,resolve_snapshot=self.lab.get),final)
        self.assertEqual(self.models.list(self.item['id'])['quota']['used'],1)
        packet=self.models.artifact(final['id'],final['packet_ref'])['content']
        self.assertEqual(packet['branch_scope'],item['branch_scope'])
        self.assertIn('campaign_goal',packet)

    async def test_changed_same_branch_cannot_publish_but_preserves_usage_raw(self):
        item,_=self.prepare('b','before-select')
        await self.start(item); await self.entered.wait()
        action=self.branch('b')['current_plan']['selected_action_id']
        self.invoke('decide_branch','b',selected_action_id=action,feedback='Actual changed preference')
        self.release.set(); await self.models.jobs[item['id']]
        final=self.models.get(item['id'])
        self.assertEqual(final['status'],'stale')
        self.assertIsNone(final['output'])
        self.assertIsNotNone(final['raw_output_ref'])
        self.assertEqual(final['usage']['total_tokens'],71)

    async def test_two_independent_native_owners_keep_one_campaign_quota(self):
        self.answer()
        a,_=self.prepare('a','a'); b,_=self.prepare('b','b')
        await self.start(a); await self.start(b)
        self.assertEqual(len(self.models.jobs),2)
        self.assertEqual(self.models.list(self.item['id'])['quota']['used'],2)
        await self.models.cancel(a['id'])
        self.assertEqual(self.models.get(a['id'])['status'],'cancelled')
        self.assertEqual(self.models.get(b['id'])['status'],'running')
        self.release.set(); await self.models.jobs[b['id']]
        self.assertEqual(self.models.get(b['id'])['status'],'completed')
        self.assertEqual(self.models.list(self.item['id'],branch_id='a')['items'][0]['id'],a['id'])
        self.assertEqual(self.models.list(self.item['id'],branch_id='b')['items'][0]['id'],b['id'])
        self.assertEqual(self.models.list(self.item['id'])['quota']['used'],2)

    async def test_pending_local_correction_blocks_only_matching_source(self):
        self.answer()
        item=self.lab.get(self.item['id'])
        inputs=self.lab.artifact(item['id'],item['input_artifact'])['content']
        inputs['sources'][0]['text']='Changed source A.'
        self.deps.prepare_lab_correction(item['id'],{'expected_revision':item['revision'],'idempotency_key':'pending',
            'reason':'Frozen durable intent','inputs':inputs},'s1')
        with self.assertRaises(ModelJobError) as caught:
            self.prepare('a','a-pending')
        self.assertEqual(caught.exception.code,'branch_blocked')
        b,_=self.prepare('b','b-unrelated')
        self.assertEqual(b['status'],'planned')
        self.assertEqual(self.models.list(item['id'])['quota']['reserved'],1)


if __name__ == '__main__':
    unittest.main()
