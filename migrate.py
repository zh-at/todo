#!/usr/bin/env python3
"""一次性迁移:各项目独立 todo 库 → todo-center 中心库(2026-09-02)。

- 冷备源库(app.db.premigrate-20260902)后合并,复合主键 (project,id) 保留原 id
- 制度发布审批管控为休眠项目,无历史库,仅预注册为归档
- 前置:旧服务已停止;中心库 tasks 为空(需重跑先手动删 app.db)

用法: python3 migrate.py
"""

import shutil
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from app import SCHEMA  # 复用中心库 schema,防止两处定义漂移

WORKSPACE = Path.home() / "kingsware" / "workspace"
BACKUP_SUFFIX = ".premigrate-20260902"
SOURCES = [
    ("质量看板系统", WORKSPACE / "质量看板系统" / "todo" / "app.db", "维护"),
    ("服务器资源管理系统", WORKSPACE / "服务器资源管理系统" / "todo" / "app.db", "活跃"),
]
EXTRA_PROJECTS = [("制度发布审批管控", "归档")]
ARCHIVE_SRC = WORKSPACE / "质量看板系统" / "todo" / "archive"
ARCHIVE_DST = BASE / "archive" / "质量看板系统"
SPOT_CHECKS = [("质量看板系统", 166), ("质量看板系统", 181), ("服务器资源管理系统", 13)]
COLS = ["id", "type", "title", "detail", "reporter", "status", "priority", "progress",
        "start_time", "end_time", "plan_start", "due_date", "est_hours", "actual_hours",
        "test_notes", "review_notes", "created_at", "updated_at"]
DB = BASE / "app.db"


def source_counts(path: Path) -> tuple[int, int]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        cnt, mx = conn.execute("SELECT COUNT(*), COALESCE(MAX(id),0) FROM tasks").fetchone()
    return cnt, mx


def main() -> None:
    if DB.exists():
        with sqlite3.connect(DB) as conn:
            n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        if n:
            sys.exit(f"✗ 中心库已有 {n} 条任务,拒绝重跑(确需重跑先删 {DB})")

    print("== 1/5 冷备源库 ==")
    for name, src, _ in SOURCES:
        bak = src.parent / ("app.db" + BACKUP_SUFFIX)
        shutil.copy2(src, bak)
        print(f"  {name}: {src} → {bak}")

    print("== 2/5 建中心库 + 预注册项目 ==")
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    for name, mode in [(n, m) for n, _, m in SOURCES] + EXTRA_PROJECTS:
        conn.execute("INSERT OR IGNORE INTO projects (name, mode) VALUES (?, ?)", (name, mode))
        print(f"  项目 {name} = {mode}")
    conn.commit()

    print("== 3/5 迁移任务(保原 id) ==")
    for name, src, _ in SOURCES:
        cnt, mx = source_counts(src)
        conn.execute(f"ATTACH ? AS src", (str(src),))
        col_list = ", ".join(COLS)
        conn.execute(
            f"INSERT INTO tasks (project, {col_list}) "
            f"SELECT ?, {col_list} FROM src.tasks", (name,))
        got_cnt, got_mx = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id),0) FROM tasks WHERE project=?", (name,)).fetchone()
        if (got_cnt, got_mx) != (cnt, mx):
            conn.close()
            sys.exit(f"✗ {name} 迁移后不一致: 期望 {cnt}条/max={mx}, 实际 {got_cnt}条/max={got_mx}")
        conn.commit()
        conn.execute("DETACH src")
        print(f"  {name}: {cnt} 条迁移完成, max_id={mx}")

    print("== 4/5 抽查标题一致性 ==")
    for name, tid in SPOT_CHECKS:
        center_title = conn.execute(
            "SELECT title FROM tasks WHERE project=? AND id=?", (name, tid)).fetchone()
        src = next(s for s in SOURCES if s[0] == name)[1]
        with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as ro:
            src_title = ro.execute("SELECT title FROM tasks WHERE id=?", (tid,)).fetchone()
        if center_title != src_title:
            conn.close()
            sys.exit(f"✗ 抽查不一致 {name}#{tid}: 中心={center_title} 源={src_title}")
        print(f"  {name}#{tid}: 「{(center_title or ['?'])[0]}」一致")

    conn.commit()
    conn.close()

    print("== 5/5 归档目录 ==")
    if ARCHIVE_SRC.exists():
        shutil.copytree(ARCHIVE_SRC, ARCHIVE_DST, dirs_exist_ok=True)
        print(f"  {ARCHIVE_SRC} → {ARCHIVE_DST}")
    else:
        print("  源归档目录不存在,跳过")

    total = sum(source_counts(s[1])[0] for s in SOURCES)
    print(f"✓ 迁移完成:共 {total} 条任务,中心库 {DB}")


if __name__ == "__main__":
    main()
