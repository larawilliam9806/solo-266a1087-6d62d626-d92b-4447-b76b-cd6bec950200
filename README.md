# 试验炉温曲线压缩归档服务

质检复查用的炉温曲线归档服务：FastAPI 提供 HTTP 接口，档案存 SQLite，可用
`docker compose` 一键启动。

## 快速开始

```bash
docker compose up --build
# 服务在 http://localhost:8000，交互文档 /docs
```

本地运行（不装 Docker）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 数据库默认 data/archive.db，可用 FURNACE_DB_PATH 覆盖
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/runs`（别名 `/archive`、`/archives`、`/api/...`） | 按炉次提交分段采样并归档；带 `stream=1` 时为流式分段提交 |
| `PUT` | `/runs/{run_id}` | 同上，炉次号取路径（同样支持 `stream=1`） |
| `GET` | `/runs/{run_id}` | 按炉次回查归档折线与超温时段 |
| `GET` | `/runs` | 列出全部炉次 |
| `GET` | `/health` | 健康检查 |

参数既可放在 JSON body，也可用查询参数传。`limit`、`tolerance`、
`max_points` 也可放在 `parameters` / `params` / `config` 子对象里。

### 提交归档

```bash
curl -X POST http://localhost:8000/runs \
  -H 'Content-Type: application/json' \
  --data @examples/sample_run.json
```

`examples/sample_run.json` 是一炉 75 个实测点（含一段缺测）的示例；归档为 10 个点。

- `segments`：按炉次的分段采样。每段是点的列表，每点为 `[整数毫秒, 整数温度]`
  （也接受 `{"t": ..., "v": ...}`）。段与段之间是缺测，绝不连接；每段首尾点必留。
  平铺的点列表会被当作一段；列表里的 `null`（或温度为 `null` 的点）表示缺测，
  曲线在此断开。
- `limit`：超温线。`tolerance`：允许温差。`max_points`：归档存点上限。
- 服务只保存满足条件所需的**最少实测点**：在每个原采样时刻，归档折线与实测值
  之差不超过 `tolerance`；归档折线超过 `limit` 的时段与原采样点逐点连线的结果
  完全一致。

入参较宽容：键名大小写/连字符不敏感（如 `OverTemperatureLimit`、
`max-points`），运行号可用 `run_id` / `run` / `heat_no` / `heat_id` / `batch_id`，
采样可用 `[t, v]` 点对、`{"t", "v"}` 对象或 `{"times": [...], "values": [...]}`，
整段也可给 `{"points": [...]}`。数值可为数字或数字字符串；温度、毫秒须为整数。

成功时返回 `200` 且 `archived: true`，含归档后的折线 `segments` 与超温时段
`over_temperature_periods`。

### 流式补交（`stream=1`，长炉次推荐）

整份采样一次读进内存对长炉次开销过大。请求带 `stream=1`（也接受
`streaming=1` / `stream=true`）时，炉次号与 `limit`、`tolerance`、
`max_points` 照旧从查询串传入（键名别名同上），正文是一个 **JSON 数组的分段
采样**，至少一段；每段是由 `[整数毫秒, 整数温度]` 点对组成的非空数组：

```bash
curl -X POST \
  "http://localhost:8000/runs?run_id=H-2026-002&limit=217&tolerance=3&max_points=200&stream=1" \
  -H 'Content-Type: application/json' \
  --data-binary @examples/sample_stream.json
```

流式正文采用严格格式（不接受 `null`、点对象或数字字符串等宽松写法）：

- 顶层必须是数组，段与段之间即缺测边界，段顺序、段内缺测断开与普通交档一致；
- 每段非空，每点恰好两个整数；段内毫秒必须严格递增，各段时间轴相互独立；
- 服务按网络接收块增量解析，**收到一整段就立即按原规则压缩并释放原始采样**，
  因此除单个接收块外，处理本次交档的工作内存只随**最长单段**和
  **`max_points` 存点上限**增长，与总点数、总段数无关；
- 即使中途已超 `max_points`，仍继续校验并核算余下分段。正文合法但超限时，
  回执与普通交档相同（`archived: false`、`error: point_limit_exceeded`），
  并给出整炉所需的最少点数 `required_points`，不写入、不覆盖原档；
- 分段或正文结尾无效（含连接中断、正文被截断）一律返回 `422` 输入错误；
- 仅当整炉数据完整、合法且未超限，并且保存成功时才换档；断传、输入错误、
  超限或保存失败（`500`）时，原档仍可原样回查；
- 成功回执的字段与普通交档完全一致。

### 超出存点上限

若最少点数仍大于 `max_points`，返回 `200` 且 `archived: false`，给出所需最少
点数 `required_points`，**不写入、不覆盖**原有档案：

```json
{
  "run_id": "H-2026-001",
  "archived": false,
  "error": "point_limit_exceeded",
  "required_points": 37,
  "max_points": 30
}
```

### 回查

```bash
curl http://localhost:8000/runs/H-2026-001
```

```json
{
  "run_id": "H-2026-001",
  "limit": 217,
  "tolerance": 2,
  "segments": [
    {"index": 0, "points": [{"t": 0, "v": 25}, ...],
     "point_count": 8, "start_ms": 0, "end_ms": 590000},
    {"index": 1, "points": [{"t": 700000, "v": 41}, {"t": 840000, "v": 34}],
     "point_count": 2, "start_ms": 700000, "end_ms": 840000}
  ],
  "over_temperature_periods": [
    {"segment_index": 0, "start_ms": 347500, "end_ms": 455000,
     "duration_ms": 107500, "start_closed": false, "end_closed": false}
  ],
  "total_over_temperature_ms": 107500,
  "point_count": 10,
  "source_point_count": 75
}
```

超温时段由**归档折线**与**同次归档的超温线**算出：`start_ms`/`end_ms` 为与超温线
的交点时刻（必要时为小数），`start_closed`/`end_closed` 表示该端点是否含在超温
区间内（经过交点的端点恒为开区间）。

## 算法

每段独立求解：在实测评点中选取包含首尾的最小子集，使归档折线满足

1. **容差**：在原采样时刻 `t_k`，`|归档(t_k) − v_k| ≤ tolerance`；
2. **超温一致性**：`{t : 归档(t) > limit}` 与原折线（逐点连线）的集合完全相同。

做法是把采样下标当作顶点、可行的“弦”（相邻两个保留点之间的线段）当作边，求
`0 → n−1` 的最短路径；边权为 1，故 BFS 给出**精确最少点数**。

- 容差检查是增量的：固定起点 `i` 时，可行斜率构成一个随终点前移只收紧的区间，
  每条弦 O(1)，区间空即提前终止。全程用 `Fraction` 精确有理数，避免舍入放行。
- 超温检查用预先求出的极大超温区间，每条弦 O(1)；弦之间分割时间轴、保留点处
  归档值等于实测值，所以逐弦集合相等即全局相等。

复杂度最坏 O(n²)、空间 O(n)；平直/光滑段通常在少量检查内收敛。

## 测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

`tests/test_compression.py` 含随机曲线的暴力最小点数对拍；`tests/test_api.py`
覆盖归档、回查、超温时段重算、存点上限保留原档、缺测断开等。
