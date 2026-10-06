# metric-alerts

指标抓取与持续阈值告警后端。周期性并发抓取 Prometheus 文本格式（0.0.4 子集）的
gauge 指标，按"值连续超过阈值若干秒"的规则产生 firing / resolved 事件，全部状态
持久化在 SQLite 中，并提供只读 HTTP 查询接口。不包含 PromQL、通知和前端。

## 环境

- Python 3.14（使用仓库内 `.venv/bin/python`）
- 依赖：aiohttp 3.13（HTTP 抓取与 API）、prometheus-client 0.22（仅供 demo exporter 使用）
- 测试：pytest（`.venv/bin/python -m pytest tests/ -q`）

## 运行

```bash
.venv/bin/python -m app --config config.json
```

启动时校验配置，非法配置打印原因并以退出码 2 结束。`SIGINT`/`SIGTERM` 触发优雅退出：
取消所有抓取任务、等待其结束、关闭 HTTP 服务与客户端连接、关闭数据库。

## 配置（JSON）

```json
{
  "host": "127.0.0.1",        // 可选，默认 127.0.0.1
  "port": 8080,               // 本机 HTTP 端口，1-65535
  "sqlite_path": "alerts.db", // SQLite 文件路径
  "targets": [
    {
      "id": "demo",                               // 唯一目标 ID
      "url": "http://127.0.0.1:9100/metrics",     // HTTP(S) 抓取地址
      "interval_seconds": 2,                      // 抓取周期，> 0
      "timeout_seconds": 1.5,                     // 单次抓取超时，> 0
      "max_response_bytes": 65536                 // 响应字节上限，正整数
    }
  ],
  "rules": [
    {
      "id": "demo-temp-high",                 // 唯一规则 ID
      "target_id": "demo",                    // 引用已存在的目标
      "metric": "demo_temperature_celsius",   // 指标名
      "labels": {"room": "server"},           // 标签等值筛选，可省略（默认 {}）
      "threshold": 30.0,                      // 有限阈值（严格大于才超限）
      "duration_seconds": 5                   // 非负持续秒数，0 表示立即触发
    }
  ]
}
```

未知键、重复 ID、非有限数值（`NaN`/`Infinity` 字面量也会被拒绝）、引用不存在的
目标等都会在启动时报错。

## 抓取与解析

- 每个目标一个独立 asyncio 任务：目标间互不阻塞；目标内严格串行，绝不重叠抓取。
- 支持 0.0.4 的 `# HELP`/`# TYPE` 指令与 gauge 样本行；标签值支持 `\\`、`\"`、`\n`
  转义；未声明 TYPE 的指标按 gauge 处理。
- 以下情况整轮失败，不发布任何半份样本（上一轮快照保留）：
  显式时间戳、非有限值（`NaN`/`±Inf`/溢出如 `1e999`）、重复序列（标签乱序也算同一
  序列）、重复标签名、非 gauge 的 TYPE、其他任何格式错误、HTTP 非 200、超时、
  响应超过字节上限、非 UTF-8。

## 告警语义

- 告警身份 = (规则, 完整标签集)；规则唯一属于一个目标，标签乱序不改变身份。
- `value > threshold` 进入 pending，以单调时钟计时；持续满足 `duration_seconds`
  才 firing（产生一次 firing 事件）；`duration_seconds = 0` 立即触发。
- 持续超限不会重复触发；恢复（值回落）、序列消失、抓取失败都会清除 pending；
  若已 firing 则产生一次 resolved 事件，原因分别为
  `recovered` / `series_missing` / `scrape_failed`。
- 失败轮不为 pending 计时：再次超限从零重新计时，不跨失败轮累计。
- 每轮的抓取结果、样本快照、告警状态变更与事件在同一个 SQLite 事务中落库；
  事件 ID 为自增主键，跨重启稳定。
- 重启时：历史全部保留；pending 只存在于内存，自然清除；上一进程遗留的 firing
  告警一次性以 `restart` 原因 resolved；停机时间不累计，若仍超限则重新计时。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 存活探针 |
| GET | `/api/targets` | 所有目标配置及健康（最近一轮结果、错误、成功时间、轮次统计） |
| GET | `/api/targets/{id}/samples` | 该目标最近一次成功轮的样本快照，可加 `?metric=` 过滤 |
| GET | `/api/alerts` | 当前活动（firing）告警 |
| GET | `/api/events?after_id=0&limit=50` | 事件按 ID 升序翻页，`next_after_id` 作为下一页的 `after_id`；`limit` 上限 500 |

## 演示

终端 1 启动 demo exporter（用 prometheus-client 暴露一个可调的 gauge）：

```bash
.venv/bin/python demo/exporter.py          # http://127.0.0.1:9100/metrics
```

终端 2 启动后端：

```bash
.venv/bin/python -m app --config config.json
```

终端 3 用 curl 观察触发与恢复：

```bash
curl -s http://127.0.0.1:8080/api/targets                 # 目标健康
curl -s http://127.0.0.1:8080/api/targets/demo/samples    # 样本快照

curl -s http://127.0.0.1:9100/set/42    # 超过阈值 30
sleep 8                                  # 持续 5s 后才触发
curl -s http://127.0.0.1:8080/api/alerts   # 看到 firing 告警
curl -s http://127.0.0.1:8080/api/events   # 事件 1: firing

curl -s http://127.0.0.1:9100/set/10    # 回落
sleep 3
curl -s http://127.0.0.1:8080/api/events   # 事件 2: resolved / recovered

# 翻页
curl -s 'http://127.0.0.1:8080/api/events?after_id=0&limit=2'
curl -s 'http://127.0.0.1:8080/api/events?after_id=2&limit=2'
```

把 exporter 停掉可观察 `scrape_failed` 恢复；在 firing 期间重启后端可观察
`restart` 恢复。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `app/textparse.py` | 0.0.4 文本格式子集解析器（gauge、标签转义、严格校验） |
| `app/config.py` | JSON 配置加载与启动校验 |
| `app/scraper.py` | 每目标独立任务的周期抓取、超时与字节上限 |
| `app/alerts.py` | pending/firing 状态机，产出纯 transition（先落库后应用） |
| `app/store.py` | SQLite 仓储：轮次、样本快照、活动告警、事件，单事务写入 |
| `app/server.py` | aiohttp 只读查询 API |
| `app/main.py` | 组装与生命周期（信号处理、任务与连接回收） |
| `demo/exporter.py` | 演示用指标源 |
| `tests/` | 解析器、配置、状态机、仓储单元测试与端到端集成测试 |
