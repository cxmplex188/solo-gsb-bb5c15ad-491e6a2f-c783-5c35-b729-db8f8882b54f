# Live Subtitle Finalizer — 直播字幕定稿服务

基于 FastAPI + SQLite 的直播字幕定稿服务。客户端按频道提交带**序号 / 修订号 / 文本**的字幕片段，服务将乱序到达的片段放入缓冲区，只从序号 1 起按**连续前缀**推进水位线并生成**不可再修改**的定稿字幕。

## 语义规则

- 片段可乱序到达；同一序号只接受**更高修订号**（更低或相等的修订号被拒绝）。
- 水位线 = 已定稿的最大连续序号。遇到缺口即停，**不会越过缺口**定稿。
- 缺口被补齐的提交会在**同一事务**内一次释放整段连续片段。
- 序号 ≤ 当前水位线的提交一律不生效（定稿不可变）。
- 所有提交在 `BEGIN IMMEDIATE` 事务内串行执行，并发下不会重复定稿或跳过缺口。

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
- `already_finalized` — 该序号已定稿，不可再修改。

### 查询频道定稿

```bash
curl 'http://localhost:8000/channels/news/finalized?after=0&limit=1000'
```

```json
{
  "channel": "news",
  "watermark": 1,
  "items": [{"seq": 1, "revision": 1, "text": "各位观众晚上好"}]
}
```

`after` 用于增量拉取（只返回序号大于该值的定稿片段）。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest                     # 含乱序、修订竞争、并发事务等用例
uvicorn app.main:app --reload --port 8000
```

## 实现要点

- **事务化提交**：`app/service.py` 中「检查修订号 → 写入缓冲区 → 推进水位线 → 生成定稿」在单个 `BEGIN IMMEDIATE` 事务内完成；`finalized` 表主键 `(channel, seq)` 作为防重复定稿的最后防线。
- **连续前缀推进**：`_advance_watermark` 从 `watermark+1` 起逐条消费缓冲区，遇到第一个缺口即停止。
- **存储**：`channels`（水位线）、`fragments`（待定稿缓冲）、`finalized`（定稿，不可变）三张表；WAL 模式 + `busy_timeout`。
