# 监管附件包的清单校验与断点恢复采集

附件包的分段接收记录与成员清单分开保存。成员引用未核验时，不把解压成功等同完整材料。

项目从冻结来源响应读取资料：transport 保证请求与原文字节身份（含 Range/206 分段交互），
domain 解释本领域字段，ledger 保存版本和原始收据，collector 按冻结清单与内容身份恢复 ZIP 包，
view 展示已确认内容与缺口。样例为合成资料，不访问真实监管站或企业系统。

## 采集器的核验与恢复规则

- **内容身份续传，不按文件名**：每个分片以 `(起点,终点,sha256)` 为身份落盘，文件名即内容摘要。
  同一请求区间重传时摘要相同才幂等放行；重叠区间逐字节不一致即拒绝拼接，
  避免网络中断后把旧包片段接到新包尾部。
- **包级摘要闭合**：分片按区间排序拼接，逐片复核摘要后整体计算 sha256，
  与冻结清单声明的 `body_sha256` 不符则拒绝，绝不发布。
- **成员级核验**：解析 ZIP 时拒绝目录穿越、绝对路径、反斜杠/盘符、重复成员、符号链接等非常规类型；
  记录每个成员的路径、大小、sha256，以及它在包体内的字节区间由哪些请求分片（收据）覆盖。
- **公告引用闭合**：成员清单（index）与外部公告（notice）的引用路径、摘要、页码、notice 版本号
  必须一致；只有包内有效成员与公告引用全部闭合，状态才为 `closed` 并允许物化完整材料。
- **清单晚到**：分片先到时进入 `pending-revision` 并保留缺口；冻结清单到达后自动提升版次、组包核验。
- **同名不同版**：`bundle` 相同但 `revision` 不同的包使用独立目录与状态，互不污染。
- **未完成不发布**：未闭合时保留已验片段、组包与明确缺口（`byte_gaps`），供后续续采；
  物化（materialize）只对 `closed` 的包生效。
- **批号不来自文件名**：产品批号只取清单/公告的结构化 `lot` 字段，
  解压成员一律以内容摘要命名（`<sha256>.bin`），不从文件名推断批号。

## 运行

Python 3.11 以上。先安装本包（隔离模式 `-I` 不读取当前目录与 PYTHONPATH）：

    python -I -m pip install -e .
    python -I -m unittest discover -s tests -v
    python -I -m compileall -q capture_service tests tools
    python -I -m capture_service.cli inspect data/normal.json

分段采集与核验（store 与 db 路径由参数指定，每个包版次在 store 下有独立目录）：

    python -I -m capture_service.cli collect data/happy/manifest.json \
        --store /tmp/evidence/store --db /tmp/evidence/cap.sqlite --materialize

合成夹具（ZIP、206 分片、冻结清单）由生成器确定性产出：

    python -I tools/make_fixtures.py

夹具覆盖：正常闭合、断点前缀续采、重复成员、目录穿越、同名不同版、
分片拼接攻击、包级摘要不符、公告版本不符。

Dockerfile 提供 Linux 环境；docker build -t evidence-bundle . 构建，docker run --rm evidence-bundle 运行样例。
