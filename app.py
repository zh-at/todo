#!/usr/bin/env python3
"""任务中心(多项目) —— 仅标准库(http.server + sqlite3 + json)。

所有项目共用一个库,tasks 复合主键 (project, id),id 按项目独立自增,
引用任务时务必带项目名(如 质量#166)。projects.mode 用于项目分层:
活跃(正常排需求) / 维护(只接缺陷) / 归档(冻结,禁止新增任务)。

用法:
    python3 app.py            # 启动服务(端口 TODO_PORT,默认 8765)
    python3 app.py --export   # 仅生成 snapshot.json 后退出
"""

import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "app.db"
INDEX_PATH = BASE_DIR / "index.html"
SNAPSHOT_PATH = BASE_DIR / "snapshot.json"

TYPES = ("task", "bug")
STATUSES = ("未开始", "进行中", "已完成", "已取消", "待审核")
PRIORITIES = ("必须做", "应该做", "可不做")
MODES = ("活跃", "维护", "归档")
EDITABLE_FIELDS = (
    "type", "title", "detail", "reporter", "status",
    "priority", "progress", "due_date", "est_hours", "actual_hours", "test_notes",
    "start_time", "end_time", "plan_start", "review_notes",
)
TIME_FMT = "%Y-%m-%d %H:%M:%S"

# 排序:超期优先 → 必须做 > 应该做 > 可不做 → due_date 升序
ORDER_SQL = """
ORDER BY CASE WHEN due_date IS NOT NULL AND due_date < date('now','localtime')
               AND status NOT IN ('已完成','已取消') THEN 0 ELSE 1 END,
         CASE priority WHEN '必须做' THEN 0 WHEN '应该做' THEN 1 ELSE 2 END,
         due_date IS NULL, due_date ASC, project ASC, id ASC
"""

PROJECT_ORDER_SQL = """
ORDER BY CASE mode WHEN '活跃' THEN 0 WHEN '维护' THEN 1 ELSE 2 END, name ASC
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  name       TEXT PRIMARY KEY,
  mode       TEXT NOT NULL DEFAULT '活跃'
             CHECK(mode IN ('活跃','维护','归档')),
  work_stats INTEGER NOT NULL DEFAULT 1
             CHECK(work_stats IN (0,1)),
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
  end_time     TEXT,              -- 提交审核进入"待审核"时自动补(实际值,只读)
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
CREATE TRIGGER IF NOT EXISTS trg_tasks_pending_review_end_time_insert
AFTER INSERT ON tasks
WHEN NEW.status = '待审核' AND NEW.end_time IS NULL
BEGIN
  UPDATE tasks SET end_time = datetime('now','localtime')
   WHERE project = NEW.project AND id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS trg_tasks_pending_review_end_time_update
AFTER UPDATE OF status, end_time ON tasks
WHEN NEW.status = '待审核' AND NEW.end_time IS NULL
BEGIN
  UPDATE tasks SET end_time = datetime('now','localtime')
   WHERE project = NEW.project AND id = NEW.id;
END;
"""


def now_str() -> str:
    return time.strftime(TIME_FMT)


def query(sql: str, args: tuple = ()) -> list[dict]:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, args).fetchall()]


def execute(sql: str, args: tuple = ()) -> int:
    """执行写语句,返回 lastrowid。"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


def require_project(source: dict) -> tuple[str | None, str | None]:
    """从 body/query 参数中提取并校验 project。id 按项目独立,不带项目寻址会撞号。"""
    project = source.get("project")
    if isinstance(project, list):  # parse_qs 的值是列表
        project = project[0] if project else None
    if not isinstance(project, str) or not project.strip():
        return None, "必须指定 project(id 按项目独立,跨项目会撞号)"
    return project.strip(), None


def decode_latin1(s: str) -> str:
    """http.server 按 latin-1 解码请求行,curl 裸拼的中文(未 percent-encode)需还原回 UTF-8。"""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def parse_query(qs: str) -> dict:
    """解析 query string,值做 latin-1 → UTF-8 还原。"""
    out: dict = {}
    for key, vals in parse_qs(qs).items():
        out[key] = [decode_latin1(v) for v in vals]
    return out


def clean_fields(data: dict) -> tuple[dict, str | None]:
    """提取允许写入的字段并校验枚举/格式,返回 (字段字典, 错误信息)。"""
    fields: dict = {}
    for key in EDITABLE_FIELDS:
        if key not in data:
            continue
        val = data[key]
        if key in ("title", "detail", "reporter", "test_notes", "review_notes"):
            if not isinstance(val, str):
                return {}, f"{key} 必须是字符串"
            fields[key] = val.strip() if key == "title" else val
        elif key == "type":
            if val not in TYPES:
                return {}, "type 只能是 task 或 bug"
            fields[key] = val
        elif key == "status":
            if val not in STATUSES:
                return {}, "status 只能是:未开始/进行中/已完成/已取消/待审核"
            fields[key] = val
        elif key == "priority":
            if val not in PRIORITIES:
                return {}, "priority 只能是:必须做/应该做/可不做"
            fields[key] = val
        elif key == "progress":
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                return {}, "progress 必须是数字"
            if not 0 <= val <= 100:
                return {}, "progress 必须在 0-100 之间"
            fields[key] = int(val)
        elif key in ("est_hours", "actual_hours"):
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                return {}, f"{key} 必须是数字"
            fields[key] = float(val)
        elif key in ("due_date", "plan_start"):
            if val in ("", None):
                fields[key] = None
                continue
            try:
                datetime.strptime(str(val), "%Y-%m-%d")
            except ValueError:
                return {}, f"{key} 格式必须为 YYYY-MM-DD"
            fields[key] = str(val)
        elif key in ("start_time", "end_time"):
            if val in ("", None):
                fields[key] = None
                continue
            normalized = str(val).replace("T", " ")
            if len(normalized) == 16:
                normalized += ":00"
            try:
                datetime.strptime(normalized, TIME_FMT)
            except ValueError:
                return {}, f"{key} 格式必须为 YYYY-MM-DD HH:MM"
            fields[key] = normalized
    return fields, None


def list_tasks(params: dict) -> tuple[list[dict], str | None]:
    where, args = [], []
    project = params.get("project", [""])[0]
    if project:
        where.append("project=?")
        args.append(project)
    for key, allowed, label in (("type", TYPES, "task 或 bug"),
                                ("status", STATUSES, "未开始/进行中/已完成/已取消/待审核"),
                                ("priority", PRIORITIES, "必须做/应该做/可不做")):
        val = params.get(key, [""])[0]
        if val:
            if val not in allowed:
                return [], f"{key} 只能是:{label}"
            where.append(f"{key}=?")
            args.append(val)
    kw = params.get("q", [""])[0].strip()
    if kw:
        if kw.isdigit():
            where.append("(title LIKE ? OR id=?)")
            args.append(f"%{kw}%")
            args.append(int(kw))
        else:
            where.append("title LIKE ?")
            args.append(f"%{kw}%")
    sql = "SELECT * FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " " + ORDER_SQL
    return query(sql, tuple(args)), None


def create_task(data: dict) -> tuple[dict, str | None]:
    project, err = require_project(data)
    if err:
        return {}, err
    prows = query("SELECT mode FROM projects WHERE name=?", (project,))
    if not prows:
        return {}, f"项目「{project}」不存在,请先注册(POST /api/projects)"
    if prows[0]["mode"] == "归档":
        return {}, f"项目「{project}」已归档,禁止新增任务"
    fields, err = clean_fields(data)
    if err:
        return {}, err
    if not fields.get("title"):
        return {}, "标题不能为空"
    if "type" not in fields:
        return {}, "缺少 type(task 或 bug)"
    new_id = query("SELECT COALESCE(MAX(id),0)+1 AS nid FROM tasks WHERE project=?",
                   (project,))[0]["nid"]
    cols = ["project", "id"] + list(fields)
    execute(f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (project, new_id, *fields.values()))
    return {"project": project, "id": new_id}, None


def fetch_task(project: str, task_id: int) -> dict | None:
    rows = query("SELECT * FROM tasks WHERE project=? AND id=?", (project, task_id))
    return rows[0] if rows else None


def update_task(project: str, task_id: int, data: dict) -> tuple[dict, str | None]:
    """更新任务;body 可带 move_project 把任务移到别的项目(跨项目引用注意 id 可能变化)。"""
    fields, err = clean_fields(data)
    if err:
        return {}, err
    old = fetch_task(project, task_id)
    if not old:
        return {}, "任务不存在"
    new_project, new_id = project, task_id
    move_to = data.get("move_project")
    if move_to is not None:
        if not isinstance(move_to, str) or not move_to.strip():
            return {}, "move_project 必须是字符串"
        move_to = move_to.strip()
        if move_to != project:
            prows = query("SELECT mode FROM projects WHERE name=?", (move_to,))
            if not prows:
                return {}, f"项目「{move_to}」不存在,请先注册(POST /api/projects)"
            if prows[0]["mode"] == "归档":
                return {}, f"项目「{move_to}」已归档,不可移入"
            new_project = move_to
    if not fields and new_project == project:
        return {}, "没有可更新的字段"
    new_status = fields.get("status", old["status"])
    if new_status == "进行中" and not old["start_time"] and not fields.get("start_time"):
        fields["start_time"] = now_str()
    if new_status in ("待审核", "已完成"):
        if not old["end_time"] and not fields.get("end_time"):
            fields["end_time"] = now_str()
    if new_status == "已完成" and "progress" not in fields:
        fields["progress"] = 100
    fields["updated_at"] = now_str()
    set_sql = ", ".join([f"{k}=?" for k in fields] + ["project=?", "id=?"])
    base_args = (*fields.values(), new_project, new_id, project, task_id)
    try:
        execute(f"UPDATE tasks SET {set_sql} WHERE project=? AND id=?", base_args)
    except sqlite3.IntegrityError:
        # 目标项目已占用该 id:改用目标项目 max(id)+1
        new_id = query("SELECT COALESCE(MAX(id),0)+1 AS nid FROM tasks WHERE project=?",
                       (new_project,))[0]["nid"]
        execute(f"UPDATE tasks SET {set_sql} WHERE project=? AND id=?",
                (*fields.values(), new_project, new_id, project, task_id))
    return {"ok": True, "project": new_project, "id": new_id}, None


def submit_task_for_review(project: str, task_id: int, data: dict) -> tuple[dict, str | None]:
    notes = data.get("test_notes")
    if not isinstance(notes, str) or not notes.strip():
        return {}, "test_notes 必填且不能为空"
    task = fetch_task(project, task_id)
    if not task:
        return {}, "任务不存在"
    if task["status"] != "进行中":
        return {}, "仅「进行中」状态可提交审核"
    end = now_str()
    if data.get("actual_hours") is not None:
        hours_val = data["actual_hours"]
        if not isinstance(hours_val, (int, float)) or isinstance(hours_val, bool):
            return {}, "actual_hours 必须是数字"
        hours = round(float(hours_val), 1)
    elif task["start_time"]:
        start = datetime.strptime(task["start_time"], TIME_FMT)
        elapsed = datetime.strptime(end, TIME_FMT) - start
        hours = round(elapsed.total_seconds() / 3600, 1)
    else:
        hours = 0.0
    execute(
        "UPDATE tasks SET status='待审核', end_time=?, actual_hours=?, progress=100,"
        " test_notes=?, updated_at=? WHERE project=? AND id=?",
        (end, hours, notes.strip(), now_str(), project, task_id),
    )
    return {"project": project, "id": task_id, "actual_hours": hours}, None


def review_task(project: str, task_id: int, data: dict) -> tuple[dict, str | None]:
    action = data.get("action")
    review_notes = data.get("review_notes", "")
    if not isinstance(review_notes, str):
        return {}, "review_notes 必须是字符串"
    review_notes = review_notes.strip()

    task = fetch_task(project, task_id)
    if not task:
        return {}, "任务不存在"
    if task["status"] != "待审核":
        return {}, "仅「待审核」状态可执行审核操作"
    if action == "approve":
        execute(
            "UPDATE tasks SET status='已完成', review_notes=?, updated_at=? WHERE project=? AND id=?",
            (review_notes, now_str(), project, task_id),
        )
        return {"project": project, "id": task_id, "status": "已完成"}, None
    if action == "reject":
        progress = data.get("progress", 80)
        if not isinstance(progress, (int, float)) or isinstance(progress, bool):
            return {}, "progress 必须是数字"
        if not 0 <= progress <= 100:
            return {}, "progress 必须在 0-100 之间"
        execute(
            "UPDATE tasks SET status='进行中', end_time=NULL, progress=?, review_notes=?, updated_at=? WHERE project=? AND id=?",
            (int(progress), review_notes, now_str(), project, task_id),
        )
        return {"project": project, "id": task_id, "status": "进行中"}, None
    return {}, "action 只能是 approve 或 reject"


def list_projects() -> list[dict]:
    return query("""
        SELECT p.name, p.mode, p.work_stats, p.created_at,
          (SELECT COUNT(*) FROM tasks t WHERE t.project=p.name
             AND t.status NOT IN ('已完成','已取消')) AS open_count,
          (SELECT COUNT(*) FROM tasks t WHERE t.project=p.name) AS total_count
        FROM projects p
    """ + PROJECT_ORDER_SQL)


def parse_work_stats(data: dict, default: int | None = None) -> tuple[int | None, str | None]:
    value = data.get("work_stats", default)
    if value not in (0, 1, False, True):
        return None, "work_stats 只能是 true 或 false"
    return int(value), None


def create_project(data: dict) -> tuple[dict, str | None]:
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return {}, "name 必填且必须是字符串"
    name = name.strip()
    mode = data.get("mode", "活跃")
    if mode not in MODES:
        return {}, f"mode 只能是:{'/'.join(MODES)}"
    work_stats, err = parse_work_stats(data, 1)
    if err:
        return {}, err
    if query("SELECT 1 FROM projects WHERE name=?", (name,)):
        return {}, f"项目「{name}」已存在"
    execute("INSERT INTO projects (name, mode, work_stats) VALUES (?, ?, ?)",
            (name, mode, work_stats))
    return {"name": name, "mode": mode, "work_stats": work_stats}, None


def update_project(name: str, data: dict) -> tuple[dict, str | None]:
    rows = query("SELECT mode, work_stats FROM projects WHERE name=?", (name,))
    if not rows:
        return {}, f"项目「{name}」不存在"
    mode = data.get("mode", rows[0]["mode"])
    if mode not in MODES:
        return {}, f"mode 只能是:{'/'.join(MODES)}"
    work_stats, err = parse_work_stats(data, rows[0]["work_stats"])
    if err:
        return {}, err
    execute("UPDATE projects SET mode=?, work_stats=? WHERE name=?",
            (mode, work_stats, name))
    return {"name": name, "mode": mode, "work_stats": work_stats}, None


def write_snapshot() -> dict:
    """生成 snapshot.json。内容为 `window.TODO_SNAPSHOT = {JSON}`,
    以便 index.html 在 file:// 下用 <script> 标签加载只读数据。"""
    payload = {"exported_at": now_str(),
               "projects": list_projects(),
               "tasks": query("SELECT * FROM tasks " + ORDER_SQL)}
    text = "window.TODO_SNAPSHOT = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n"
    SNAPSHOT_PATH.write_text(text, encoding="utf-8")
    return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, msg: str) -> None:
        self._send_json(code, {"error": msg})

    def _read_body(self) -> object:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/":
            body = INDEX_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/tasks":
            tasks, err = list_tasks(parse_query(url.query))
            self._error(400, err) if err else self._send_json(200, tasks)
        elif url.path == "/api/projects":
            self._send_json(200, list_projects())
        else:
            m = re.fullmatch(r"/api/tasks/(\d+)", url.path)
            if not m:
                self._error(404, "接口不存在")
                return
            project, err = require_project(parse_query(url.query))
            if err:
                self._error(400, err)
                return
            task = fetch_task(project, int(m.group(1)))
            self._error(404, "任务不存在") if not task else self._send_json(200, task)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        try:
            data = self._read_body()
        except (ValueError, UnicodeDecodeError):
            self._error(400, "请求体不是合法 JSON")
            return
        if url.path == "/api/tasks":
            result, err = create_task(data)
            self._error(400, err) if err else self._send_json(201, result)
        elif url.path == "/api/projects":
            result, err = create_project(data)
            self._error(400, err) if err else self._send_json(201, result)
        elif url.path == "/api/export":
            snap = write_snapshot()
            self._send_json(200, {"ok": True, "count": len(snap["tasks"])})
        else:
            m = re.fullmatch(r"/api/tasks/(\d+)/submit-review", url.path)
            if m:
                project, err = require_project(data)
                if err:
                    self._error(400, err)
                    return
                result, err = submit_task_for_review(project, int(m.group(1)), data)
                self._error(400, err) if err else self._send_json(200, result)
                return
            m = re.fullmatch(r"/api/tasks/(\d+)/review", url.path)
            if m:
                project, err = require_project(data)
                if err:
                    self._error(400, err)
                    return
                result, err = review_task(project, int(m.group(1)), data)
                self._error(400, err) if err else self._send_json(200, result)
                return
            self._error(404, "接口不存在")

    def do_DELETE(self) -> None:
        m = re.fullmatch(r"/api/tasks/(\d+)", urlparse(self.path).path)
        if not m:
            self._error(404, "接口不存在")
            return
        project, err = require_project(parse_query(urlparse(self.path).query))
        if err:
            self._error(400, err)
            return
        task_id = int(m.group(1))
        if not fetch_task(project, task_id):
            self._error(404, "任务不存在")
            return
        execute("DELETE FROM tasks WHERE project=? AND id=?", (project, task_id))
        self._send_json(200, {"ok": True})

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/projects/(.+)", unquote(decode_latin1(path)))
        if m:
            try:
                data = self._read_body()
            except (ValueError, UnicodeDecodeError):
                self._error(400, "请求体不是合法 JSON")
                return
            result, err = update_project(m.group(1), data)
            self._error(400, err) if err else self._send_json(200, result)
            return
        m = re.fullmatch(r"/api/tasks/(\d+)", path)
        if not m:
            self._error(404, "接口不存在")
            return
        try:
            data = self._read_body()
        except (ValueError, UnicodeDecodeError):
            self._error(400, "请求体不是合法 JSON")
            return
        project, err = require_project(data)
        if err:
            self._error(400, err)
            return
        result, err = update_task(project, int(m.group(1)), data)
        self._error(400, err) if err else self._send_json(200, result)


def main() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executescript(SCHEMA)
        project_columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
        if "work_stats" not in project_columns:
            conn.execute(
                "ALTER TABLE projects ADD COLUMN work_stats INTEGER NOT NULL DEFAULT 1 "
                "CHECK(work_stats IN (0,1))"
            )
        conn.commit()
    if "--export" in sys.argv[1:]:
        snap = write_snapshot()
        print(f"已导出 {len(snap['tasks'])} 条任务到 snapshot.json")
        return
    port = int(os.environ.get("TODO_PORT") or 8765)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except (OSError, ValueError):
        print(f"端口 {port} 无法监听,启动失败", file=sys.stderr)
        sys.exit(1)
    print(f"服务已启动:http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
