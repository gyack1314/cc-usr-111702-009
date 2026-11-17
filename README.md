# 监管附件包的清单校验与断点恢复采集

附件包的分段接收记录与成员清单分开保存。成员引用未核验时，不把解压成功等同完整材料。

现有项目从冻结来源响应读取资料，由 transport 保证请求与原文字节身份，domain 解释本领域字段，ledger 保存版本和原始收据，view 展示已确认内容与缺口。样例为合成资料，不访问真实监管站或企业系统。

Python 3.11 以上；在项目根目录执行：

    python -m unittest discover -s tests -v
    python -m compileall -q capture_service tests
    python -m capture_service.cli inspect data/normal.json

Dockerfile 提供 Linux 环境；docker build -t evidence-bundle . 构建，docker run --rm evidence-bundle 运行样例。

样例只收到了包头范围描述和声明的成员目录，没有真实 ZIP 字节。
