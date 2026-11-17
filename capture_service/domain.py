"""解释附件包接收范围与成员索引。"""
import json
from pathlib import PurePosixPath

def safe_member_path(value):
    """成员路径必须是相对、不越界、不含盘符或反斜杠的 POSIX 路径。"""
    if not isinstance(value,str) or not value:
        raise ValueError('成员路径非法')
    if '\\' in value:
        raise ValueError('成员路径越界')
    path=PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('成员路径越界')
    if value.endswith('/'):
        return path,True
    if len(path.parts)>=2 and path.parts[0].endswith(':'):
        raise ValueError('成员路径越界')
    return path,False

def decode(raw,receipt):
    d=json.loads(raw.decode('utf-8'))
    if 'ranges' in d:
        return [{'type':'bundle','id':d['bundle'],'version':d['revision'],
                 'payload':{'sha256':d['body_sha256'],'bytes':d['bytes'],'ranges':d['ranges']}}]
    if 'members' in d:
        for m in d['members']:
            safe_member_path(m['path'])
        return [{'type':'index','id':d['bundle'],'version':d['revision'],
                 'payload':{'notice':d['notice'],'members':d['members']}}]
    if 'notice' in d and 'references' in d:
        for ref in d['references']:
            safe_member_path(ref['path'])
        return [{'type':'notice','id':d['bundle'],'version':d['revision'],
                 'payload':{'notice':d['notice'],'lot':d.get('lot'),
                            'revision':d.get('revision'),'references':d['references']}}]
    raise ValueError('附件包资料非法')

def structured_lot(index_payload,notice_payload):
    """产品批号只来自清单/公告的结构化字段，绝不从解压文件名推断。"""
    candidates=[]
    if notice_payload and notice_payload.get('lot'):
        candidates.append(notice_payload['lot'])
    if index_payload and index_payload.get('lot'):
        candidates.append(index_payload['lot'])
    if not candidates:
        return None
    if len(set(candidates))>1:
        raise ValueError('清单与公告的产品批号不一致')
    return candidates[0]

def assess(rows):
    bundles={r['id']:r for r in rows if r['type']=='bundle'}
    indexes=[r for r in rows if r['type']=='index']
    reasons=['包体字节尚未核验']
    for index in indexes:
        if index['id'] not in bundles:reasons.append('缺少包体:'+index['id'])
        for member in index['payload']['members']:
            if not member['verified']:reasons.append('成员未核验:'+member['path'])
    return {'publishable':not reasons,'reasons':reasons,'bundle_ids':sorted(bundles)}
