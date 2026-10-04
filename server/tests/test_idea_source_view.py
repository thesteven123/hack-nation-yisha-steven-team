"""Frozen original identity, ambiguity and truly readonly packet routes."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock,patch

import httpx
from fastapi import FastAPI,HTTPException
from idea_lab import IdeaStore
from idea_lab_routes import create_router
from idea_source_view import provenance_hash
from idea_source_archive import RawSourceArchive,RawArchiveError
import idea_literature as reader
from tests.test_idea_lab import brief
from tests.test_idea_literature import BODY


class SourceViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store=IdeaStore(self.root/'ideas')
        self.item=self.store.create({'idempotency_key':'source-identity','brief':brief(False)})
        def authorize(request):
            if request.headers.get('authorization')!='Bearer fixture':
                raise HTTPException(403,'Native control required')
        async def forbidden(*args):
            raise AssertionError('No provider work is allowed')
        self.app=FastAPI()
        self.app.include_router(create_router(storage_root=self.root/'ideas',authorize=authorize,generate=forbidden,source_library_root=self.root/'library'))
        await self.enterAsyncContext(self.app.router.lifespan_context(self.app))
        self.client=await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://fixture',headers={'authorization':'Bearer fixture'}))

    async def reading(self,key,retained=True, same_generation=False):
        url='https://example.org/'+key
        with patch.object(reader,'_download',AsyncMock(return_value=(BODY,'text/html',url))):
            if retained:
                value=await reader.retrieve_papers([{'url':url}],{},library_root=self.root/'library')
            else:
                with patch.object(RawSourceArchive,'retain',side_effect=RawArchiveError('raw_archive_full')):
                    value=await reader.retrieve_papers([{'url':url}],{},library_root=self.root/'library')
        if not same_generation:
            self.item,_=self.store.begin(self.item['id'],{'expected_revision':self.item['revision'],'idempotency_key':key},full_research=True)
        self.store.retrieval(self.item['id'],self.item['generation_id'],value)
        self.store.freeze_packet(self.item['id'],self.item['generation_id'],'literature' if not same_generation else 'ideas',value['sources'],{})
        source=value['sources'][0]
        return source, {'source_hash':hashlib.sha256(source['text'].encode()).hexdigest(),
            'generation_id':self.item['generation_id'],'provenance_hash':provenance_hash(source)}

    def finish(self):
        self.item=self.store.stop(self.item['id'],self.item['generation_id'],'interrupted','Bounded fixture only')

    def url(self,source,raw=False):
        return '/api/research/ideas/'+self.item['id']+'/papers/'+source['id']+('/raw' if raw else '')

    async def same_fetch_projection(self):
        original,identity=await self.reading('same-fetch')
        projected=copy.deepcopy(original)
        projected['provenance'].update(cache_hit=True,cache_freshness_seconds=123,
            discovery={'query':'additional cached discovery'},retrieved_at='2026-10-04T01:00:00Z')
        self.store.retrieval(self.item['id'],self.item['generation_id'],
            {'sources':[projected],'papers':[],'coverage_gaps':[]})
        self.store.freeze_packet(self.item['id'],self.item['generation_id'],'ideas',[projected],{})
        self.finish()
        return original,identity,projected,{**identity,'provenance_hash':provenance_hash(projected)}

    async def test_explicit_generation_resolves_same_fetch_cache_projections_without_mutation(self):
        original,old_identity,projected,new_identity=await self.same_fetch_projection()
        self.assertNotEqual(old_identity['provenance_hash'],new_identity['provenance_hash'])
        self.assertEqual(original['provenance']['raw_document'],projected['provenance']['raw_document'])
        with sqlite3.connect(self.store.path) as db:
            before='\n'.join(db.iterdump())
        packet=await self.client.get(self.url(original),params={k:v for k,v in new_identity.items() if k!='provenance_hash'})
        self.assertEqual(packet.status_code,200,packet.text)
        self.assertEqual(packet.json()['source'],projected)
        self.assertEqual(packet.json()['provenance_hash'],new_identity['provenance_hash'])
        for identity,source in ((old_identity,original),(new_identity,projected)):
            exact=await self.client.get(self.url(original),params=identity)
            self.assertEqual(exact.status_code,200,exact.text)
            self.assertEqual(exact.json()['source'],source)
            raw=(await self.client.get(self.url(original,True),params=identity)).json()
            self.assertEqual(raw['status'],'retained')
            self.assertEqual(raw['provenance_hash'],identity['provenance_hash'])
            self.assertEqual(base64.b64decode(raw['body_base64']),BODY)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual('\n'.join(db.iterdump()),before)

    async def test_unbound_same_fetch_projections_still_require_identity(self):
        original,identity,projected,new_identity=await self.same_fetch_projection()
        query={'source_hash':identity['source_hash']}
        packet=await self.client.get(self.url(original),params=query)
        self.assertEqual(packet.status_code,409,packet.text)
        raw=(await self.client.get(self.url(original,True),params=query)).json()
        self.assertEqual(raw['status'],'not_retained')
        self.assertEqual(raw['reason'],'ambiguous_original_reference')
        self.assertNotIn('body_base64',raw)

    async def test_same_generation_missing_original_does_not_acquire_retained_fetch(self):
        old,old_identity=await self.reading('old-missing',False)
        missing=copy.deepcopy(old)
        missing['provenance'].pop('raw_document')
        self.store.freeze_packet(self.item['id'],self.item['generation_id'],'review',[missing],{})
        new,new_identity=await self.reading('new-retained',same_generation=True)
        self.finish()
        packet=await self.client.get(self.url(old),params={k:v for k,v in old_identity.items() if k!='provenance_hash'})
        self.assertEqual(packet.status_code,409,packet.text)
        for source,identity,status in ((old,old_identity,'not_retained'),(new,new_identity,'retained')):
            raw=(await self.client.get(self.url(source,True),params=identity)).json()
            self.assertEqual(raw['status'],status)
            self.assertEqual(raw['provenance_hash'],identity['provenance_hash'])

    async def test_same_fetch_with_changed_core_identity_still_blocks(self):
        original,identity=await self.reading('core-identity')
        changed=copy.deepcopy(original)
        changed['provenance']['requested_url']='https://example.org/different-origin'
        self.store.freeze_packet(self.item['id'],self.item['generation_id'],'ideas',[changed],{})
        self.finish()
        packet=await self.client.get(self.url(original),params={k:v for k,v in identity.items() if k!='provenance_hash'})
        self.assertEqual(packet.status_code,409,packet.text)

    async def test_new_fetch_does_not_change_old_exact_receipt_and_new_one_downloads(self):
        old,old_identity=await self.reading('old',False); self.finish()
        new,new_identity=await self.reading('new'); self.finish()
        for identity in (old_identity,new_identity):
            packet=await self.client.get(self.url(old),params=identity)
            self.assertEqual(packet.status_code,200,packet.text)
            self.assertEqual(packet.json()['provenance_hash'],identity['provenance_hash'])
            self.assertEqual(packet.json()['generation_id'],identity['generation_id'])
        old_raw=(await self.client.get(self.url(old,True),params=old_identity)).json()
        new_raw=(await self.client.get(self.url(new,True),params=new_identity)).json()
        self.assertEqual(old_raw['status'],'not_retained')
        self.assertNotIn('body_base64',old_raw)
        self.assertEqual(base64.b64decode(new_raw['body_base64']),BODY)
        self.assertEqual(new_raw['provenance_hash'],new_identity['provenance_hash'])
        unbound=(await self.client.get(self.url(old,True),params={'source_hash':old_identity['source_hash']})).json()
        self.assertEqual(unbound['reason'],'ambiguous_original_reference')

    async def test_same_generation_same_text_different_fetch_requires_provenance(self):
        a,identity_a=await self.reading('first')
        b,identity_b=await self.reading('second',same_generation=True); self.finish()
        self.assertEqual(identity_a['source_hash'],identity_b['source_hash'])
        self.assertNotEqual(identity_a['provenance_hash'],identity_b['provenance_hash'])
        self.assertNotEqual(a['provenance']['raw_document']['fetch_id'],b['provenance']['raw_document']['fetch_id'])
        response=await self.client.get(self.url(a),params={k:v for k,v in identity_a.items() if k!='provenance_hash'})
        self.assertEqual(response.status_code,409)
        for identity in (identity_a,identity_b):
            raw=(await self.client.get(self.url(a,True),params=identity)).json()
            self.assertEqual(raw['status'],'retained')
            self.assertEqual(raw['provenance_hash'],identity['provenance_hash'])

    async def test_no_cross_generation_or_foreign_source_fallback(self):
        a,identity_a=await self.reading('first'); self.finish()
        b,identity_b=await self.reading('second'); self.finish()
        mixed={**identity_a,'generation_id':identity_b['generation_id']}
        self.assertEqual((await self.client.get(self.url(a,True),params=mixed)).status_code,404)
        mixed={**identity_a,'provenance_hash':'f'*64}
        self.assertEqual((await self.client.get(self.url(a,True),params=mixed)).status_code,404)
        other=self.store.create({'idempotency_key':'other','brief':brief(False)})
        foreign=self.url(a,True).replace(self.item['id'],other['id'])
        self.assertEqual((await self.client.get(foreign,params=identity_a)).status_code,404)

    async def test_paper_and_raw_get_leave_running_payload_and_all_tables_unchanged(self):
        source,identity=await self.reading('running')
        def dump():
            with sqlite3.connect(self.store.path) as db:
                return '\n'.join(db.iterdump())
        before=dump()
        for raw in (False,True):
            response=await self.client.get(self.url(source,raw),params=identity)
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(dump(),before)
        self.assertEqual(self.store.get(self.item['id'])['status'],'running')

    async def test_reading_missing_store_creates_no_files_or_controller(self):
        root=self.root/'missing'
        app=FastAPI()
        app.include_router(create_router(storage_root=root,authorize=lambda r:None,generate=AsyncMock()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
            for suffix in ('','/raw'):
                response=await client.get('/api/research/ideas/'+'a'*32+'/papers/s'+suffix,params={'source_hash':'b'*64})
                self.assertEqual(response.status_code,404)
        self.assertFalse(root.exists())


if __name__=='__main__':
    unittest.main()
