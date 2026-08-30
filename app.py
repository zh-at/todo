#!/usr/bin/env python3
"""个人任务/缺陷管理工具 —— 仅标准库(http.server + sqlite3 + json)。

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
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "app.db"
INDEX_PATH = BASE_DIR / "index.html"
SNAPSHOT_PATH = BASE_DIR / "snapshot.json"

TYPES = ("task", "bug")
STATUSES = ("未开始", "进行中", "已完成", "已取消", "待审核")
PRIORITIES = ("必须做", "应该做", "可不做")
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
         due_date IS NULL, due_date ASC, id ASC
"""

SCHEMA = """
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
  start_time   TEXT,              -- 'YYYY-MM-DD HH:MM:SS',进入"进行中"时自动补(实际值,只读)
  end_time     TEXT,              -- 置"已完成"时自动补(实际值,只读)
  plan_start   TEXT,              -- 'YYYY-MM-DD',计划开始(排期预期值)
  due_date     TEXT,              -- 'YYYY-MM-DD',计划结束/DDL(排期预期值)
  est_hours    REAL NOT NULL DEFAULT 0,
  actual_hours REAL NOT NULL DEFAULT 0,
  test_notes   TEXT NOT NULL DEFAULT '',
  review_notes TEXT NOT NULL DEFAULT '',  -- 审核结果/拒绝原因
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
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


def create_task(data: dict) -> tuple[int, str | None]:
    fields, err = clean_fields(data)
    if err:
        return 0, err
    if not fields.get("title"):
        return 0, "标题不能为空"
    if "type" not in fields:
        return 0, "缺少 type(task 或 bug)"
    cols = list(fields)
    sql = f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    return execute(sql, tuple(fields.values())), None


def update_task(task_id: int, data: dict) -> tuple[int, str | None]:
    fields, err = clean_fields(data)
    if err:
        return 0, err
    if not fields:
        return 0, "没有可更新的字段"
    rows = query("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        return 0, "任务不存在"
    old = rows[0]
    new_status = fields.get("status", old["status"])
    if new_status == "进行中" and not old["start_time"] and not fields.get("start_time"):
        fields["start_time"] = now_str()
    if new_status == "已完成":
        if not old["end_time"] and not fields.get("end_time"):
            fields["end_time"] = now_str()
        if "progress" not in fields:
            fields["progress"] = 100
    fields["updated_at"] = now_str()
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))
    return task_id, None


def close_task(task_id: int, data: dict) -> tuple[dict, str | None]:
    notes = data.get("test_notes")
    if not isinstance(notes, str) or not notes.strip():
        return {}, "test_notes 必填且不能为空"
    rows = query("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        return {}, "任务不存在"
    task = rows[0]
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
        " test_notes=?, updated_at=? WHERE id=?",
        (end, hours, notes.strip(), now_str(), task_id),
    )
    return {"id": task_id, "actual_hours": hours}, None


def review_task(task_id: int, data: dict) -> tuple[dict, str | None]:
    action = data.get("action")
    review_notes = data.get("review_notes", "")
    if not isinstance(review_notes, str):
        return {}, "review_notes 必须是字符串"
    review_notes = review_notes.strip()
    
    rows = query("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        return {}, "任务不存在"
    if rows[0]["status"] != "待审核":
        return {}, "仅「待审核」状态可执行审核操作"
    if action == "approve":
        execute(
            "UPDATE tasks SET status='已完成', review_notes=?, updated_at=? WHERE id=?",
            (review_notes, now_str(), task_id),
        )
        return {"id": task_id, "status": "已完成"}, None
    if action == "reject":
        progress = data.get("progress", 80)
        if not isinstance(progress, (int, float)) or isinstance(progress, bool):
            return {}, "progress 必须是数字"
        if not 0 <= progress <= 100:
            return {}, "progress 必须在 0-100 之间"
        execute(
            "UPDATE tasks SET status='进行中', end_time=NULL, progress=?, review_notes=?, updated_at=? WHERE id=?",
            (int(progress), review_notes, now_str(), task_id),
        )
        return {"id": task_id, "status": "进行中"}, None
    return {}, "action 只能是 approve 或 reject"

def write_snapshot() -> dict:
    """生成 snapshot.json。内容为 `window.TODO_SNAPSHOT = {JSON}`,
    以便 index.html 在 file:// 下用 <script> 标签加载只读数据。"""
    payload = {"exported_at": now_str(),
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
            tasks, err = list_tasks(parse_qs(url.query))
            self._error(400, err) if err else self._send_json(200, tasks)
        else:
            self._error(404, "接口不存在")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        try:
            data = self._read_body()
        except (ValueError, UnicodeDecodeError):
            self._error(400, "请求体不是合法 JSON")
            return
        if url.path == "/api/tasks":
            new_id, err = create_task(data)
            self._error(400, err) if err else self._send_json(201, {"id": new_id})
        elif url.path == "/api/export":
            snap = write_snapshot()
            self._send_json(200, {"ok": True, "count": len(snap["tasks"])})
        else:
            m = re.fullmatch(r"/api/tasks/(\d+)/close", url.path)
            if m:
                result, err = close_task(int(m.group(1)), data)
                self._error(400, err) if err else self._send_json(200, result)
                return
            m = re.fullmatch(r"/api/tasks/(\d+)/review", url.path)
            if m:
                result, err = review_task(int(m.group(1)), data)
                self._error(400, err) if err else self._send_json(200, result)
                return
            self._error(404, "接口不存在")

    def do_DELETE(self) -> None:
        m = re.fullmatch(r"/api/tasks/(\d+)", urlparse(self.path).path)
        if not m:
            self._error(404, "接口不存在")
            return
        task_id = int(m.group(1))
        if not query("SELECT id FROM tasks WHERE id=?", (task_id,)):
            self._error(404, "任务不存在")
            return
        execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self._send_json(200, {"ok": True})

    def do_PUT(self) -> None:
        m = re.fullmatch(r"/api/tasks/(\d+)", urlparse(self.path).path)
        if not m:
            self._error(404, "接口不存在")
            return
        try:
            data = self._read_body()
        except (ValueError, UnicodeDecodeError):
            self._error(400, "请求体不是合法 JSON")
            return
        _, err = update_task(int(m.group(1)), data)
        self._error(400, err) if err else self._send_json(200, {"ok": True})


def main() -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if cols:
            # 迁移:老表 CHECK 约束不含「待审核」时重建
            tbl_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
            ).fetchone()
            if tbl_sql and "待审核" not in tbl_sql[0]:
                conn.execute("ALTER TABLE tasks RENAME TO tasks_migrate_old")
                conn.execute(SCHEMA)
                old_cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks_migrate_old)").fetchall()]
                col_list = ", ".join(old_cols)
                conn.execute(f"INSERT INTO tasks ({col_list}) SELECT {col_list} FROM tasks_migrate_old")
                conn.execute("DROP TABLE tasks_migrate_old")
        else:
            conn.execute(SCHEMA)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
        if "plan_start" not in cols:  # 轻量迁移:老库补列
            conn.execute("ALTER TABLE tasks ADD COLUMN plan_start TEXT")
        if "progress" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
        if "review_notes" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN review_notes TEXT NOT NULL DEFAULT ''")
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
