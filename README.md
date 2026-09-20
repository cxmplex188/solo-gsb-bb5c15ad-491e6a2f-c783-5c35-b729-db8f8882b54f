# Live Subtitle Finalizer — 直播字幕定稿服务

基于 FastAPI + SQLite 的直播字幕定稿服务。客户端按频道提交带**序号 / 修订号 / 文本**的字幕片段，服务将乱序到达的片段放入缓冲区，只从序号 1 起按**连续前缀**推进水位线并生成**不可再修改**的定稿字幕。确认无法补回的序号区间可由操作员裁决为**永久缺口**，水位线跳过缺口继续推进。

## 语义规则

- 片段可乱序到达；同一序号只接受**更高修订号**（更低或相等的修订号被拒绝）。
- 水位线 = 已处理的最大连续序号，由**定稿片段**与**永久缺口**共同覆盖；遇到缺口即停，**不会越过缺口**定稿。
- 缺口被补齐的提交会在**同一事务**内一次释放整段连续片段。
- **永久缺口裁决**：操作员提交缺失的闭区间 `[start_seq, end_seq]`：
  - `start_seq` 必须**恰好等于当前水位线下一位**；
  - 区间内**不得存在已缓冲片段**（已缓冲说明内容仍可能到达，不应跳过）；
  - 校验失败返回 `409` 且**不产生任何写入**；
  - 裁决成功在同一事务内记录跳过序号、把水位线推进到 `end_seq`，并继续释放紧随其后的连续缓冲片段；
  - 重复提交**已完整裁决的同一区间幂等返回**当前结果；与既有区间**部分重叠则拒绝**。
- 序号 ≤ 当前水位线的提交一律不生效（定稿不可变）；落入已裁决缺口的提交返回 `already_skipped`。
- 所有提交与裁决在 `BEGIN IMMEDIATE` 事务内串行执行，并发下不会重复定稿或重复跳过。

## 快速启动（单条命令）

```bash
docker compose up --build
```

服务监听 **8000** 端口，启动时自动初始化数据库（建表幂等）。

- 交互式 API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

## 配置

| 环境变量   | 默认值                 | 说明               |
| ---------- | ---------------------- | ------------------ |
| `DB_PATH`  | `/data/subtitles.db`   | SQLite 数据库文件路径 |

Compose 默认将 `/data` 挂载到命名卷 `subtitles-data`，容器重启数据不丢失。端口映射可在 `docker-compose.yml` 的 `ports` 中调整。

## API

### 提交字幕片段

```bash
curl -X POST http://localhost:8000/channels/news/fragments \
  -H 'Content-Type: application/json' \
  -d '{"seq": 1, "revision": 1, "text": "各位观众晚上好"}'
```

响应（`applied` 表示本次提交是否生效，`watermark` 为处理后的最新水位线，`finalized` 为本次新定稿的片段）：

```json
{
  "channel": "news",
  "seq": 1,
  "applied": true,
  "reason": null,
  "watermark": 1,
  "finalized": [{"seq": 1, "revision": 1, "text": "各位观众晚上好"}]
}
```

`applied=false` 时 `reason` 取值：

- `stale_revision` — 修订号不高于已缓冲的同序号片段；
- `already_finalized` — 该序号已定稿，不可再修改；
- `already_skipped` — 该序号已被裁决为永久缺口，不会再定稿。

### 裁决永久缺口

当确认一段序号永久丢失（例如上行链路丢包无法补传），操作员提交闭区间裁决：

```bash
curl -X POST http://localhost:8000/channels/news/gaps \
  -H 'Content-Type: application/json' \
  -d '{"start_seq": 2, "end_seq": 2, "reason": "上行丢包，无法补传"}'
```

响应（`watermark` 为处理后的最新水位线，`skipped` 为本次跳过区间，`finalized` 为裁决后在同一事务释放的连续缓冲片段）：

```json
{
  "channel": "news",
  "gap": {"start_seq": 2, "end_seq": 2, "reason": "上行丢包，无法补传"},
  "idempotent": false,
  "watermark": 4,
  "skipped": [{"start_seq": 2, "end_seq": 2, "reason": "上行丢包，无法补传"}],
  "finalized": [
    {"seq": 3, "revision": 1, "text": "第三条"},
    {"seq": 4, "revision": 1, "text": "第四条"}
  ]
}
```

- 重复提交**完全相同**的区间（如再次 `{"start_seq": 2, "end_seq": 2}`）返回 `idempotent: true`：`watermark` 为当前水位线，`skipped`/`finalized` 还原该次裁决的结果，不产生任何写入（请求中不同的 `reason` 不会覆盖既有记录）。
- 与既有裁决**部分重叠**（如已裁决 `[2,2]` 后提交 `[2,3]`）返回 `409 {"detail": {"reason": "gap_overlap", ...}}`；紧邻但不重叠的区间（如随后提交 `[3,4]`，前提是 3 恰为水位线下一位）正常接受。
- 其他 `409`：`gap_start_mismatch`（起点不是水位线下一位，响应体附带当前 `watermark` 与 `expected_start_seq`）、`gap_has_buffered`（区间内已有缓冲片段，附带 `buffered_seqs`）。校验失败一律零写入。
- `start_seq`/`end_seq` 为 ≥1 的整数，`end_seq < start_seq` 返回 `422`。

### 查询频道定稿

```bash
curl 'http://localhost:8000/channels/news/finalized?after=0&limit=1000'
```

`items` 按序号顺序混合返回**字幕片段**与**缺口标记**，下游可用 `type` 区分丢失内容：

```json
{
  "channel": "news",
  "watermark": 4,
  "items": [
    {"type": "fragment", "seq": 1, "revision": 1, "text": "各位观众晚上好"},
    {"type": "gap", "start_seq": 2, "end_seq": 2, "reason": "上行丢包，无法补传"},
    {"type": "fragment", "seq": 3, "revision": 1, "text": "第三条"},
    {"type": "fragment", "seq": 4, "revision": 1, "text": "第四条"}
  ]
}
```

- `type=fragment`：已定稿字幕，字段为 `seq/revision/text`；
- `type=gap`：永久缺口标记，字段为 `start_seq/end_seq/reason`（`reason` 可空）。
- `after` 用于增量拉取（只返回序号大于该值的片段、以及起点大于该值的缺口标记）。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest                     # 含乱序、修订竞争、缺口裁决、幂等/重叠、并发事务等用例
uvicorn app.main:app --reload --port 8000
```

## 实现要点

- **事务化提交/裁决**：`app/service.py` 中「检查修订号 → 写入缓冲区 → 推进水位线 → 生成定稿」与「校验缺口区间 → 记录缺口 → 跳过水位线 → 释放连续片段」分别在单个 `BEGIN IMMEDIATE` 事务内完成；裁决校验失败抛出异常即 `ROLLBACK`，保证零写入。`finalized` 表主键 `(channel, seq)`、`gaps` 表主键 `(channel, start_seq)` 作为并发下的最后防线。
- **连续前缀推进**：`_advance_watermark` 从 `watermark+1` 起逐条消费缓冲区，遇到第一个缺口即停止。片段补齐与缺口裁决共用同一条释放路径。
- **幂等裁决**：`gaps.release_until` 记录裁决当时释放连续片段后达到的水位线；重复提交同一区间时据此从 `finalized` 还原该次释放的片段，`watermark` 则返回频道当前值。
- **按序混合查询**：定稿查询对 `finalized` 与 `gaps` 两个不相交的序号空间做双路归并，Pydantic discriminator 联合模型按 `type` 序列化为字幕或缺口标记。
- **存储**：`channels`（水位线）、`fragments`（待定稿缓冲）、`finalized`（定稿，不可变）、`gaps`（永久缺口裁决）四张表；WAL 模式 + `busy_timeout`。
