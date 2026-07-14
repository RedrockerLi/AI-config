#!/usr/bin/env python3
"""清理 survey DB 中 [JSON parse failed] 的分类结果。

将所有 relevance_reason 以 "[JSON parse failed]" 开头的 survey_result
重置为未分类状态，并将对应 paper 的 flag 重置为 'unclaimed'。

用法:
    python scripts/cleanup_parse_failed.py surveys/survey_1.db
    python scripts/cleanup_parse_failed.py surveys/survey_1.db --dry-run
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def cleanup(db_path: str, dry_run: bool = False) -> int:
    """清理指定 survey DB 中的 [JSON parse failed] 行，返回清理数量。"""
    if not Path(db_path).exists():
        print(f"错误: 数据库文件不存在: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. 查找所有受影响的 survey_result 行
    rows = conn.execute(
        "SELECT id AS result_id, paper_id, relevance_reason "
        "FROM survey_result "
        "WHERE relevance_reason LIKE '[JSON parse failed]%'"
    ).fetchall()

    if not rows:
        print("没有找到 [JSON parse failed] 的记录，无需清理。")
        conn.close()
        return 0

    count = len(rows)
    print(f"找到 {count} 条 [JSON parse failed] 记录")

    if dry_run:
        print("\n[Dry-run 模式] 以下记录将被重置：")
        for r in rows:
            reason_preview = r["relevance_reason"][:80]
            print(f"  result_id={r['result_id']}  paper_id={r['paper_id']}  reason={reason_preview}...")
        print(f"\n[Dry-run] 共 {count} 条记录将被清理（未实际修改）。")
        conn.close()
        return count

    # 2. 收集 paper_id 列表用于重置 flag
    paper_ids = [r["paper_id"] for r in rows]
    result_ids = [r["result_id"] for r in rows]

    # 3. 在同一事务中执行清理
    with conn:
        # 重置 survey_result 为未分类
        for rid in result_ids:
            conn.execute(
                "UPDATE survey_result "
                "SET is_relevant = NULL, relevance_reason = '', confidence = 0.0, "
                "    analysis_json = '', classified_at = NULL "
                "WHERE id = ?",
                (rid,),
            )

        # 重置 paper flag 为 unclaimed
        for pid in paper_ids:
            conn.execute(
                "UPDATE paper SET flag = 'unclaimed' WHERE id = ?",
                (pid,),
            )

    conn.close()
    print(f"已清理 {count} 条记录：survey_result 已重置，{len(set(paper_ids))} 篇论文已标记为 unclaimed。")
    return count


def main():
    parser = argparse.ArgumentParser(
        description="清理 survey DB 中 [JSON parse failed] 的分类结果"
    )
    parser.add_argument(
        "db_path",
        help="survey 数据库路径，例如 surveys/survey_1.db",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="仅预览，不实际修改数据库",
    )
    args = parser.parse_args()
    cleanup(args.db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
