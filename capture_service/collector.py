"""按冻结清单与内容身份恢复附件包。

设计要点：
- 每个包版次（bundle+revision）使用独立目录，目录下再按用途分子目录；
  片段与组包均以内容摘要命名，绝不按成员文件名落盘。
- 片段的身份是 (起点,终点,sha256)。重叠区间必须逐字节一致，否则拒绝拼接，
  从而避免网络中断后把旧包片段接到新包尾部。
- 只有包级摘要、成员路径/大小/成员摘要、公告引用全部闭合后才产出完整材料；
  未完成时保留已验片段与缺口，供后续续采。
- 产品批号只取自清单/公告的结构化字段，不从解压文件名推断。
"""
import hashlib
import json
import os
import sqlite3
import zipfile
from pathlib import Path, PurePosixPath

from .domain import safe_member_path, structured_lot
from .ledger import connect, entities, receive, receive_receipt

PENDING='pending-revision'
MAX_ZIP_BYTES=512*1024*1024
MAX_MEMBERS=100000

COLLECTOR_SCHEMA="""
CREATE TABLE IF NOT EXISTS bundles(
  collection TEXT NOT NULL, bundle_id TEXT NOT NULL, revision TEXT NOT NULL,
  body_sha256 TEXT, total_bytes INTEGER,
  assembled_sha256 TEXT, head_receipt TEXT,
  PRIMARY KEY(collection,bundle_id,revision));
CREATE TABLE IF NOT EXISTS fragments(
  collection TEXT NOT NULL, bundle_id TEXT NOT NULL, revision TEXT NOT NULL,
  start_off INTEGER NOT NULL, end_off INTEGER NOT NULL,
  fragment_sha256 TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,
  request_url TEXT NOT NULL, range_header TEXT, step_no INTEGER,
  PRIMARY KEY(collection,bundle_id,revision,start_off,end_off),
  FOREIGN KEY(collection,bundle_id,revision) REFERENCES bundles(collection,bundle_id,revision)
    ON UPDATE CASCADE ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS member_checks(
  collection TEXT NOT NULL, bundle_id TEXT NOT NULL, revision TEXT NOT NULL,
  path TEXT NOT NULL, member_sha256 TEXT NOT NULL, size INTEGER NOT NULL,
  span_json TEXT NOT NULL, receipt_sha256s_json TEXT NOT NULL,
  PRIMARY KEY(collection,bundle_id,revision,path),
  FOREIGN KEY(collection,bundle_id,revision) REFERENCES bundles(collection,bundle_id,revision)
    ON UPDATE CASCADE ON DELETE CASCADE);
"""


def _digest(blob):
    return hashlib.sha256(blob).hexdigest()


def parse_content_range(header, body_len):
    """解析 Content-Range: bytes start-end/total，失败即拒绝。"""
    if not header or not header.lower().startswith('bytes '):
        raise ValueError('缺少或非法的 Content-Range')
    spec=header.split(' ',1)[1].strip()
    try:
        span,total_text=spec.split('/',1)
        start_text,end_text=span.split('-',1)
        start,end,total=int(start_text),int(end_text),int(total_text)
    except (ValueError,IndexError):
        raise ValueError('Content-Range 无法解析')
    if start<0 or end<start or total<0 or end>=total:
        raise ValueError('Content-Range 越界')
    if body_len != end-start+1:
        raise ValueError('Content-Range 与实际片段长度不符')
    return start,end+1,total


def _parse_url_path(url):
    from urllib.parse import urlsplit
    parts=[p for p in urlsplit(url).path.split('/') if p]
    return parts


def _target_from_parts(parts):
    """解析 /bundles/<id>[/<rev>]/<head|body|index|notice> 形式的路径。"""
    modifiers=('index','notice','head','manifest','body')
    if len(parts)>=3 and parts[0]=='bundles' and parts[-1] in modifiers:
        bundle_id=parts[1]
        revision=parts[2] if len(parts)>=4 else None
        return bundle_id,revision
    if len(parts)>=2 and parts[0]=='bundles':
        # 无修饰语：末段为 bundle id（如 /bundles/B-9）
        return parts[-1],None
    raise ValueError('无法从 URL 识别包标识')


def identify_target(url):
    """从 URL 路径推断目标 bundle（末段为 index/notice/head 等修饰语）。"""
    bundle_id,_=_target_from_parts(_parse_url_path(url))
    return bundle_id


def identify_revision(url):
    """显式版次段（/bundles/<id>/<rev>/body 等）；无段时返回 None 走 pending。"""
    _,revision=_target_from_parts(_parse_url_path(url))
    return revision


def compute_gaps(fragments, total):
    """根据已收片段求未覆盖的字节缺口（半开区间 start,end）。"""
    covered=sorted((f['start'],f['end']) for f in fragments)
    gaps=[]
    cursor=0
    for start,end in covered:
        if start>cursor:
            gaps.append((cursor,start))
        cursor=max(cursor,end)
    if cursor<total:
        gaps.append((cursor,total))
    return gaps


class Collector:
    def __init__(self, store_dir, db_path, collection='evidence-bundle',
                 max_zip_bytes=MAX_ZIP_BYTES):
        self.store=Path(store_dir)
        self.store.mkdir(parents=True,exist_ok=True)
        self.collection=collection
        self.max_zip_bytes=max_zip_bytes
        self.db=connect(db_path)
        self.db.executescript(COLLECTOR_SCHEMA)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self,*exc):
        self.close()

    # ---- 存储布局：每个包版次一个独立空目录树，全部按内容摘要命名 ----
    def dir_for(self,bundle_id,revision,sub):
        d=self.store/bundle_id/revision/sub
        d.mkdir(parents=True,exist_ok=True)
        return d

    def _bundle_row(self,bundle_id,revision):
        return self.db.execute(
            'SELECT body_sha256,total_bytes,assembled_sha256 FROM bundles '
            'WHERE collection=? AND bundle_id=? AND revision=?',
            (self.collection,bundle_id,revision)).fetchone()

    def _ensure_bundle(self,bundle_id,revision):
        self.db.execute(
            'INSERT OR IGNORE INTO bundles(collection,bundle_id,revision) VALUES(?,?,?)',
            (self.collection,bundle_id,revision))

    def _promote_pending(self,bundle_id,revision):
        """清单到达后，把先到的片段从 pending-revision 提升到正式版次。"""
        if revision==PENDING:
            return
        old_dir=self.store/bundle_id/PENDING
        new_dir=self.store/bundle_id/revision
        # 目录先迁移：失败时数据库尚未改动，不会产生库/磁盘不一致
        if old_dir.exists():
            if new_dir.exists():
                raise ValueError('版次目录已存在，拒绝覆盖提升')
            new_dir.parent.mkdir(parents=True,exist_ok=True)
            os.replace(old_dir,new_dir)
        with self.db:
            # 外键 ON UPDATE CASCADE 同步更新 fragments/member_checks
            self.db.execute(
                'UPDATE bundles SET revision=? WHERE collection=? AND bundle_id=? AND revision=?',
                (revision,self.collection,bundle_id,PENDING))

    # ---- 文档（head/index/notice）：走账本冲突事务 ----
    def note_document(self,raw,receipt):
        digest=receive(self.db,self.collection,raw,receipt)
        rows=entities(self.db,self.collection)
        newest=[r for r in rows if r['source']==digest]
        for row in newest:
            if row['type']=='bundle':
                self._promote_pending(row['id'],row['version'])
                self._ensure_bundle(row['id'],row['version'])
                p=row['payload']
                prior=self._bundle_row(row['id'],row['version'])
                if prior and (prior[0] not in (None,p['sha256'])
                              or prior[1] not in (None,p['bytes'])):
                    raise ValueError('同一包版次的包级身份发生冲突')
                self.db.execute(
                    'UPDATE bundles SET body_sha256=?,total_bytes=?,head_receipt=? '
                    'WHERE collection=? AND bundle_id=? AND revision=?',
                    (p['sha256'],p['bytes'],digest,
                     self.collection,row['id'],row['version']))
                # 清单可能晚到：身份齐备后立即尝试用已存片段组包
                self._try_assemble(row['id'],row['version'])
        return digest

    # ---- 片段：内容身份与请求区间 ----
    def receive_part(self,raw,receipt):
        if receipt['status']!=206:
            raise ValueError('分段响应必须是 206 Partial Content')
        digest=receive_receipt(self.db,self.collection,raw,receipt,allowed_statuses=(206,))
        start,end,total=parse_content_range(
            receipt.get('response_headers',{}).get('content-range'),len(raw))
        bundle_id=identify_target(receipt['url'])
        explicit=identify_revision(receipt['url'])
        revision=explicit or self._current_revision(bundle_id)
        self._ensure_bundle(bundle_id,revision)
        self._check_total(bundle_id,revision,total)
        self._reject_overlap_conflict(bundle_id,revision,start,end,raw,digest)
        frag_dir=self.dir_for(bundle_id,revision,'fragments')
        stored=frag_dir/(digest+'.part')
        if not stored.exists():
            stored.write_bytes(raw)
        with self.db:
            self.db.execute(
                'INSERT OR IGNORE INTO fragments VALUES(?,?,?,?,?,?,?,?,?,?)',
                (self.collection,bundle_id,revision,start,end,digest,
                 digest,receipt['url'],receipt.get('range_header'),receipt.get('step')))
        self._try_assemble(bundle_id,revision)
        return digest

    def _current_revision(self,bundle_id):
        rows=self.db.execute(
            'SELECT revision FROM bundles WHERE collection=? AND bundle_id=?',
            (self.collection,bundle_id)).fetchall()
        if not rows:
            return PENDING
        real=[r[0] for r in rows if r[0]!=PENDING]
        if len(real)>1:
            raise ValueError('同名包存在多个版次，续采必须显式指定版次')
        return real[0] if real else PENDING

    def _check_total(self,bundle_id,revision,total):
        row=self._bundle_row(bundle_id,revision)
        if row and row[1] is not None and row[1]!=total:
            raise ValueError('片段的总长度与冻结清单不一致')

    def _reject_overlap_conflict(self,bundle_id,revision,start,end,raw,digest):
        # 同一请求区间重传：内容身份相同即幂等放行，不同即冲突拒绝
        same_span=self.db.execute(
            'SELECT fragment_sha256 FROM fragments '
            'WHERE collection=? AND bundle_id=? AND revision=? AND start_off=? AND end_off=?',
            (self.collection,bundle_id,revision,start,end)).fetchone()
        if same_span:
            if same_span[0]!=digest:
                raise ValueError('同一请求区间收到不同内容，拒绝拼接旧包片段')
            return
        overlaps=self.db.execute(
            'SELECT start_off,end_off,fragment_sha256 FROM fragments '
            'WHERE collection=? AND bundle_id=? AND revision=? '
            'AND start_off<? AND end_off>?',
            (self.collection,bundle_id,revision,end,start)).fetchall()
        for o_start,o_end,o_digest in overlaps:
            lo,hi=max(start,o_start),min(end,o_end)
            existing=(self.dir_for(bundle_id,revision,'fragments')/(o_digest+'.part')).read_bytes()
            if existing[lo-o_start:hi-o_start] != raw[lo-start:hi-start]:
                raise ValueError('重叠区间字节冲突，拒绝拼接旧包片段')

    # ---- 组包与 ZIP 核验 ----
    def _fragments(self,bundle_id,revision):
        return [dict(start=r[0],end=r[1],sha256=r[2],receipt=r[3],
                     url=r[4],range_header=r[5],step=r[6])
                for r in self.db.execute(
                    'SELECT start_off,end_off,fragment_sha256,receipt_sha256,'
                    'request_url,range_header,step_no FROM fragments '
                    'WHERE collection=? AND bundle_id=? AND revision=? ORDER BY start_off',
                    (self.collection,bundle_id,revision))]

    def _try_assemble(self,bundle_id,revision):
        row=self._bundle_row(bundle_id,revision)
        if not row or row[1] is None:
            return  # 清单晚到：先存片段，不组包
        expected_sha,total=row[0],row[1]
        fragments=self._fragments(bundle_id,revision)
        if compute_gaps(fragments,total):
            return  # 还有缺口，保留已验片段
        if total>self.max_zip_bytes:
            raise ValueError('包体超过安全上限')
        hasher=hashlib.sha256()
        assembled_dir=self.dir_for(bundle_id,revision,'assembled')
        out=assembled_dir/('incomplete-'+str(os.getpid())+'.zip')
        cursor=0
        try:
            with open(out,'wb') as fh:
                for f in fragments:
                    if f['start']!=cursor:
                        raise ValueError('片段不连续，无法组包')
                    blob=(self.dir_for(bundle_id,revision,'fragments')/(f['sha256']+'.part')).read_bytes()
                    if len(blob)!=f['end']-f['start'] or _digest(blob)!=f['sha256']:
                        raise ValueError('片段落盘内容与身份不符')
                    fh.write(blob)
                    hasher.update(blob)
                    cursor=f['end']
            if cursor!=total:
                raise ValueError('组包长度与清单不符')
            actual=hasher.hexdigest()
            if actual!=expected_sha:
                raise ValueError('包级摘要核验失败：内容身份与冻结清单不符')
            final=assembled_dir/(actual+'.zip')
            os.replace(out,final)
            try:
                self._verify_zip(bundle_id,revision,final)
            except Exception:
                # 核验失败（重复成员/穿越等）不得保留“已组装”身份
                final.unlink(missing_ok=True)
                with self.db:
                    self.db.execute(
                        'UPDATE bundles SET assembled_sha256=NULL '
                        'WHERE collection=? AND bundle_id=? AND revision=?',
                        (self.collection,bundle_id,revision))
                    self.db.execute(
                        'DELETE FROM member_checks WHERE collection=? AND bundle_id=? AND revision=?',
                        (self.collection,bundle_id,revision))
                raise
            with self.db:
                self.db.execute(
                    'UPDATE bundles SET assembled_sha256=? '
                    'WHERE collection=? AND bundle_id=? AND revision=?',
                    (actual,self.collection,bundle_id,revision))
        finally:
            if out.exists():
                out.unlink()

    def _verify_zip(self,bundle_id,revision,zip_path):
        fragments=self._fragments(bundle_id,revision)
        try:
            zf=zipfile.ZipFile(zip_path)
        except zipfile.BadZipFile:
            raise ValueError('组包不是有效 ZIP')
        with zf:
            infos=zf.infolist()
            if len(infos)>MAX_MEMBERS:
                raise ValueError('成员数量超过安全上限')
            seen={}
            records=[]
            for info in infos:
                safe_member_path(info.filename)  # 目录穿越直接拒绝
                if info.filename in seen:
                    raise ValueError('ZIP 内重复成员:'+info.filename)
                seen[info.filename]=True
                if info.is_dir():
                    continue
                # 符号链接/设备文件等非常规类型拒绝
                mode=info.external_attr>>16
                if mode and (mode & 0o170000) not in (0o100000,0):
                    raise ValueError('不支持的成员类型:'+info.filename)
                if info.file_size>self.max_zip_bytes:
                    raise ValueError('成员超过安全上限:'+info.filename)
                blob=zf.read(info)  # 只读到内存做摘要，不按文件名解压
                member_sha=_digest(blob)
                header_offset=info.header_offset
                span=(header_offset,header_offset+self._local_block_size(zf,info))
                if span[0]<0 or span[1]>self._bundle_row(bundle_id,revision)[1]:
                    raise ValueError('成员来源区间越出包体:'+info.filename)
                receipts=self._span_receipts(bundle_id,revision,span)
                records.append((info.filename,member_sha,len(blob),span,receipts))
        with self.db:
            for path,member_sha,size,span,receipts in records:
                self.db.execute(
                    'INSERT OR REPLACE INTO member_checks VALUES(?,?,?,?,?,?,?,?)',
                    (self.collection,bundle_id,revision,path,member_sha,size,
                     json.dumps(list(span)),json.dumps(sorted(set(receipts)))))

    @staticmethod
    def _local_block_size(zf,info):
        # 本地文件头固定 30 字节 + 名 + 额外字段 + 压缩数据；
        # 数据描述符（flag bit 3）另占 16 字节（含可选签名），取上界保证覆盖。
        size=30+len(info.filename.encode('utf-8'))+len(info.extra)+info.compress_size
        if info.flag_bits & 0x08:
            size+=16
        return size

    def _span_receipts(self,bundle_id,revision,span):
        start,end=span
        hits=self.db.execute(
            'SELECT receipt_sha256,start_off,end_off FROM fragments '
            'WHERE collection=? AND bundle_id=? AND revision=? '
            'AND start_off<? AND end_off>? ORDER BY start_off',
            (self.collection,bundle_id,revision,end,start)).fetchall()
        covered=[]
        receipts=[]
        for receipt,f_start,f_end in hits:
            receipts.append(receipt)
            covered.append((max(start,f_start),min(end,f_end)))
        merged=[]
        for s,e in sorted(covered):
            if not merged or s>merged[-1][1]:
                merged.append([s,e])
            else:
                merged[-1][1]=max(merged[-1][1],e)
        if merged != [[start,end]]:
            raise ValueError('成员来源区间未被请求片段完整覆盖')
        return receipts

    # ---- 状态与闭合 ----
    def status(self):
        out=[]
        rows=self.db.execute(
            'SELECT bundle_id,revision,body_sha256,total_bytes,assembled_sha256,head_receipt '
            'FROM bundles WHERE collection=? ORDER BY bundle_id,revision',
            (self.collection,)).fetchall()
        docs=entities(self.db,self.collection)
        for bundle_id,revision,body_sha,total,assembled,head_receipt in rows:
            fragments=self._fragments(bundle_id,revision)
            index_docs=[r for r in docs if r['type']=='index' and r['id']==bundle_id and r['version']==revision]
            notice_docs=[r for r in docs if r['type']=='notice' and r['id']==bundle_id and r['version']==revision]
            bundle_docs=[r for r in docs if r['type']=='bundle' and r['id']==bundle_id and r['version']==revision]
            checks=[dict(path=r[0],sha256=r[1],size=r[2],span=json.loads(r[3]),
                         receipts=json.loads(r[4]))
                    for r in self.db.execute(
                        'SELECT path,member_sha256,size,span_json,receipt_sha256s_json '
                        'FROM member_checks WHERE collection=? AND bundle_id=? AND revision=? ORDER BY path',
                        (self.collection,bundle_id,revision))]
            state,reasons=self._evaluate(
                bundle_id,revision,body_sha,total,assembled,fragments,
                index_docs,notice_docs,bundle_docs,checks)
            gaps=compute_gaps(fragments,total) if total is not None else []
            out.append(dict(bundle_id=bundle_id,revision=revision,state=state,
                            closed=state=='closed',publishable=state=='closed',
                            body_sha256=body_sha,total_bytes=total,
                            assembled_sha256=assembled,
                            byte_gaps=[dict(start=s,end=e) for s,e in gaps],
                            fragments=[dict(start=f['start'],end=f['end'],
                                            sha256=f['sha256'],receipt=f['receipt'],
                                            url=f['url'],range=f['range_header'],step=f['step'])
                                       for f in fragments],
                            members=checks,reasons=reasons))
        return out

    def _evaluate(self,bundle_id,revision,body_sha,total,assembled,fragments,
                  index_docs,notice_docs,bundle_docs,checks):
        reasons=[]
        if not bundle_docs:
            reasons.append('冻结清单（包级身份）晚到或缺失')
        if not index_docs:
            reasons.append('成员清单晚到或缺失')
        if not notice_docs:
            reasons.append('外部公告晚到或缺失')
        if total is None:
            reasons.append('包总长度未知')
            return 'awaiting-manifest',reasons
        gaps=compute_gaps(fragments,total)
        if gaps:
            reasons.append('包体存在字节缺口 %d 段'%len(gaps))
        if not assembled:
            if not gaps:
                reasons.append('组包未完成或包级摘要未通过')
            return 'incomplete',reasons
        assembled_file=self.dir_for(bundle_id,revision,'assembled')/(assembled+'.zip')
        if not assembled_file.exists():
            reasons.append('已验组包文件缺失，需重新组包')
            return 'incomplete',reasons
        if not checks:
            reasons.append('ZIP 结构/成员核验未通过')
            return 'quarantined',reasons
        index=index_docs[0]['payload'] if index_docs else None
        notice=notice_docs[0]['payload'] if notice_docs else None
        if index:
            by_path={c['path']:c for c in checks}
            for m in index['members']:
                got=by_path.get(m['path'])
                if not got:
                    reasons.append('清单成员不在有效 ZIP 内:'+m['path'])
                    continue
                if got['size']!=m['bytes']:
                    reasons.append('成员大小与清单不符:'+m['path'])
                if got['sha256']!=m['sha256']:
                    reasons.append('成员摘要与清单不符:'+m['path'])
            extra=set(by_path)-{m['path'] for m in index['members']}
            for p in sorted(extra):
                reasons.append('ZIP 存在清单未声明的成员:'+p)
        if notice:
            refs={r['path']:r for r in notice['references']}
            check_paths={c['path'] for c in checks}
            index_pages={m['path']:m.get('pages') for m in index['members']} if index else {}
            for path,ref in refs.items():
                if path not in check_paths:
                    reasons.append('公告引用未闭合:'+path)
                    continue
                got=next(c for c in checks if c['path']==path)
                if got['sha256']!=ref['sha256']:
                    reasons.append('公告引用摘要不符:'+path)
                # 页码与目录相符：公告页码对清单页码，字节身份由成员摘要保证
                if ref.get('pages') is not None and index_pages.get(path) is not None \
                        and ref['pages']!=index_pages[path]:
                    reasons.append('公告页码与目录不符:'+path)
            if index and notice.get('notice')!=index.get('notice'):
                reasons.append('公告版本号与清单 notice 不一致')
        try:
            lot=structured_lot(index,notice)
        except ValueError as exc:
            lot=None
            reasons.append(str(exc))
        if reasons:
            return 'quarantined',reasons
        return 'closed',reasons

    def materialize(self,bundle_id,revision):
        """仅对闭合的包产出完整材料；成员按内容摘要命名，批号取自结构化字段。"""
        status=next((s for s in self.status()
                     if s['bundle_id']==bundle_id and s['revision']==revision),None)
        if not status:
            raise ValueError('未知包版次')
        if status['state']!='closed':
            raise ValueError('未闭合，不得对外给出完整材料: %s'%';'.join(status['reasons']))
        row=self._bundle_row(bundle_id,revision)
        zip_path=self.dir_for(bundle_id,revision,'assembled')/(row[2]+'.zip')
        out_dir=self.dir_for(bundle_id,revision,'materialized')
        manifest={'bundle_id':bundle_id,'revision':revision,
                  'body_sha256':row[2],'lot':None,'members':[]}
        docs=entities(self.db,self.collection)
        index=next((r['payload'] for r in docs
                    if r['type']=='index' and r['id']==bundle_id and r['version']==revision),None)
        notice=next((r['payload'] for r in docs
                     if r['type']=='notice' and r['id']==bundle_id and r['version']==revision),None)
        manifest['lot']=structured_lot(index,notice)
        with zipfile.ZipFile(zip_path) as zf:
            for c in status['members']:
                info=zf.getinfo(c['path'])
                blob=zf.read(info)
                if _digest(blob)!=c['sha256']:
                    raise ValueError('组包成员再次核验失败')
                target=out_dir/(c['sha256']+'.bin')  # 不使用成员文件名落盘
                if not target.exists():
                    target.write_bytes(blob)
                manifest['members'].append(dict(
                    path=c['path'],stored=target.name,sha256=c['sha256'],
                    size=c['size'],source_span=c['span'],source_receipts=c['receipts']))
        manifest_path=out_dir/'material-manifest.json'
        tmp=manifest_path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(tmp,manifest_path)
        return manifest_path

    # ---- 冻结重放驱动 ----
    def run_replay(self,replay):
        for step in replay.steps:
            req=step['request']
            raw,receipt=replay.request(req['method'],req['url'],
                                       range_header=req.get('range'))
            if receipt['status']==206:
                self.receive_part(raw,receipt)
            else:
                self.note_document(raw,receipt)
        return self.status()
