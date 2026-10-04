"""Trash is an explicit, reversible mutation; it never erases evidence."""
import json
import sqlite3
import unittest

from idea_lab import IdeaError, IdeaStore
from research_lab import LabError, ResearchLabStore
from research_lab_inspection import ResearchLabInspector, verify_bundle
from tests import test_idea_lab_routes as idea_fixture
from tests import test_research_lab_routes as lab_fixture
from tests import test_research_model_routes as model_fixture
from tests import test_research_dependency_routes as dependency_fixture
from tests.test_research_lab_routes import creation, NATIVE, BASE


class IdeaTrashRoutes(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = idea_fixture.IdeaRoutesTests.asyncSetUp
    create = idea_fixture.IdeaRoutesTests.create

    async def test_delete_restore_restart_and_stale_generation(self):
        original = await self.create()
        path = self.base + '/' + original['id']
        response = await self.client.post(path + '/trash', json={'expected_revision': original['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        deleted = response.json()
        self.assertTrue(deleted['deleted_at'])
        self.assertEqual((await self.client.get(self.base)).json()['items'], [])
        self.assertEqual((await self.client.get(self.base + '/trash')).json()['items'][0]['id'], original['id'])
        self.assertEqual((await self.client.get(path)).status_code, 409)
        self.assertEqual((await self.client.post(path + '/generate', json={'expected_revision': deleted['revision'], 'idempotency_key': 'blocked'})).status_code, 409)
        self.assertEqual(IdeaStore(self.root).list()['items'], [])
        response = await self.client.post(path + '/restore', json={'expected_revision': deleted['revision']})
        self.assertEqual(response.status_code, 200, response.text)
        restored = response.json()
        self.assertIsNone(restored['deleted_at'])
        self.assertEqual(restored['brief'], original['brief'])
        self.assertEqual(restored['research'], original['research'])
        self.assertEqual(restored['revision'], original['revision'] + 2)
        self.assertEqual((await self.client.get(self.base + '/trash')).json()['items'], [])
        self.assertEqual((await self.client.post(path + '/trash', json={'expected_revision': original['revision']})).status_code, 409)

    async def test_running_research_and_authentication_block_deletion(self):
        original = await self.create()
        store = IdeaStore(self.root)
        running, _ = store.begin(original['id'], {'expected_revision': original['revision'], 'idempotency_key': 'running-fixture'})
        path = self.base + '/' + original['id'] + '/trash'
        response = await self.client.post(path, json={'expected_revision': running['revision']})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['detail']['code'], 'busy')
        self.assertEqual(store.get(original['id']), running)
        self.assertEqual((await self.client.post(path, headers={'authorization': 'invalid'}, json={})).status_code, 403)


class CampaignTrashRoutes(unittest.TestCase):
    setUp = lab_fixture.ResearchLabRouteTests.setUp
    make_client = lab_fixture.ResearchLabRouteTests.make_client
    post = lab_fixture.ResearchLabRouteTests.post

    def test_delete_restore_retains_results_exports_and_replay(self):
        original = self.post('', creation('trash'))
        path = '/' + original['id']
        deleted = self.post(path + '/trash', {'expected_revision': original['revision'], 'idempotency_key': 'trash-1'})
        self.assertTrue(deleted['deleted_at'])
        self.assertEqual(self.client.get(BASE, headers=NATIVE).json()['items'], [])
        self.assertEqual(self.client.get(BASE + '/trash', headers=NATIVE).json()['items'][0]['id'], original['id'])
        self.assertEqual(self.client.get(BASE + path, headers=NATIVE).status_code, 409)
        self.post(path + '/run', {'expected_revision': deleted['revision'], 'idempotency_key': 'blocked'}, 409)
        self.assertEqual(self.post(path + '/trash', {'expected_revision': original['revision'], 'idempotency_key': 'trash-1'}), deleted)
        export = self.client.get(BASE + path + '/export', headers=NATIVE)
        self.assertEqual(export.status_code, 200, export.text)
        verify_bundle(export.json())
        recreated = ResearchLabStore(self.root)
        self.assertEqual(recreated.list()['items'], [])
        self.assertEqual(ResearchLabInspector(self.root).list(trashed=True)['items'][0]['id'], original['id'])
        restored = self.post(path + '/restore', {'expected_revision': deleted['revision'], 'idempotency_key': 'restore-1'})
        self.assertIsNone(restored['deleted_at'])
        self.assertEqual(restored['revision'], original['revision'] + 2)
        for field in ('brief', 'input_artifact', 'rounds', 'claims', 'current_plan', 'budget'):
            self.assertEqual(restored[field], original[field], field)
        self.assertEqual(self.client.get(BASE + '/trash', headers=NATIVE).json()['items'], [])
        self.post(path + '/trash', {'expected_revision': original['revision'], 'idempotency_key': 'stale'}, 409)

    def test_active_native_role_blocks_delete_without_changes(self):
        original = self.post('', creation('running'))
        jobs = self.root.parent / 'model-jobs'
        jobs.mkdir()
        db = sqlite3.connect(jobs / 'model-jobs.sqlite3')
        try:
            db.execute('CREATE TABLE jobs(campaign_id TEXT,payload TEXT)')
            db.execute('INSERT INTO jobs VALUES (?,?)', (original['id'], json.dumps({'status': 'running'})))
            db.commit()
        finally:
            db.close()
        value = self.post('/' + original['id'] + '/trash', {'expected_revision': original['revision'], 'idempotency_key': 'busy'}, 409)
        self.assertEqual(value['detail']['code'], 'busy')
        self.assertEqual(ResearchLabStore(self.root).get(original['id']), original)
        denied = self.client.post(BASE + '/' + original['id'] + '/trash', headers={**NATIVE, 'Origin': 'https://example.invalid'}, json={})
        self.assertEqual(denied.status_code, 403)

    def test_creation_retry_cannot_reopen_trashed_research(self):
        request = creation('creation-replay')
        original = self.post('', request)
        path = '/' + original['id']
        deleted = self.post(path + '/trash', {'expected_revision': original['revision'], 'idempotency_key': 'delete-created'})
        response = self.post('', request, 409)
        self.assertEqual(response['detail']['code'], 'deleted')
        self.assertEqual(self.client.get(BASE, headers=NATIVE).json()['items'], [])
        self.post(path + '/restore', {'expected_revision': deleted['revision'], 'idempotency_key': 'restore-created'})
        self.assertEqual(self.post('', request), original)

    def test_trash_pagination_has_separate_scope(self):
        for index in range(3):
            item = self.post('', creation('page-' + str(index)))
            self.post('/' + item['id'] + '/trash', {'expected_revision': item['revision'], 'idempotency_key': 'delete-page-' + str(index)})
        inspector = ResearchLabInspector(self.root)
        page = inspector.list(trashed=True, limit=1)
        next_page = inspector.list(trashed=True, before=page['next_cursor'], limit=1)
        self.assertNotEqual(next_page['items'][0]['id'], page['items'][0]['id'])
        with self.assertRaises(LabError):
            inspector.list(before=page['next_cursor'], limit=1)


class TrashedModelAdmission(unittest.TestCase):
    setUp = model_fixture.ResearchModelRouteTests.setUp
    make_app = model_fixture.ResearchModelRouteTests.make_app
    request = model_fixture.ResearchModelRouteTests.request
    seed_planned = model_fixture.ResearchModelRouteTests.seed_planned

    def test_old_planned_job_and_new_jobs_cannot_start_after_deletion(self):
        planned = self.seed_planned()
        deleted = self.lab.set_deleted(self.snapshot['id'], {'expected_revision': self.snapshot['revision'], 'idempotency_key': 'delete-fixture'}, True)
        new = self.client.post(self.prefix, headers=NATIVE, json=self.request(expected_revision=deleted['revision'], idempotency_key='new-blocked'))
        self.assertEqual(new.status_code, 409, new.text)
        old = self.client.post(self.prefix + '/' + planned['id'] + '/start', headers=NATIVE, json={})
        self.assertEqual(old.status_code, 409, old.text)
        self.assertEqual(self.calls, [])


class TrashedDependencyReplay(unittest.TestCase):
    setUp = dependency_fixture.ResearchDependencyRouteTests.setUp
    post = dependency_fixture.ResearchDependencyRouteTests.post
    get = dependency_fixture.ResearchDependencyRouteTests.get

    def test_run_receipt_does_not_reopen_trash_or_reexecute_after_restore(self):
        original = self.post('', creation('dependency-replay'))
        path = '/' + original['id']
        request = {'expected_revision': original['revision'], 'idempotency_key': 'saved-run'}
        executed = self.post(path + '/run', request)
        deleted = self.post(path + '/trash', {'expected_revision': executed['revision'], 'idempotency_key': 'trash-after-run'})
        self.assertEqual(self.post(path + '/run', request, 409)['detail']['code'], 'deleted')
        self.assertEqual(self.get('')['items'], [])
        self.post(path + '/restore', {'expected_revision': deleted['revision'], 'idempotency_key': 'restore-after-run'})
        self.assertEqual(self.post(path + '/run', request), executed)
        current = self.get(path)
        self.assertEqual(current['rounds'], executed['rounds'])
        self.assertEqual(current['claims'], executed['claims'])
        self.assertEqual(current['input_artifact'], executed['input_artifact'])
        self.assertEqual(len(current['rounds']), 1)
        self.assertEqual(current['budget']['used_actions'], 1)


if __name__ == '__main__':
    unittest.main()
