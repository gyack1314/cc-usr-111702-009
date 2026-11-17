"""解释附件包接收范围与成员索引。"""
import json
from pathlib import PurePosixPath
def decode(raw,receipt):
    d=json.loads(raw.decode('utf-8'))
    if 'ranges' in d:
        return [{'type':'bundle','id':d['bundle'],'version':d['revision'],
                 'payload':{'sha256':d['body_sha256'],'bytes':d['bytes'],'ranges':d['ranges']}}]
    if 'members' in d:
        for m in d['members']:
            path=PurePosixPath(m['path'])
            if path.is_absolute() or '..' in path.parts:raise ValueError('成员路径越界')
        return [{'type':'index','id':d['bundle'],'version':d['revision'],
                 'payload':{'notice':d['notice'],'members':d['members']}}]
    raise ValueError('附件包资料非法')
def assess(rows):
    bundles={r['id']:r for r in rows if r['type']=='bundle'}
    indexes=[r for r in rows if r['type']=='index']
    reasons=['包体字节尚未核验']
    for index in indexes:
        if index['id'] not in bundles:reasons.append('缺少包体:'+index['id'])
        for member in index['payload']['members']:
            if not member['verified']:reasons.append('成员未核验:'+member['path'])
    return {'publishable':not reasons,'reasons':reasons,'bundle_ids':sorted(bundles)}
