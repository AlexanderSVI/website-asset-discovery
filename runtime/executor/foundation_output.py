"""D-078 evidence provenance and bounded approved-field CSV serialization."""
import csv
import json
import unicodedata
from pathlib import Path

FIELDS = {
    'Sources.csv': ('schema_version','run_id','source_id','source_type','requested_url','final_url','normalized_final_url','observation_status'),
    'Asset_Inventory.csv': ('schema_version','run_id','asset_id','asset_category','asset_type','primary_url','observation_status','evidence_ids_json'),
    'Evidence.csv': ('schema_version','run_id','evidence_id','source_id','observed_at','evidence_type','discovery_method','observation_result','technical_attributes_json'),
    'Occurrences.csv': ('schema_version','run_id','occurrence_id','asset_id','placement_page_url','page_area','component','usage_context','observation_status','evidence_ids_json'),
    'Relationships.csv': ('schema_version','run_id','relationship_id','source_object_type','source_object_id','relationship_type','target_object_type','target_object_id','target_url','confirmation_status','evidence_ids_json'),
}
RELATION_TYPES = frozenset({'LINKS_TO','USES','CONTAINS','EMBEDS','REDIRECTS_TO','CANONICAL_OF','POSSIBLE_DUPLICATE_OF'})


def attach_evidence(ids, source, asset, history, *, confirmed, observed_at, evidence_type, discovery_method, observation_result):
    """Caller supplies a directly supported Asset observation, never inferred here."""
    if source['run_id'] != ids.run_id or asset['run_id'] != ids.run_id:
        raise ValueError('run mismatch')
    if confirmed is not True:
        raise ValueError('explicit confirmation required')
    if not any(type(attempt.get('http_status')) is int and attempt.get('url') == source['final_url']
               for attempt in history['attempts']):
        raise ValueError('confirmed HTTP response required')
    if not all((observed_at, evidence_type, discovery_method, observation_result)):
        raise ValueError('explicit evidence required')
    attempts = [{k: attempt[k] for k in ('url','http_status','error_kind','reason','forced') if k in attempt}
                for attempt in history['attempts']]
    attrs = {'attempts': attempts, 'redirects': history['redirects'], 'reason': history['reason']}
    evidence = {'schema_version':source['schema_version'], 'run_id':ids.run_id,
                'evidence_id':ids.next('EVD'), 'source_id':source['source_id'],
                'observed_at':observed_at, 'evidence_type':evidence_type,
                'discovery_method':discovery_method, 'observation_result':observation_result,
                'technical_attributes_json':json.dumps(attrs,ensure_ascii=False)}
    refs = json.loads(asset['evidence_ids_json'])
    refs.append(evidence['evidence_id'])
    asset['evidence_ids_json'] = json.dumps(refs,ensure_ascii=False)
    return evidence


def _csv_value(value):
    if value is None:
        return ''
    if not isinstance(value,str):
        return value
    i=0
    while i<len(value) and (value[i]==' ' or unicodedata.category(value[i])=='Cc'):
        i+=1
    if i<len(value) and value[i] in '=+-@':
        return "'"+value
    return value


def write_csv(directory, *, sources, assets, evidence, relationships=(), occurrences=()):
    """Serialize existing approved fields; do not invent a Source->Asset entity."""
    source_ids={source['source_id'] for source in sources}
    evidence_ids={item['evidence_id'] for item in evidence}
    if len(source_ids)!=len(sources) or len(evidence_ids)!=len(evidence):
        raise ValueError('duplicate identifiers')
    if any(item['source_id'] not in source_ids for item in evidence):
        raise ValueError('missing evidence source')
    source_runs={source['source_id']:source['run_id'] for source in sources}
    evidence_runs={item['evidence_id']:item['run_id'] for item in evidence}
    required={
        'Sources.csv':('schema_version','run_id','source_id','source_type','requested_url','observation_status'),
        'Asset_Inventory.csv':('schema_version','run_id','asset_id','asset_category','asset_type','observation_status','evidence_ids_json'),
        'Evidence.csv':('schema_version','run_id','evidence_id','source_id','observed_at','evidence_type','discovery_method','observation_result'),
        'Occurrences.csv':('schema_version','run_id','occurrence_id','asset_id','placement_page_url','observation_status','evidence_ids_json'),
        'Relationships.csv':('schema_version','run_id','relationship_id','source_object_type','source_object_id','relationship_type','target_object_type','confirmation_status','evidence_ids_json'),
    }
    rows={'Sources.csv':sources,'Asset_Inventory.csv':assets,'Evidence.csv':evidence,'Relationships.csv':relationships,'Occurrences.csv':occurrences}
    for filename, values in rows.items():
        for row in values:
            if any(not row.get(key) for key in required[filename]):
                raise ValueError('missing mandatory field in '+filename)
    if any(item['run_id'] != source_runs[item['source_id']] for item in evidence):
        raise ValueError('evidence source run mismatch')
    for asset in assets:
        refs=json.loads(asset['evidence_ids_json'])
        if not isinstance(refs,list) or not set(refs).issubset(evidence_ids):
            raise ValueError('invalid evidence references')
        if any(evidence_runs[ref] != asset['run_id'] for ref in refs):
            raise ValueError('asset evidence run mismatch')
        if not asset['asset_type']:
            raise ValueError('asset_type is required for output')
    asset_runs={asset['asset_id']:asset['run_id'] for asset in assets}
    occurrence_ids=set()
    for occurrence in occurrences:
        refs=json.loads(occurrence['evidence_ids_json'])
        if (occurrence['occurrence_id'] in occurrence_ids or
                asset_runs.get(occurrence['asset_id']) != occurrence['run_id'] or
                not isinstance(refs,list) or not refs or
                any(ref not in evidence_runs or evidence_runs[ref] != occurrence['run_id'] for ref in refs)):
            raise ValueError('invalid occurrence references')
        occurrence_ids.add(occurrence['occurrence_id'])
    if any(item['relationship_type'] not in RELATION_TYPES for item in relationships):
        raise ValueError('unapproved relationship type')
    for relation in relationships:
        refs=json.loads(relation['evidence_ids_json'])
        if (not relation.get('target_object_id') and not relation.get('target_url')) or not isinstance(refs,list) or not refs or not set(refs).issubset(evidence_ids):
            raise ValueError('invalid relationship target or evidence')
    root=Path(directory)
    for filename, fields in FIELDS.items():
        with (root/filename).open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields,lineterminator='\r\n',extrasaction='raise')
            writer.writeheader()
            for row in rows[filename]:
                writer.writerow({key:_csv_value(value) for key,value in row.items()})
