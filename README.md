# 个人任务管理工具(todo)

单需求、单人使用的任务/缺陷小工具:SQLite(`app.db`)是唯一真相,本地网页操作,
agent 可用 `sqlite3` 直查直写。启停:`./task.sh start` / `./task.sh stop`
(端口默认 8765,可用环境变量 `TODO_PORT` 覆盖)。停服后 `index.html` 以 file://
打开会读取 `snapshot.json` 渲染只读视图。

## 一、建表语句全文

```sql
CREATE TABLE IF NOT EXISTS tasks (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  type         TEXT NOT NULL CHECK(type IN ('task','bug')),
  title        TEXT NOT NULL,
  detail       TEXT NOT NULL DEFAULT '',
  reporter     TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT '未开始'
               CHECK(status IN ('未开始','进行中','已完成','已取消','待审核')),
  priority     TEXT NOT NULL DEFAULT '应该做'
               CHECK(priority IN ('必须做','应该做','可不做')),
  progress     INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
  start_time   TEXT,              -- 'YYYY-MM-DD HH:MM:SS',进入"进行中"时自动补
  end_time     TEXT,              -- 置"已完成"时自动补
  plan_start   TEXT,              -- 'YYYY-MM-DD',计划开始
  due_date     TEXT,              -- 'YYYY-MM-DD',计划截止
  est_hours    REAL NOT NULL DEFAULT 0,
  actual_hours REAL NOT NULL DEFAULT 0,
  test_notes   TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
```

## 二、常用查询示例

以下命令均在 todo-app 目录下执行。

```bash
# 任务列表(全部)
sqlite3 -header -column app.db "SELECT id,type,title,status,priority,due_date FROM tasks ORDER BY id;"

# 按状态筛选:进行中的条目
sqlite3 -header -column app.db "SELECT id,type,title,status,due_date FROM tasks WHERE status='进行中' ORDER BY id;"

# 超期查询:截止日期早于今天且未完成/未取消
sqlite3 -header -column app.db "SELECT id,type,title,status,due_date FROM tasks WHERE due_date < date('now','localtime') AND status NOT IN ('已完成','已取消') ORDER BY due_date;"
```

## 三、写入示例

```bash
# 新建一条任务
sqlite3 app.db "INSERT INTO tasks(type,title,detail,reporter,priority,due_date) VALUES('task','示例任务:演示写入','由 README 示例创建','本人','应该做',date('now','+7 day'));"

# 将其置为进行中(直接写库需自行维护 start_time 与 updated_at)
sqlite3 app.db "UPDATE tasks SET status='进行中', start_time=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE title='示例任务:演示写入' AND status='未开始';"
```

说明:

- 状态流转规则(应用层/API 已实现):置「进行中」且 `start_time` 为空时自动写当前时间;
  关单时自动写 `end_time`,`actual_hours` 建议值 =
  round((end_time − start_time)/3600, 1),且 `test_notes` 必填非空;
  关单进入「待审核」,`progress` 未显式给出则自动置 100;
  审核通过(POST `/api/tasks/{id}/review` body `{"action":"approve"}`)→ 已完成;
  打回(body `{"action":"reject","progress":80}`)→ 进行中并清除 end_time;
  任何 UPDATE 都要同步刷新 `updated_at`。
- 验证说明 `test_notes` 展示:列表「内容」列在详情首行下方以绿字 `验证:` 前缀展示其首行预览(120 字截断);
  编辑表单含「验证说明」字段,可回看并修改完整内容(直接 PUT 亦可)。
- 进度 `progress`:0-100 整数,越界 API 拒 400;列表页以进度条展示,已完成为绿色。
- 网页操作按钮(编辑弹窗):「关单…」仅「进行中」显示;「启动」仅「未开始」显示
  (一键置进行中并自动补 start_time);「通过/打回」仅「待审核」显示;
  右上角「删除」对已有单始终显示(物理删除,有确认框)。
- 编辑抽屉保存后保持打开并回读当前单;新建保存后切换为编辑态。抽屉打开时仍可点击左侧
  行切换到其他任务/缺陷;标题栏另有关闭按钮。
- 物理删除走网页「删除」按钮或 `DELETE /api/tasks/{id}`(不可恢复,删后不可再查);
  软路径是取消:把 `status` 置为「已取消」,数据保留。
- `snapshot.json` 内容为 `window.TODO_SNAPSHOT = {JSON}`(供 index.html 在
  file:// 下以 script 标签加载只读数据);agent 取数请直接查 `app.db`,不要解析快照。

规则:

- **R1** 任务状态变更一律走 db 或 API,禁止旁路另建 txt 记账。
- **R2** 只读目录(refs 类)一律不写不删(配合主工作区架构)。
- **R3** Agent 完成的任务关单后进入「待审核」,由开发者人工验证后通过或打回,
  不直接置为「已完成」。
