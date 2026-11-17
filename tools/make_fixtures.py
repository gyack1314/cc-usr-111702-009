"""生成 capture_service 采集器所需的合成冻结交互夹具。

纯标准库、确定性输出：为每个场景构造 ZIP、切成带 Content-Range 的分片，
并写出带逐响应 sha256 的清单。样例为合成资料，不访问真实监管站。

用法：python tools/make_fixtures.py
"""
import hashlib
import io
import json
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'
EMPTY_SHA=hashlib.sha256(b'').hexdigest()


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def build_zip(entries):
    """entries: [(path, bytes)]；非流式写入，避免数据描述符歧义。

    固定 ZipInfo 时间戳与外部属性，保证夹具字节确定性。
    """
    import warnings
    buf=io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')  # 重复成员夹具需要真实产生重复条目
        with zipfile.ZipFile(buf,'w',compression=zipfile.ZIP_DEFLATED) as zf:
            for path,blob in entries:
                info=zipfile.ZipInfo(filename=path,date_time=(2026,1,1,0,0,0))
                info.external_attr=(0o644<<16)
                info.compress_type=zipfile.ZIP_DEFLATED
                zf.writestr(info,blob)
    return buf.getvalue()


def split_ranges(body,cut_points):
    points=sorted(set([0,*cut_points,len(body)]))
    return [(points[i],points[i+1],body[points[i]:points[i+1]])
            for i in range(len(points)-1)]


def write_step(manifest_dir,steps,origin,bundle,part,method,blob,range_header=None,
               status=200,headers=None,extra_url=None):
    fname=f'{len(steps):02d}-{part}.bin'
    (manifest_dir/fname).write_bytes(blob)
    url=extra_url if extra_url is not None else f'{origin}/bundles/{bundle}/{part}'
    step={'request':{'method':method,'url':url,'body_sha256':EMPTY_SHA},
          'response':{'file':fname,'status':status,'bytes':len(blob),
                      'sha256':sha(blob)}}
    if range_header is not None:
        step['request']['range']=range_header
    if headers:
        step['response']['headers']=headers
    steps.append(step)


def head_doc(bundle,revision,body,notice=None):
    return json.dumps({'bundle':bundle,'revision':revision,
                       'body_sha256':sha(body),'bytes':len(body),
                       'ranges':[{'start':0,'end':len(body)}]},
                      ensure_ascii=False,indent=2).encode('utf-8')


def index_doc(bundle,revision,notice,members,lot=None):
    payload={'bundle':bundle,'revision':revision,'notice':notice,'members':members}
    if lot:
        payload['lot']=lot
    return json.dumps(payload,ensure_ascii=False,indent=2).encode('utf-8')


def notice_doc(bundle,revision,notice,lot,references):
    return json.dumps({'bundle':bundle,'revision':revision,'notice':notice,
                       'lot':lot,'revision':revision,
                       'references':references},ensure_ascii=False,indent=2).encode('utf-8')


def member_entry(path,text,pages):
    blob=(f'%PDF-1.4 synthetic {text}\n'.encode('utf-8')
          +bytes(range(256))*2+b'PAGES=%d\n'%pages)
    return path,blob,{'path':path,'bytes':len(blob),'sha256':sha(blob),
                      'verified':False,'pages':pages}


def make_manifest(name,steps,allowed=('https://files.example.test',)):
    d=DATA/name
    d.mkdir(parents=True,exist_ok=True)
    for old in d.glob('*'):
        old.unlink()
    return d


def write_manifest(name,steps,allowed=('https://files.example.test',)):
    (DATA/name/'manifest.json').write_text(
        json.dumps({'allowed_origins':list(allowed),'steps':steps},
                   ensure_ascii=False,indent=2),encoding='utf-8')


def scenario_happy():
    bundle,revision,notice,lot='B-9','r1','notice-27','LOT-2026-001'
    m1=member_entry('reports/R-17.pdf','report-17',8)
    m2=member_entry('reports/R-18.pdf','report-18',12)
    body=build_zip([(m1[0],m1[1]),(m2[0],m2[1])])
    cuts=[len(body)//3,2*len(body)//3]
    steps=[]
    d=make_manifest('happy',steps)
    write_step(d,steps,'https://files.example.test',bundle,'head','GET',
               head_doc(bundle,revision,body))
    for start,end,blob in split_ranges(body,cuts):
        rng=f'bytes={start}-{end-1}'
        write_step(d,steps,'https://files.example.test',bundle,'body','GET',blob,
                   range_header=rng,status=206,
                   headers={'Content-Range':f'bytes {start}-{end-1}/{len(body)}'})
    members=[{k:v for k,v in m[2].items()} for m in (m1,m2)]
    write_step(d,steps,'https://files.example.test',bundle,'index','GET',
               index_doc(bundle,revision,notice,members,lot=lot))
    refs=[{'path':m[2]['path'],'sha256':m[2]['sha256'],'pages':m[2]['pages']}
          for m in (m1,m2)]
    write_step(d,steps,'https://files.example.test',bundle,'notice','GET',
               notice_doc(bundle,revision,notice,lot,refs))
    write_manifest('happy',steps)


def scenario_resume_prefix():
    """只含前两个分片，模拟网络中断；happy 清单用于续采剩余部分。"""
    bundle,revision='B-9','r1'
    full=json.loads((DATA/'happy'/'manifest.json').read_text(encoding='utf-8'))
    part_steps=[s for s in full['steps'] if s['request'].get('range')]
    head=full['steps'][0]
    steps=[]
    d=make_manifest('resume-prefix',steps)
    # 头部文档原样复制（文件也要复制到本目录）
    src=DATA/'happy'/head['response']['file']
    hb=src.read_bytes()
    write_step(d,steps,'https://files.example.test',bundle,'head','GET',hb)
    for s in part_steps[:2]:
        blob=(DATA/'happy'/s['response']['file']).read_bytes()
        write_step(d,steps,'https://files.example.test',bundle,'body','GET',blob,
                   range_header=s['request']['range'],status=206,
                   headers=s['response'].get('headers'))
    write_manifest('resume-prefix',steps)


def scenario_duplicate_member():
    bundle,revision,notice,lot='B-10','r1','notice-31','LOT-2026-002'
    m1=member_entry('reports/R-20.pdf','dup',4)
    body=build_zip([(m1[0],m1[1]),(m1[0],b'other-content')])
    steps=[]
    d=make_manifest('dup-member',steps)
    write_step(d,steps,'https://files.example.test',bundle,'head','GET',
               head_doc(bundle,revision,body))
    for start,end,blob in split_ranges(body,[len(body)//2]):
        write_step(d,steps,'https://files.example.test',bundle,'body','GET',blob,
                   range_header=f'bytes={start}-{end-1}',status=206,
                   headers={'Content-Range':f'bytes {start}-{end-1}/{len(body)}'})
    write_step(d,steps,'https://files.example.test',bundle,'index','GET',
               index_doc(bundle,revision,notice,[m1[2]],lot=lot))
    write_step(d,steps,'https://files.example.test',bundle,'notice','GET',
               notice_doc(bundle,revision,notice,lot,
                          [{'path':m1[2]['path'],'sha256':m1[2]['sha256'],'pages':4}]))
    write_manifest('dup-member',steps)


def scenario_traversal():
    bundle,revision,notice,lot='B-11','r1','notice-33','LOT-2026-003'
    m1=member_entry('reports/R-21.pdf','ok',3)
    body=build_zip([(m1[0],m1[1]),('../evil.bin',b'pwn')])
    steps=[]
    d=make_manifest('traversal',steps)
    write_step(d,steps,'https://files.example.test',bundle,'head','GET',
               head_doc(bundle,revision,body))
    for start,end,blob in split_ranges(body,[len(body)//2]):
        write_step(d,steps,'https://files.example.test',bundle,'body','GET',blob,
                   range_header=f'bytes={start}-{end-1}',status=206,
                   headers={'Content-Range':f'bytes {start}-{end-1}/{len(body)}'})
    # 清单本身也含越界路径，decode 阶段即应拒绝
    write_step(d,steps,'https://files.example.test',bundle,'index','GET',
               index_doc(bundle,revision,notice,
                         [m1[2],{'path':'../evil.bin','bytes':3,'sha256':sha(b'pwn'),
                                 'verified':False,'pages':1}],lot=lot))
    write_manifest('traversal',steps)


def scenario_versions():
    """同名 B-12 的两个版次 r1/r2，分片交错到达，必须分开处置。"""
    bundle,notice,lot='B-12','notice-40','LOT-2026-004'
    m_r1=member_entry('reports/R-30.pdf','v1',5)
    m_r2=member_entry('reports/R-30.pdf','v2-different',5)
    b1=build_zip([(m_r1[0],m_r1[1])])
    b2=build_zip([(m_r2[0],m_r2[1])])
    steps=[]
    d=make_manifest('versions',steps)
    origin='https://files.example.test'
    write_step(d,steps,origin,bundle,'head','GET',head_doc(bundle,'r1',b1),
               extra_url=f'{origin}/bundles/{bundle}/r1/head')
    s1,_,blob1=split_ranges(b1,[len(b1)//2])[0]
    write_step(d,steps,origin,bundle,'body','GET',blob1,
               range_header=f'bytes=0-{len(blob1)-1}',status=206,
               headers={'Content-Range':f'bytes 0-{len(blob1)-1}/{len(b1)}'},
               extra_url=f'{origin}/bundles/{bundle}/r1/body')
    write_step(d,steps,origin,bundle,'head','GET',head_doc(bundle,'r2',b2),
               extra_url=f'{origin}/bundles/{bundle}/r2/head')
    write_step(d,steps,origin,bundle,'index','GET',
               index_doc(bundle,'r1',notice,[m_r1[2]],lot=lot),
               extra_url=f'{origin}/bundles/{bundle}/r1/index')
    for start,end,blob in split_ranges(b2,[len(b2)//2]):
        write_step(d,steps,origin,bundle,'body','GET',blob,
                   range_header=f'bytes={start}-{end-1}',status=206,
                   headers={'Content-Range':f'bytes {start}-{end-1}/{len(b2)}'},
                   extra_url=f'{origin}/bundles/{bundle}/r2/body')
    write_step(d,steps,origin,bundle,'index','GET',
               index_doc(bundle,'r2',notice,[m_r2[2]],lot=lot),
               extra_url=f'{origin}/bundles/{bundle}/r2/index')
    # r1 剩余分片晚到
    _,_,rest=split_ranges(b1,[len(b1)//2])[1]
    write_step(d,steps,origin,bundle,'body','GET',rest,
               range_header=f'bytes={len(blob1)}-{len(b1)-1}',status=206,
               headers={'Content-Range':f'bytes {len(blob1)}-{len(b1)-1}/{len(b1)}'},
               extra_url=f'{origin}/bundles/{bundle}/r1/body')
    write_step(d,steps,origin,bundle,'notice','GET',
               notice_doc(bundle,'r1',notice,lot,
                          [{'path':m_r1[2]['path'],'sha256':m_r1[2]['sha256'],'pages':5}]),
               extra_url=f'{origin}/bundles/{bundle}/r1/notice')
    write_step(d,steps,origin,bundle,'notice','GET',
               notice_doc(bundle,'r2',notice,lot,
                          [{'path':m_r2[2]['path'],'sha256':m_r2[2]['sha256'],'pages':5}]),
               extra_url=f'{origin}/bundles/{bundle}/r2/notice')
    write_manifest('versions',steps)


def scenario_splice_attack():
    """同区间旧包片段重传且字节不同：重叠冲突必须拒绝。"""
    bundle,revision,notice,lot='B-13','r1','notice-41','LOT-2026-005'
    m1=member_entry('reports/R-40.pdf','splice',6)
    body=build_zip([(m1[0],m1[1])])
    cut=len(body)//2
    parts=split_ranges(body,[cut])
    steps=[]
    d=make_manifest('splice',steps)
    write_step(d,steps,'https://files.example.test',bundle,'head','GET',
               head_doc(bundle,revision,body))
    s,e,first=parts[0]
    write_step(d,steps,'https://files.example.test',bundle,'body','GET',first,
               range_header=f'bytes=0-{len(first)-1}',status=206,
               headers={'Content-Range':f'bytes 0-{len(first)-1}/{len(body)}'})
    # 攻击者把旧包的不同片段按相同区间重放
    evil=bytearray(first)
    evil[0]^=0xFF
    write_step(d,steps,'https://files.example.test',bundle,'body','GET',bytes(evil),
               range_header=f'bytes=0-{len(evil)-1}',status=206,
               headers={'Content-Range':f'bytes 0-{len(evil)-1}/{len(body)}'})
    write_manifest('splice',steps)


def scenario_body_hash_mismatch():
    """分片齐全但包级摘要与冻结清单不符（清单声明了别的身份）。"""
    bundle,revision,notice,lot='B-14','r1','notice-42','LOT-2026-006'
    m1=member_entry('reports/R-41.pdf','wronghash',2)
    body=build_zip([(m1[0],m1[1])])
    steps=[]
    d=make_manifest('body-mismatch',steps)
    fake_head=json.dumps({'bundle':bundle,'revision':revision,
                          'body_sha256':'00'*32,'bytes':len(body),
                          'ranges':[{'start':0,'end':len(body)}]},
                         ensure_ascii=False,indent=2).encode('utf-8')
    write_step(d,steps,'https://files.example.test',bundle,'head','GET',fake_head)
    for start,end,blob in split_ranges(body,[len(body)//2]):
        write_step(d,steps,'https://files.example.test',bundle,'body','GET',blob,
                   range_header=f'bytes={start}-{end-1}',status=206,
                   headers={'Content-Range':f'bytes {start}-{end-1}/{len(body)}'})
    write_step(d,steps,'https://files.example.test',bundle,'index','GET',
               index_doc(bundle,revision,notice,[m1[2]],lot=lot))
    write_manifest('body-mismatch',steps)


def scenario_notice_version_mismatch():
    """公告版本与清单 notice 不一致，引用无法闭合。"""
    bundle,revision,lot='B-15','r1','LOT-2026-007'
    m1=member_entry('reports/R-42.pdf','notice-version',4)
    body=build_zip([(m1[0],m1[1])])
    steps=[]
    d=make_manifest('notice-mismatch',steps)
    write_step(d,steps,'https://files.example.test',bundle,'head','GET',
               head_doc(bundle,revision,body))
    for start,end,blob in split_ranges(body,[len(body)//2]):
        write_step(d,steps,'https://files.example.test',bundle,'body','GET',blob,
                   range_header=f'bytes={start}-{end-1}',status=206,
                   headers={'Content-Range':f'bytes {start}-{end-1}/{len(body)}'})
    write_step(d,steps,'https://files.example.test',bundle,'index','GET',
               index_doc(bundle,revision,'notice-50',[m1[2]],lot=lot))
    write_step(d,steps,'https://files.example.test',bundle,'notice','GET',
               notice_doc(bundle,revision,'notice-OTHER',lot,
                          [{'path':m1[2]['path'],'sha256':m1[2]['sha256'],'pages':4}]))
    write_manifest('notice-mismatch',steps)


def main():
    scenario_happy()
    scenario_resume_prefix()
    scenario_duplicate_member()
    scenario_traversal()
    scenario_versions()
    scenario_splice_attack()
    scenario_body_hash_mismatch()
    scenario_notice_version_mismatch()
    print('fixtures written under',DATA)


if __name__=='__main__':
    main()
