"""Read-only structural audit of saved Bemula runs; no LLM/API calls.

Run with .venv/bin/python for optional FAISS inspection. Semantic review packets
contain full stores, not retrieved candidates; they are inputs for future review,
not automatically scored semantic-forgetting results.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3


def read_jsonl(path):
    return [json.loads(line) for line in path.open() if line.strip()]


def terms(text):
    # Independently implement the persisted FTS representation (including CJK).
    result = []
    for token in re.findall(r"[\u3400-\u9fff]+|[^\W_\u3400-\u9fff]+", text.lower()):
        if all("\u3400" <= c <= "\u9fff" for c in token) and len(token) > 1:
            result.extend(token[i:i + 2] for i in range(len(token) - 1))
        else:
            result.append(token)
    return " ".join(result)


def inspect_database(path):
    # Do not use SQLiteStore: its initialization can migrate/rebuild the source.
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def rows(table):
            return [dict(r) for r in db.execute(f'SELECT * FROM "{table}"')]

        raw = rows("conversation_turns")
        atoms = rows("atomic_memories")
        evidence = rows("memory_evidence")
        experiences = rows("experience_memories")
        profiles = rows("user_profiles")
        atom_by_id = {r['memory_id']: r for r in atoms}
        raw_by_id = {r['turn_id']: r for r in raw}
        counts = Counter(raw=len(raw), atomic=len(atoms), evidence=len(evidence),
                         experience=len(experiences), profile=len(profiles))
        counts.update({"status_" + s: n for s, n in Counter(r['status'] for r in atoms).items()})
        issues = Counter()
        examples = {}

        def flag(name, key):
            issues[name] += 1
            examples.setdefault(name, [])
            if len(examples[name]) < 5:
                examples[name].append(key)

        for row in db.execute("PRAGMA foreign_key_check"):
            flag("foreign_key_violation", list(row))
        for e in evidence:
            source = raw_by_id.get(e['turn_id'])
            if source is None or e['memory_id'] not in atom_by_id:
                flag("dangling_evidence", e['evidence_id'])
            elif source['session_id'] != e['session_id']:
                flag("evidence_session_mismatch", e['evidence_id'])
            if e['span_start'] is not None or e['span_end'] is not None:
                counts['bounded_spans'] += 1
                start, end = e['span_start'], e['span_end']
                if source is None or start is None or end is None or not (0 <= start < end <= len(source['content'])):
                    flag("out_of_bounds_span", e['evidence_id'])
        referenced = {e['memory_id'] for e in evidence}
        counts['atomic_without_evidence'] = len(atom_by_id.keys() - referenced)
        # Absence of evidence is reported, not equated with semantic falsehood.
        for table, key, canonical, queue in (
            ('atomic_memories_fts', 'memory_id', atom_by_id, 'fts_pending'),
            ('conversation_turns_fts', 'turn_id', raw_by_id, 'raw_fts_pending'),
        ):
            pending = {r[key] for r in rows(queue)} if queue in tables else set()
            counts[queue] = len(pending)
            seen = Counter()
            for r in rows(table):
                seen[r[key]] += 1
                original = canonical.get(r[key])
                if original is None or r['content'] != terms(original['content']):
                    flag(table + ('_pending_difference' if r[key] in pending else '_unexpected_difference'), r[key])
            for key_id in canonical.keys() - seen.keys():
                flag(table + ('_pending_missing' if key_id in pending else '_unexpected_missing'), key_id)
            for key_id, n in seen.items():
                if n > 1:
                    flag(table + '_duplicate', key_id)
        vectors = {r['vector_id']: r['memory_id'] for r in rows('memory_vector_ids')}
        embeddings = rows('memory_embeddings')
        counts['embeddings'] = len(embeddings)
        for r in embeddings:
            original = atom_by_id.get(vectors.get(r['vector_id']))
            if original is None:
                flag('orphan_embedding', r['vector_id'])
            elif r['content'] != original['content']:
                flag('stale_embedding_text', r['vector_id'])
            if original is not None and original['status'] != 'ACTIVE':
                flag('inactive_embedding', r['vector_id'])
            if len(r['vector']) != 4 * r['dimension']:
                flag('invalid_vector_length', r['vector_id'])
        snapshots = rows('dense_snapshots')
        counts['dense_snapshots'] = len(snapshots)
        revisions = {r['user_id']: r['revision'] for r in rows('retrieval_revisions')}
        for r in snapshots:
            if r['revision'] != revisions.get(r['user_id']):
                flag('stale_dense_revision', r['user_id'])
            try:
                import faiss
                import numpy as np
            except ImportError:
                counts['dense_not_deserialized'] += 1
                continue
            try:
                index = faiss.deserialize_index(np.frombuffer(r['index_data'], dtype='uint8'))
                actual = faiss.vector_to_array(index.id_map).tolist()
                expected = {v for v, key_id in vectors.items()
                            if key_id in atom_by_id and atom_by_id[key_id]['status'] == 'ACTIVE'
                            and atom_by_id[key_id]['user_id'] == r['user_id']}
                if set(actual) != expected or len(actual) != len(expected):
                    flag('dense_id_mismatch', r['user_id'])
                embedded = {e['vector_id']: e for e in embeddings if e['embedding_key'] == r['embedding_key']}
                for vector_id in actual:
                    e = embedded.get(vector_id)
                    if e is None or e['dimension'] != index.d:
                        flag('dense_embedding_mismatch', vector_id)
                    else:
                        vector = np.frombuffer(e['vector'], dtype='float32')
                        norm = np.linalg.norm(vector.astype('float64'))
                        if norm == 0 or not np.isfinite(vector).all():
                            flag('invalid_dense_vector', vector_id)
                        elif not np.allclose(index.reconstruct(int(vector_id)), vector / norm,
                                             rtol=1e-5, atol=1e-6):
                            flag('dense_vector_mismatch', vector_id)
                counts['dense_deserialized'] += 1
            except Exception as error:
                flag('dense_decode_error', type(error).__name__)
        groups = rows('experience_groups')
        sources = rows('experience_sources')
        counts['experience_groups'] = len(groups)
        counts['experience_sources'] = len(sources)
        counts['empty_experience_groups'] = len({r['group_id'] for r in groups} - {r['group_id'] for r in sources})
        counts['experiences_without_groups'] = len({r['experience_id'] for r in experiences} - {r['experience_id'] for r in groups})
        records = []
        for layer, key, values in [('RAW', 'turn_id', raw), ('ATOMIC', 'memory_id', atoms),
                                   ('EXPERIENCE', 'experience_id', experiences), ('PROFILE', 'profile_id', profiles)]:
            records.extend({'layer': layer, 'id': r[key], **r} for r in values)
        for record in records:
            if record['layer'] == 'ATOMIC':
                record['evidence'] = [e for e in evidence if e['memory_id'] == record['id']]
            elif record['layer'] == 'EXPERIENCE':
                record['source_groups'] = [
                    {**g, 'sources': [s for s in sources if s['group_id'] == g['group_id']]}
                    for g in groups if g['experience_id'] == record['id']]
        return {'path': str(path), 'counts': dict(counts), 'issues': dict(issues),
                'examples': examples}, records


def audit_run(run, out, dialogues, profiles):
    meta = json.loads((run / 'run_meta.json').read_text())
    metrics = json.loads((run / 'metrics.json').read_text())
    items = read_jsonl(run / 'items.jsonl')
    result = {'run': str(run), 'status': meta['status'], 'by_type': {}, 'databases': []}
    for kind, block in metrics['by_forget_type'].items():
        requests = [r for r in metrics['by_request'].values() if r['forget_type'] == kind]
        result['by_type'][kind] = {'requests': len(requests), 'FE': block['FE'],
                                  'AR': block['AR']['average'],
                                  'strict_pass': sum(r['strict_pass'] for r in requests)}
    event_errors, reports, routing = Counter(), [], []
    packets = out / run.name
    packets.mkdir()
    for directory in sorted((run / 'states').iterdir()):
        events = read_jsonl(directory / 'events.jsonl')
        instance_id = events[0]['instance_id']
        for e in events:
            if e['operation'] == 'system_error':
                event_errors[(e.get('variant'), e.get('stage'), e.get('error_type'))] += 1
        # Exclude relearning when matching request timestamps and counting reports.
        forget_events = []
        for e in events:
            if e.get('variant') == 'forget':
                if e['operation'] == 'end_variant':
                    break
                forget_events.append(e)
        observed = [e for e in forget_events if e['operation'] == 'observe']
        reports.extend(e['forget'] for e in observed if e.get('forget'))
        dialogue = dialogues[instance_id]
        for request in dialogue['forget_requests']:
            pos = request['position']
            turn = dialogue['sessions'][pos['session_index']]['turns'][pos['turn_index']]
            candidates = [e for e in observed if e.get('timestamp', '').replace('+00:00', 'Z') == turn['timestamp']]
            routing.append({'instance_id': instance_id, 'request_id': request['request_id'],
                            'forget_type': request['forget_type'],
                            'observed_actions': [e.get('routing', {}).get('action') for e in candidates],
                            'has_report': any(e.get('forget') for e in candidates),
                            'reports': [e['forget'] for e in candidates if e.get('forget')],
                            'official_request_metrics': metrics['by_request'][request['request_id']]})
        packet = {'instance_id': instance_id, 'requests': dialogue['forget_requests'],
                  'facts': profiles[dialogue['profile_id']]['facts'], 'stores': [],
                  'instruction': 'Unscored review material. Inspect full stores and inferential combinations. Final snapshots are before probes/relearning, not immediately after each request. Later dialogue and other requests can confound before/final comparisons. Do not treat all profile facts as observed non-target facts.'}
        paths = [('control_final', directory / 'control.snapshot.sqlite3'),
                 ('forget_final', directory / 'forget.snapshot.sqlite3')]
        paths += [('before_request', p) for p in sorted((directory / 'forget' / 'before-forget').glob('*.sqlite3'))]
        for phase, path in paths:
            audit, records = inspect_database(path)
            audit.update(instance_id=instance_id, phase=phase)
            result['databases'].append(audit)
            packet['stores'].append({'phase': phase, 'path': str(path), 'records': records})
        # Keep model outputs separate so reviewers can audit storage blind to QA.
        (packets / (instance_id.replace(':', '_') + '.json')).write_text(json.dumps(packet, ensure_ascii=False, indent=2) + '\n')
    result['request_routing'] = routing
    result['report_counts'] = dict(Counter('verified' if r.get('verified') else 'unverified' for r in reports))
    result['committed_reports'] = sum(bool(r.get('committed')) for r in reports)
    result['verification_scopes'] = dict(Counter(r.get('verification_scope', 'unspecified') for r in reports))
    result['event_errors'] = [{'variant': k[0], 'stage': k[1], 'error_type': k[2], 'events': n} for k, n in sorted(event_errors.items())]
    # Record-level events are not independent failed requests.
    result['request_report_missing'] = [{k: v for k, v in r.items() if k != 'official_request_metrics'}
                                        for r in routing if not r['has_report']]
    for phase in ('control_final', 'forget_final', 'before_request'):
        dbs = [d for d in result['databases'] if d['phase'] == phase]
        result[phase] = {'databases': len(dbs),
                         'counts': dict(sum((Counter(d['counts']) for d in dbs), Counter())),
                         'issues': dict(sum((Counter(d['issues']) for d in dbs), Counter()))}
    # QA remains available in source items.jsonl; no answer-based storage scores.
    result['scored_items'] = len(items)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, action='append', required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    dialogues_file = args.dataset / 'dialogues.jsonl'
    digest = hashlib.sha256(dialogues_file.read_bytes()).hexdigest()
    dialogues = {r['instance_id']: r for r in read_jsonl(dialogues_file)}
    profiles = {r['profile_id']: r for r in read_jsonl(args.dataset / 'profiles.jsonl')}
    for run in args.run:
        meta = json.loads((run / 'run_meta.json').read_text())
        expected = [v for k, v in meta['configuration']['inputs'].items() if k.endswith('/dialogues.jsonl')]
        if expected != [digest]:
            raise ValueError(f'Dataset dialogue fingerprint does not match {run}')
    args.out.mkdir(parents=True, exist_ok=False)
    results = [audit_run(run, args.out, dialogues, profiles) for run in args.run]
    (args.out / 'audit.json').write_text(json.dumps({'dialogues_sha256': digest, 'runs': results}, ensure_ascii=False, indent=2) + '\n')
    for r in results:
        print(json.dumps({k: v for k, v in r.items() if k not in ('databases', 'request_routing')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
