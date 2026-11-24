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
| `POST` | `/runs`（别名 `/archive`、`/archives`、`/api/...`） | 按炉次提交分段采样并归档 |
| `PUT` | `/runs/{run_id}` | 同上，炉次号取路径 |
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
