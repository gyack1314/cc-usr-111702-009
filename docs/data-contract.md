# 监管附件包资料约定

bundle 与 revision 标识同一包版次，body_sha256 是包级预期身份；ranges 为已见区间。members 列出成员路径、长度和摘要；verified 表示成员字节核验状态。

normal.json 保存两个按顺序接收的来源，conflict.json 在首份原文之后给出同一实体版本的不同内容。相同身份的版本冲突由账本事务拒绝；未闭合的来源不能被解释成最终处置结果。

bundle 的 body_sha256 是来源声明，ranges 是候选片段；未通过独立字节核验前不称其为已恢复完整包。

## 分段采集（collector）扩展约定

冻结步骤可携带 `request.range`（Range 头原文）与 `response.headers`（含 `Content-Range: bytes start-end/total`），
分段响应状态为 206。采集器按内容身份管理分片：

- 分片身份是 `(start, end, sha256)`，半开区间；Content-Range 与实际长度不符即拒绝。
- 同区间重传摘要相同则幂等；重叠区间字节不同则判定为拼接攻击并拒绝，已验片段仍保留。
- 每个包版次（bundle+revision）在 store 下有独立目录：`fragments/` 按分片摘要存 `.part`，
  `assembled/` 按包摘要存组包，`materialized/` 仅对闭合包产出，成员以 `<成员摘要>.bin` 落盘。
- 成员核验记录路径、大小、成员 sha256、包内字节 span 与覆盖该 span 的收据摘要列表。
- 公告（notice）文档含 `notice` 版本号、结构化 `lot` 批号与 `references`（path/sha256/pages）。
  批号只取自结构化字段；页码与目录的相符性通过公告页码对清单页码、字节身份由成员摘要保证。
- 状态机：`awaiting-manifest`（清单晚到）、`incomplete`（有缺口或未组包）、
  `quarantined`（ZIP/引用核验未通过）、`closed`（全部闭合，唯一可物化/发布状态）。
