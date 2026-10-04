"""Read frozen Idea source packets without operational recovery or migration."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from idea_lab import IdeaError, SCHEMA_VERSION


def provenance_hash(source):
    """Bind the complete source, including exact fetch/coverage/discovery receipt."""
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _same_retained_fetch(matches):
    """Only complete original-file identities can equate source projections."""
    identities = []
    for match in matches:
        source = match['source']
        provenance = source.get('provenance')
        if not isinstance(provenance, dict):
            return False
        raw = provenance.get('raw_document')
        if (not isinstance(raw, dict) or set(raw) != {'status', 'sha256', 'bytes', 'fetch_id', 'mime'}
                or raw['status'] != 'retained'
                or not isinstance(raw['fetch_id'], str) or not re.fullmatch(r'[0-9a-f]{32}', raw['fetch_id'])
                or not isinstance(raw['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', raw['sha256'])
                or type(raw['bytes']) is not int or raw['bytes'] <= 0
                or not isinstance(raw['mime'], str) or not raw['mime']):
            return False
        keys = ('content_hash', 'url', 'requested_url', 'source_version_id', 'parser_version')
        if (not isinstance(source.get('uri'), str) or not source['uri']
                or any(not isinstance(provenance.get(key), str) or not provenance[key] for key in keys)
                or provenance['content_hash'] != raw['sha256']):
            return False
        identities.append((raw, source['uri'], {key: provenance[key] for key in keys}))
    return bool(identities) and all(identity == identities[0] for identity in identities[1:])


def source_packet(root, session_id, source_id, source_hash=None, *, generation_id=None, provenance_hash_value=None):
    if not isinstance(session_id, str) or not re.fullmatch(r'[0-9a-f]{32}', session_id):
        raise IdeaError('not_found', 'Idea session was not found')
    if generation_id is not None and (not isinstance(generation_id, str) or not re.fullmatch(r'[0-9a-f]{32}', generation_id)):
        raise IdeaError('invalid_request', 'Provide an exact generation identity')
    for value in (source_hash, provenance_hash_value):
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)):
            raise IdeaError('invalid_request', 'Provide an exact lowercase packet hash')
    root = Path(root)
    path = root/'ideas.sqlite3'
    if root.is_symlink() or path.is_symlink():
        raise IdeaError('storage_error', 'Idea source storage cannot use symlinks')
    if not path.is_file():
        raise IdeaError('not_found', 'Idea source archive was not found')

    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise IdeaError('unsupported_schema', 'Unsupported Idea source archive schema')
            row=db.execute('SELECT data FROM sessions WHERE id=?',(session_id,)).fetchone()
            if row is None:
                raise IdeaError('not_found', 'Idea session was not found')
            current=json.loads(row['data'])
            matches=[]

            def consider(source, snapshot, round_index=None):
                if generation_id is not None and snapshot.get('generation_id') != generation_id:
                    return
                digest=hashlib.sha256(source['text'].encode('utf-8')).hexdigest()
                if source_hash is not None and digest != source_hash:
                    return
                descriptor=provenance_hash(source)
                if provenance_hash_value is not None and descriptor != provenance_hash_value:
                    return
                provenance=source.get('provenance')
                if isinstance(provenance,dict) and 'raw_document' in provenance and not isinstance(provenance['raw_document'],dict):
                    raise IdeaError('corrupt_source', 'The saved original-file reference is malformed')
                papers=(snapshot.get('research') or {}).get('papers',[])
                result={'source':source, 'paper':next((p for p in papers if p.get('id')==source_id),None),
                    'packet_hash':digest, 'provenance_hash':descriptor,
                    'generation_id':snapshot.get('generation_id'), 'revision':snapshot['revision'],
                    'archived':snapshot.get('generation_id') != current.get('generation_id')}
                if round_index is not None:
                    result['round']=round_index
                matches.append(result)
                if len(matches)>2000:
                    raise IdeaError('source_packet_limit', 'Too many matching historical packets; specify the exact generation and provenance')

            for source in (current.get('research') or {}).get('sources',[]):
                if source.get('id')==source_id:
                    consider(source,current)

            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'stage_packets' in tables:
                rows=db.execute("SELECT p.generation_id,p.round,s.value AS source FROM stage_packets p "
                    "JOIN generations g ON g.id=p.generation_id,json_each(p.sources) s "
                    "WHERE g.session_id=? AND json_extract(s.value,'$.id')=? "
                    "AND (? IS NULL OR p.generation_id=?) ORDER BY g.rowid DESC,p.round DESC,p.stage",
                    (session_id,source_id,generation_id,generation_id))
                for packet in rows:
                    version=db.execute("SELECT data FROM versions WHERE session_id=? "
                        "AND json_extract(data,'$.generation_id')=? ORDER BY revision DESC LIMIT 1",
                        (session_id,packet['generation_id'])).fetchone()
                    if version is None:
                        raise IdeaError('storage_error', 'Frozen packet generation has no saved snapshot')
                    consider(json.loads(packet['source']),json.loads(version['data']),packet['round'])
            # The saved source may predate stage packets. These rows are read
            # only; neither current state nor historical provenance is upgraded.
            for field in ('research.sources','brief.sources'):
                rows=db.execute("SELECT v.data,s.value AS source FROM versions v,json_each(v.data,?) s "
                    "WHERE v.session_id=? AND json_extract(s.value,'$.id')=? "
                    "AND (? IS NULL OR json_extract(v.data,'$.generation_id')=?) ORDER BY v.revision DESC",
                    ('$.'+field,session_id,source_id,generation_id,generation_id))
                for row in rows:
                    consider(json.loads(row['source']),json.loads(row['data']))
            if not matches:
                raise IdeaError('not_found', 'Exact source provenance was not found in this session and generation')
            if (source_hash is not None and len({m['provenance_hash'] for m in matches})>1
                    and not (generation_id is not None and _same_retained_fetch(matches))):
                raise IdeaError('ambiguous_source', 'This text hash has multiple original fetch identities; select exact saved provenance')
            return matches[0]
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        raise IdeaError('storage_error', 'Saved source packet could not be read or verified') from None
