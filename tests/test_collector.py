"""采集器：冻结清单、内容身份恢复与异常分置的端到端测试。"""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from capture_service.collector import (
    Collector, compute_gaps, parse_content_range, identify_target,
    identify_revision, PENDING)
from capture_service.domain import safe_member_path, structured_lot
from capture_service.transport import Replay
from capture_service.view import collection_view

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'
COLLECTION='evidence-bundle'


class CollectorCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        base=Path(self.tmp.name)
        self.store=base/'store'
        self.db=base/'cap.sqlite'

    def tearDown(self):
        self.tmp.cleanup()

    def collector(self):
        return Collector(self.store,self.db,collection=COLLECTION)

    def run_manifest(self,path,collector=None):
        col=collector or self.collector()
        manifest=path if isinstance(path,Path) else DATA/path
        if manifest.is_dir():
            manifest=manifest/'manifest.json'
        with col:
            col.run_replay(Replay(manifest))
            return col,collection_view(col)

    def bundle(self,view,bundle_id,revision):
        return next(b for b in view['bundles']
                    if b['bundle_id']==bundle_id and b['revision']==revision)

    def test_happy_path_closes_and_records_member_provenance(self):
        col,view=self.run_manifest('happy')
        self.assertTrue(view['publishable'])
        b=self.bundle(view,'B-9','r1')
        self.assertEqual(b['state'],'closed')
        self.assertEqual(b['body_sha256'],b['assembled_sha256'])
        self.assertEqual(b['byte_gaps'],[])
        # 每个成员保留来源请求区间，且区间由多个分片收据闭合
        for member in b['members']:
            self.assertEqual(len(member['span']),2)
            self.assertGreaterEqual(len(member['receipts']),1)
        # 每个分片都带请求区间与收据身份
        self.assertTrue(all(f['range'] and f['receipt'] and f['url']
                            for f in b['fragments']))
        # 物化输出按内容摘要命名，批号只来自结构化字段（重开连接模拟新进程）
        with Collector(self.store,self.db) as fresh:
            manifest_path=fresh.materialize('B-9','r1')
            doc=json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        self.assertEqual(doc['lot'],'LOT-2026-001')
        for m in doc['members']:
            self.assertTrue(m['stored'].endswith('.bin'))
            self.assertEqual(Path(m['stored']).stem,m['sha256'])
            self.assertNotIn('.pdf',m['stored'])
            self.assertGreater(m['source_span'][1]-m['source_span'][0],0)

    def test_resume_keeps_verified_fragments_and_closes_gap(self):
        store,db=self.store,self.db
        with Collector(store,db) as col:
            col.run_replay(Replay(DATA/'resume-prefix'/'manifest.json'))
            before=col.status()[0]
        self.assertEqual(before['state'],'incomplete')
        self.assertEqual(before['byte_gaps'],[{'start':575,'end':863}])
        # 已验片段与状态落盘
        self.assertTrue(any((store/'B-9'/'r1'/'fragments').iterdir()))
        # 新实例续采完整清单：已收片段幂等，缺口补齐后闭合
        with Collector(store,db) as col:
            col.run_replay(Replay(DATA/'happy'/'manifest.json'))
            after=col.status()[0]
        self.assertEqual(after['state'],'closed')
        self.assertEqual(after['byte_gaps'],[])
        self.assertEqual(len(after['fragments']),3)

    def test_splice_conflict_is_rejected_not_appended(self):
        manifest=json.loads((DATA/'splice'/'manifest.json').read_text(encoding='utf-8'))
        first=manifest['steps'][1]
        total=int(first['response']['headers']['Content-Range'].split('/')[1])
        first_len=first['response']['bytes']
        with self.assertRaises(ValueError):
            self.run_manifest('splice')
        # 冲突后已收的第一段仍保留，状态可查，缺口可见
        with Collector(self.store,self.db) as col:
            b=col.status()[0]
        self.assertEqual(b['revision'],'r1')
        self.assertEqual(len(b['fragments']),1)
        self.assertEqual(b['byte_gaps'],[{'start':first_len,'end':total}])

    def test_duplicate_member_quarantined(self):
        with self.assertRaises(ValueError) as cm:
            self.run_manifest('dup-member')
        self.assertIn('重复成员',str(cm.exception))

    def test_path_traversal_rejected_at_index_and_zip(self):
        with self.assertRaises(ValueError) as cm:
            self.run_manifest('traversal')
        self.assertIn('越界',str(cm.exception))
        # 纯函数层面的防护
        for evil in ('../x','/abs/x','a/../../b','x\\y','C:/x'):
            with self.assertRaises(ValueError):
                safe_member_path(evil)

    def test_same_name_different_revisions_handled_separately(self):
        _,view=self.run_manifest('versions')
        ids={(b['bundle_id'],b['revision']) for b in view['bundles']}
        self.assertEqual(ids,{('B-12','r1'),('B-12','r2')})
        for b in view['bundles']:
            self.assertEqual(b['state'],'closed')
            self.assertEqual(b['byte_gaps'],[])
        # 两版次成员同名但内容身份不同，各自独立存储目录
        r1=self.bundle(view,'B-12','r1')
        r2=self.bundle(view,'B-12','r2')
        self.assertNotEqual(r1['members'][0]['sha256'],r2['members'][0]['sha256'])
        self.assertTrue((self.store/'B-12'/'r1').is_dir())
        self.assertTrue((self.store/'B-12'/'r2').is_dir())

    def test_body_digest_mismatch_never_published(self):
        with self.assertRaises(ValueError) as cm:
            self.run_manifest('body-mismatch')
        self.assertIn('包级摘要',str(cm.exception))
        with Collector(self.store,self.db) as col:
            b=col.status()[0]
        self.assertIsNone(b['assembled_sha256'])
        self.assertFalse(b['publishable'])

    def test_notice_version_mismatch_holds_publication(self):
        _,view=self.run_manifest('notice-mismatch')
        b=view['bundles'][0]
        self.assertEqual(b['state'],'quarantined')
        self.assertTrue(any('公告版本号' in r for r in b['reasons']))
        with Collector(self.store,self.db) as col:
            with self.assertRaises(ValueError):
                col.materialize(b['bundle_id'],b['revision'])

    def test_late_manifest_assembles_after_identity_arrives(self):
        src=DATA/'happy'
        manifest=json.loads((src/'manifest.json').read_text(encoding='utf-8'))
        steps=manifest['steps']
        late_dir=Path(self.tmp.name)/'late'
        late_dir.mkdir()
        for s in steps:
            (late_dir/s['response']['file']).write_bytes(
                (src/s['response']['file']).read_bytes())
        reordered={'allowed_origins':manifest['allowed_origins'],
                   'steps':steps[1:4]+[steps[0]]+steps[4:]}
        mp=late_dir/'manifest.json'
        mp.write_text(json.dumps(reordered),encoding='utf-8')
        col,view=self.run_manifest(late_dir)
        self.assertEqual(view['bundles'][0]['state'],'closed')

    def test_lot_never_inferred_from_filenames(self):
        # 文件名里塞批号样式的 token，结构化字段缺失时批号必须为 None
        index={'members':[],'lot':None}
        notice={'lot':None,'references':[]}
        self.assertIsNone(structured_lot(index,notice))
        index2={'members':[{'path':'reports/LOT-FAKE-999.pdf'}],'lot':'LOT-REAL-1'}
        self.assertEqual(structured_lot(index2,notice | {'lot':'LOT-REAL-1'}),
                         'LOT-REAL-1')
        with self.assertRaises(ValueError):
            structured_lot(index2,{'lot':'LOT-OTHER','references':[]})

    def test_content_range_parsing(self):
        self.assertEqual(parse_content_range('bytes 0-99/200',100),(0,100,200))
        self.assertEqual(parse_content_range('bytes 100-199/200',100),(100,200,200))
        for bad in (None,'items 0-99/200','bytes 100-90/200','bytes 0-99/x',
                    'bytes 0-200/200'):
            with self.assertRaises(ValueError):
                parse_content_range(bad,100)
        with self.assertRaises(ValueError):
            parse_content_range('bytes 0-99/200',99)

    def test_gap_computation(self):
        self.assertEqual(compute_gaps([],10),[(0,10)])
        self.assertEqual(compute_gaps([{'start':0,'end':4},{'start':7,'end':10}],10),
                         [(4,7)])
        self.assertEqual(compute_gaps([{'start':0,'end':10}],10),[])

    def test_url_target_identification(self):
        self.assertEqual(identify_target('https://x.test/bundles/B-9/body'),'B-9')
        self.assertEqual(identify_revision('https://x.test/bundles/B-9/r1/body'),'r1')
        self.assertIsNone(identify_revision('https://x.test/bundles/B-9/body'))
        self.assertEqual(identify_target('https://x.test/bundles/B-9/index'),'B-9')

    def test_pending_fragments_promoted_when_manifest_arrives(self):
        # 无显式版次的分片先入 pending，head 到达后提升为正式版次
        with Collector(self.store,self.db) as col:
            col.run_replay(Replay(DATA/'happy'/'manifest.json'))
            rows=col.db.execute(
                'SELECT DISTINCT revision FROM bundles').fetchall()
        self.assertNotIn((PENDING,),rows)

    def test_fragments_before_head_are_held_then_promoted(self):
        import shutil
        manifest=json.loads((DATA/'happy'/'manifest.json').read_text(encoding='utf-8'))
        body_steps=[s for s in manifest['steps'] if s['request'].get('range')]
        work=Path(self.tmp.name)/'prehead'
        work.mkdir()
        for s in body_steps:
            (work/s['response']['file']).write_bytes(
                (DATA/'happy'/s['response']['file']).read_bytes())
        (work/'manifest.json').write_text(json.dumps(
            {'allowed_origins':manifest['allowed_origins'],'steps':body_steps}),
            encoding='utf-8')
        with Collector(self.store,self.db) as col:
            col.run_replay(Replay(work/'manifest.json'))
            waiting=col.status()[0]
        self.assertEqual(waiting['state'],'awaiting-manifest')
        self.assertEqual(waiting['revision'],PENDING)
        # head 到达：pending 提升，包组装但缺清单/公告，仍不得发布
        head=manifest['steps'][0]
        (work/head['response']['file']).write_bytes(
            (DATA/'happy'/head['response']['file']).read_bytes())
        head_manifest=Path(self.tmp.name)/'head-only'
        head_manifest.mkdir()
        (head_manifest/head['response']['file']).write_bytes(
            (DATA/'happy'/head['response']['file']).read_bytes())
        (head_manifest/'manifest.json').write_text(json.dumps(
            {'allowed_origins':manifest['allowed_origins'],'steps':[head]}),
            encoding='utf-8')
        with Collector(self.store,self.db) as col:
            col.run_replay(Replay(head_manifest/'manifest.json'))
            s=col.status()[0]
        self.assertEqual(s['revision'],'r1')
        self.assertTrue(s['assembled_sha256'])
        self.assertFalse(s['publishable'])
        self.assertTrue(any('成员清单晚到' in r for r in s['reasons']))

    def test_zip_bomb_size_guard(self):
        with self.assertRaises(ValueError):
            Collector(self.store,self.db,max_zip_bytes=10) \
                .run_replay(Replay(DATA/'happy'/'manifest.json'))


if __name__=='__main__':
    unittest.main()
