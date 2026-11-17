"""只读重放合成的来源交互并展示当前接收状态。"""
import argparse
import json
from .transport import Replay
from .ledger import connect,receive
from .view import overview,collection_view
from .collector import Collector

def cmd_inspect(args):
    replay=Replay(args.manifest)
    db=connect(':memory:')
    collection='evidence-bundle'
    for step in replay.steps:
        request=step['request']
        raw,receipt=replay.request(request['method'],request['url'])
        receive(db,collection,raw,receipt)
    print(json.dumps(overview(db,collection),ensure_ascii=False,indent=2))
    db.close()

def cmd_collect(args):
    with Collector(args.store,args.db,collection='evidence-bundle') as collector:
        replay=Replay(args.manifest)
        collector.run_replay(replay)
        view=collection_view(collector)
        if args.materialize:
            for bundle in view['bundles']:
                if bundle['closed']:
                    path=collector.materialize(bundle['bundle_id'],bundle['revision'])
                    bundle['material_manifest']=str(path)
        print(json.dumps(view,ensure_ascii=False,indent=2))

def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest='command',required=True)

    p_inspect=sub.add_parser('inspect',help='重放冻结交互并展示账本状态')
    p_inspect.add_argument('manifest')
    p_inspect.set_defaults(func=cmd_inspect)

    p_collect=sub.add_parser('collect',help='按内容身份分段采集并核验附件包')
    p_collect.add_argument('manifest')
    p_collect.add_argument('--store',required=True,help='每个包版次独立的片段/组包目录')
    p_collect.add_argument('--db',required=True,help='采集状态 SQLite 路径')
    p_collect.add_argument('--materialize',action='store_true',
                           help='仅对闭合的包产出完整材料清单')
    p_collect.set_defaults(func=cmd_collect)

    args=p.parse_args()
    args.func(args)

if __name__=='__main__':main()
