"""STAGING real controller/SQLite execution. No native/provider operations."""
import copy
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from research_lab import ResearchLabStore, LabError
from research_dependencies import ResearchDependencies
from tests.test_research_lab import source_request
from tests.test_research_branches import proposals
from research_lab_inspection import ResearchLabInspector, verify_bundle, restore_campaign
import research_backup as backup


class BranchControllerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lab = ResearchLabStore(self.root / 'lab')
        self.deps = ResearchDependencies(self.root / 'dependencies', self.root / 'ideas', self.root / 'lab')
        self.request = source_request()
        self.item = self.lab.create(self.request)
        self.sequence = 0

    def enable(self, changes=None):
        value = {'expected_revision': self.item['revision'], 'idempotency_key': 'branches', 'branches': changes or proposals()}
        with self.deps.branch_guard(self.item['id']) as dependencies:
            self.item = self.lab.enable_branches(self.item['id'], value, dependency_state=dependencies)
        return self.view()

    def view(self):
        with self.deps.branch_guard(self.item['id'], write=False) as dependencies:
            return self.lab.branch_snapshot(self.item['id'], dependency_state=dependencies)

    def branch(self, name, snapshot=None):
        return next(b for b in (snapshot or self.view())['branch_set']['branches'] if b['id'] == name)

    def invoke(self, method, name, **extra):
        self.sequence += 1
        current = self.view()
        request = {'expected_branch_revision': self.branch(name, current)['revision'],
            'expected_authority_epoch': current['branch_set']['authority_epoch'], 'idempotency_key': str(self.sequence), **extra}
        with self.deps.branch_guard(self.item['id']) as dependencies:
            self.item = getattr(self.lab, method)(self.item['id'], name, request, dependency_state=dependencies)
        return self.view(), request

    def answer(self):
        self.invoke('answer_branch', 'a', answers=[{'question_id': 'q1', 'answer': '  Exact current scope, please.\n'}])
        return self.invoke('plan_branch', 'a')

    def error(self, code, function):
        with self.assertRaises(LabError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def test_waiting_a_does_not_block_b_real_machine_run_and_c_waits(self):
        item = self.enable()
        self.assertFalse(self.branch('a')['gate']['allowed'])
        self.assertTrue(self.branch('b')['gate']['allowed'])
        self.assertFalse(self.branch('c')['gate']['allowed'])
        after, request = self.invoke('run_branch', 'b')
        self.assertEqual(after['budget']['used_actions'], 1)
        self.assertEqual(len(after['rounds']), 1)
        self.assertEqual(after['rounds'][0]['run']['branch_id'], 'b')
        self.assertEqual(after['rounds'][0]['observation']['data']['match_count'], 1)
        self.assertEqual(after['rounds'][0]['run']['usage']['model_tokens'], 0)
        self.assertFalse(self.branch('a')['gate']['allowed'])
        self.assertEqual(self.lab.run_branch(self.item['id'], 'b', request), self.item)
        self.assertEqual(self.lab.get(self.item['id'])['budget']['used_actions'], 1)

    def test_answer_a_enables_actual_dependency_bound_c_and_single_ledger(self):
        self.enable(); self.invoke('run_branch', 'b'); self.answer()
        self.invoke('run_branch', 'a')
        self.invoke('plan_branch', 'c')
        branch = self.branch('c')
        self.assertEqual(branch['current_scope']['dependency_refs'][0]['branch_id'], 'a')
        after, _ = self.invoke('run_branch', 'c')
        self.assertEqual(after['budget']['used_actions'], 3)
        self.assertEqual([r['index'] for r in after['rounds']], [1, 2, 3])
        self.assertEqual(len({r['run']['id'] for r in after['rounds']}), 3)
        self.assertTrue(all(not r['run']['is_independent_replicate'] for r in after['rounds']))
        run = after['rounds'][1]['run']
        packet = self.lab.artifact(after['id'], run['dispatch_artifact'])['content']['task_packet']
        self.assertEqual(packet['relevant_state']['branch_answers'][0]['answer']['answer'], '  Exact current scope, please.\n')

    def test_correct_a_only_blocks_a_and_c_while_b_scope_stays_current(self):
        self.enable(); self.answer(); self.invoke('run_branch', 'a')
        before = self.branch('b')['current_scope']
        item = self.lab.get(self.item['id'])
        inputs = self.lab.artifact(item['id'], item['input_artifact'])['content']
        inputs['sources'][0]['text'] = 'Corrected source A; no old quoted text.'
        self.item = self.lab.correct_inputs(item['id'], {'expected_revision': item['revision'], 'idempotency_key': 'correct', 'reason': 'Actual local correction', 'inputs': inputs})
        self.assertEqual(self.branch('b')['current_scope'], before)
        self.assertFalse(self.branch('a')['gate']['allowed'])
        self.assertFalse(self.branch('c')['gate']['allowed'])
        self.invoke('run_branch', 'b')

    def test_root_pause_resume_fences_old_plan_without_affecting_old_replay(self):
        self.enable(); before, request = self.invoke('run_branch', 'b')
        original_receipt = copy.deepcopy(self.item)
        item = self.lab.get(self.item['id'])
        self.item = self.lab.decide(item['id'], {'expected_revision': item['revision'], 'idempotency_key': 'pause', 'kind': 'defer', 'feedback': 'Pause all branches'})
        self.assertTrue(all(not b['gate']['allowed'] for b in self.view()['branch_set']['branches']))
        self.assertEqual(self.lab.run_branch(item['id'], 'b', request), original_receipt)

    def test_no_unknown_dependency_authority_and_old_root_run_requires_branch(self):
        self.enable()
        snapshot = self.lab.branch_snapshot(self.item['id'])
        self.assertTrue(all(not b['gate']['allowed'] for b in snapshot['branch_set']['branches']))
        item = self.lab.get(self.item['id'])
        self.error('branch_required', lambda: self.lab.run(item['id'], {'expected_revision': item['revision'], 'idempotency_key': 'root-run'}))

    def test_two_concurrent_branches_share_one_last_action_and_round(self):
        request = source_request('one-action'); request['budget'] = {'max_actions': 1, 'max_rounds': 1}
        self.item = self.lab.create(request)
        values = proposals()
        for value in values:
            value.update(questions=[], depends_on=[])
        self.enable(values)
        current=self.view(); cid=current['id']; epoch=current['branch_set']['authority_epoch']
        def run(bid):
            body={'expected_branch_revision':self.branch(bid,current)['revision'], 'expected_authority_epoch':epoch, 'idempotency_key':'race-'+bid}
            try:
                with self.deps.branch_guard(cid) as dependency:
                    return self.lab.run_branch(cid,bid,body,dependency_state=dependency)['budget']['used_actions']
            except LabError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(run,['b','c']))
        self.assertCountEqual(results,[1,'budget_exhausted'])
        final=self.lab.get(cid)
        self.assertEqual(final['budget']['used_actions'],1)
        self.assertEqual(final['budget']['reserved_actions'],0)
        self.assertEqual(len(final['rounds']),1)

    def test_old_saved_run_and_raw_version_bytes_unchanged_after_explicit_enable(self):
        body={'expected_revision':self.item['revision'], 'idempotency_key':'old-run'}
        original=self.lab.run(self.item['id'],body)
        self.item=original
        with self.lab._db() as db:
            before=[tuple(row) for row in db.execute('SELECT revision,payload FROM versions WHERE campaign_id=? ORDER BY revision',(self.item['id'],))]
        self.enable()
        self.assertEqual(self.lab.run(self.item['id'],body),original)
        with self.lab._db() as db:
            after=[tuple(row) for row in db.execute('SELECT revision,payload FROM versions WHERE campaign_id=? AND revision<=? ORDER BY revision',(self.item['id'],original['revision']))]
        self.assertEqual(before,after)
        self.assertEqual(self.item['branch_set']['historical_unassigned_run_ids'],[original['rounds'][0]['run']['id']])

    def test_exact_selection_and_answers_frozen_in_actual_dispatch(self):
        self.enable(); self.answer()
        plan=self.branch('a')['current_plan']
        self.invoke('decide_branch','a',selected_action_id=plan['selected_action_id'],feedback='Keep my exact branch choice.\n')
        expected=self.branch('a')['current_scope']
        after,_=self.invoke('run_branch','a')
        record=after['rounds'][-1]
        packet=self.lab.artifact(after['id'],record['run']['dispatch_artifact'])['content']['task_packet']
        self.assertEqual(packet['branch_scope'],expected)
        self.assertEqual(packet['actual_selection']['reason'],'Keep my exact branch choice.\n')
        self.assertEqual(packet['relevant_state']['branch_answers'][0]['answer']['answer'],'  Exact current scope, please.\n')

    def test_all_branch_records_export_and_whole_restore_without_reexecution(self):
        self.enable(); self.invoke('run_branch','b'); self.answer(); original,request=self.invoke('run_branch','a')
        receipt=copy.deepcopy(self.item)
        bundle=ResearchLabInspector(self.root/'lab').export_campaign(original['id'])
        self.assertEqual(verify_bundle(bundle)['runs'],2)
        clone=self.root.parent/(self.root.name+'-campaign')
        self.addCleanup(lambda: __import__('shutil').rmtree(clone,ignore_errors=True))
        restore_campaign(bundle,clone)
        self.assertEqual(ResearchLabStore(clone).run_branch(original['id'],'a',request),receipt)
        with backup.research_service_guard(self.root):
            pass
        destination=self.root.parent/(self.root.name+'-backup')
        restored=self.root.parent/(self.root.name+'-restore')
        for path in (destination,restored):
            self.addCleanup(lambda path=path: __import__('shutil').rmtree(path,ignore_errors=True))
        manifest=backup.backup_research(self.root,destination)
        backup.restore_research(destination,restored)
        store=ResearchLabStore(restored/'lab')
        self.assertEqual(store.run_branch(original['id'],'a',request),receipt)
        self.assertEqual(store.get(original['id'])['budget']['used_actions'],2)
        with backup._read(restored/next(name for name in manifest['databases'] if name.endswith('/corrections.sqlite3'))) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM scope_registrations WHERE campaign_id=?',(original['id'],)).fetchone()[0],3)

    def test_pause_preserves_machine_result_currency_but_blocks_new_authority(self):
        self.enable(); self.invoke('run_branch','b')
        self.assertTrue(self.branch('b')['latest_result']['current'])
        self.invoke('control_branch','b',operation='pause',feedback='Pause new work, preserve the existing measurement')
        paused=self.branch('b')
        self.assertTrue(paused['latest_result']['current'])
        self.assertFalse(paused['gate']['allowed'])
        self.assertEqual(paused['scope_status'],'blocked')


if __name__ == '__main__':
    unittest.main()
