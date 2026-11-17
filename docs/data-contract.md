# 监管附件包资料约定

bundle 与 revision 标识同一包版次，body_sha256 是包级预期身份；ranges 为已见区间。members 列出成员路径、长度和摘要；verified 表示成员字节核验状态。

normal.json 保存两个按顺序接收的来源，conflict.json 在首份原文之后给出同一实体版本的不同内容。相同身份的版本冲突由账本事务拒绝；未闭合的来源不能被解释成最终处置结果。

bundle 的 body_sha256 是来源声明，ranges 是候选片段；未通过独立字节核验前不称其为已恢复完整包。
