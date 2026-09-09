# 任务中心(todo-center)

多项目共用的任务/缺陷管理:所有 workspace 的任务集中在一个 SQLite(`app.db`),
一个网页(默认 http://127.0.0.1:8765)完成全部项目的增删改查;
项目的新增与模式调整在页面右上角「项目管理」弹窗操作,改完自动同步筛选与降噪。
启停:`./task.sh start` / `./task.sh stop`(默认 8765,被占自动顺延并记录在 `.port`)。
停服后 `index.html` 以 file:// 打开会读取 `snapshot.json` 渲染只读视图。
agent 可用 `sqlite3` 直查直写,也可走 HTTP API。

## 一、核心语义(与旧单项目版的差异)

- **tasks 复合主键 `(project, id)`**:id 按项目独立自增,跨项目会撞号。
  引用任务必须带项目名,写作 `质量看板系统#166`(口语可简写 质量#166)。
- **任务可换项目**:编辑页切换项目下拉,或 `PUT /api/tasks/{id}` body 带 `move_project`;
  id 默认保留,若目标项目已占用该 id 则改为目标项目 `max(id)+1`(跨项目引用会失效,移动前注意)。
- **projects 表**登记项目并分层:
  - `活跃`:正常排新需求(当前:服务器资源管理系统)
  - `维护`:只接缺陷修复与已排任务,不排新需求(当前:质量看板系统)
  - `归档`:冻结,禁止新增任务(当前:制度发布审批管控)
  - 改动项目分层:`PUT /api/projects/{name}`,body `{"mode":"维护"}`。
- 网页「全部项目」视图默认隐藏 维护/归档 项目的 已完成/已取消 任务(降噪),选中具体项目可见全部。

## 二、建表语句全文

```sql
CREATE TABLE IF NOT EXISTS projects (
  name       TEXT PRIMARY KEY,
  mode       TEXT NOT NULL DEFAULT '活跃'
             CHECK(mode IN ('活跃','维护','归档')),
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS tasks (
  project      TEXT NOT NULL REFERENCES projects(name),
  id           INTEGER NOT NULL,
  type         TEXT NOT NULL CHECK(type IN ('task','bug')),
  title        TEXT NOT NULL,
  detail       TEXT NOT NULL DEFAULT '',
  reporter     TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT '未开始'
               CHECK(status IN ('未开始','进行中','已完成','已取消','待审核')),
  priority     TEXT NOT NULL DEFAULT '应该做'
               CHECK(priority IN ('必须做','应该做','可不做')),
  progress     INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
  start_time   TEXT,              -- 'YYYY-MM-DD HH:MM:SS',进入"进行中"时自动补(实际值,只读)
  end_time     TEXT,              -- 置"已完成"时自动补(实际值,只读)
  plan_start   TEXT,              -- 'YYYY-MM-DD',计划开始(排期预期值)
  due_date     TEXT,              -- 'YYYY-MM-DD',计划结束/DDL(排期预期值)
  est_hours    REAL NOT NULL DEFAULT 0,
  actual_hours REAL NOT NULL DEFAULT 0,
  test_notes   TEXT NOT NULL DEFAULT '',
  review_notes TEXT NOT NULL DEFAULT '',  -- 审核结果/拒绝原因
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (project, id)
);
```

## 三、agent 直查直写约定

- **任何按 id 的读写必须带 project**:`WHERE project='质量看板系统' AND id=166`。
  只写 `WHERE id=...` 在多项目下会命中错误项目的任务。
- 新建任务自己取号:`SELECT COALESCE(MAX(id),0)+1 FROM tasks WHERE project='…'`;
  或走 API `POST /api/tasks`(body 必含 `project`)由服务端取号。
- **完成只置「待审核」,不直接置「已完成」**,由人工审核通过;打回则回「进行中」。
- Agent 关单应优先调用 `POST /api/tasks/{id}/close`,不要用通用 PUT 只改状态。
- 状态机自动字段(直写 sqlite 时除 `end_time` 有触发器兜底外,其余字段需自行维护):
  - 进「进行中」→ 补 `start_time`;
  - 关单(→待审核)→ 自动补 `end_time`;走 `/close` 时还会置 `progress=100`、校验 `test_notes` 必填并计算 `actual_hours`;
  - 审核通过(→已完成)→ `review_notes` 记录结果;
  - 审核打回(→进行中)→ 清 `end_time`,`review_notes` 记录原因。
- 直查直写后若停服兜底要看最新数据,可 `python3 app.py --export` 刷新 snapshot.json。

## 四、HTTP API

| 方法/路径 | 说明 |
|---|---|
| GET `/api/tasks?project=&type=&status=&priority=&q=` | 列表,各筛选可选 |
| POST `/api/tasks` | 新建,body 必含 `project`(归档项目拒绝) |
| GET/PUT/DELETE `/api/tasks/{id}` | 单条;GET/DELETE 用 `?project=`,PUT 用 body 字段 `project` 寻址、可带 `move_project` 换项目(响应含最终 project/id) |
| POST `/api/tasks/{id}/close` | 关单→待审核,body 必含 `project`、`test_notes` |
| POST `/api/tasks/{id}/review` | 审核,body 必含 `project`、`action=approve/reject` |
| GET/POST `/api/projects`、PUT `/api/projects/{name}` | 项目登记与分层管理 |
| POST `/api/export` | 重新生成 snapshot.json |

## 五、项目接入与迁移记录

- 新项目接入:一条 `INSERT INTO projects(name) VALUES('项目名')`(或 POST /api/projects),
  然后在该 workspace 的 CLAUDE.md 铁律里写明中心库路径与本项目 project 名即可。
- 2026-09-02 由各项目独立 todo 库合并而来(脚本 `migrate.py` 留档):
  - 质量看板系统 148 条(id 1–182)、服务器资源管理系统 13 条(id 1–13),原 id 全部保留;
  - 源库冷备于各自 `todo/app.db.premigrate-20260902`,原目录已冻结;
  - 历史归档文档在 `archive/质量看板系统/`。
