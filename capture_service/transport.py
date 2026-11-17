"""记录冻结 HTTP 交互，限制请求与授权作用域。

冻结步骤可携带 request.range（Range 头原文）与 response.headers，
用于重放分段采集（206 Partial Content）。旧步骤缺省这些字段时行为不变。
"""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

def origin(url):
    parsed=urlsplit(url)
    if parsed.scheme not in ('http','https') or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError('来源地址非法')
    return parsed.scheme+'://'+parsed.netloc.lower()

class Replay:
    def __init__(self, manifest_path):
        document=json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        self.root=Path(manifest_path).parent.resolve()
        self.allowed=set(document['allowed_origins'])
        self.steps=document['steps']
        self.position=0
        self.history=[]
    def request(self, method, url, body=b'', credential_scope='', range_header=None):
        if origin(url) not in self.allowed or credential_scope and credential_scope!=origin(url):
            raise ValueError('请求或凭据越界')
        if self.position>=len(self.steps):raise EOFError('没有冻结响应')
        row=self.steps[self.position]
        expected=row['request']
        if range_header!=expected.get('range'):
            raise ValueError('Range 请求与冻结交互不符')
        if (method,url,hashlib.sha256(body).hexdigest()) != (expected['method'],expected['url'],expected['body_sha256']):
            raise ValueError('请求与冻结交互不符')
        path=(self.root/row['response']['file']).resolve()
        if not path.is_relative_to(self.root):raise ValueError('响应文件越界')
        raw=path.read_bytes()
        if len(raw)!=row['response']['bytes'] or hashlib.sha256(raw).hexdigest()!=row['response']['sha256']:
            raise ValueError('响应字节身份不符')
        self.position+=1
        receipt={'method':method,'url':url,'status':row['response']['status'],
                 'body_sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),
                 'credential_scope':credential_scope,'step':self.position,
                 'range_header':range_header,
                 'response_headers':{k.lower():v for k,v in row['response'].get('headers',{}).items()}}
        self.history.append(receipt)
        return raw,receipt
