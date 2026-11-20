"""复现:OR 条件在建立等值索引后悄悄漏文档,交换 OR 分支会改变返回的键。

运行(仓库根目录): python3 -m verification.repro_or_index

对同一条件 P = scope="prod" AND (region="east" OR priority=true):
  * 无索引、只建 region 索引、只建 priority 索引、两个都建、再全部移除,
    结果都必须是有序键 [a, b, e, g](每键一次);
  * 交换两个 OR 分支不得改变结果。
退出码 0 表示行为正确;1 表示复现了缺陷,输出每种索引组合实际返回的键。
"""
import json
import sys
import tempfile
from pathlib import Path

from strataleaf import Database
from verification.oracle import DOCS, EXPECTED_P, P, P_OR_SWAPPED, seed


def keys(page):
    return [record.key for record in page.records]


def run():
    observations = {}
    with tempfile.TemporaryDirectory(prefix="strataleaf-repro-") as directory:
        with Database(Path(directory) / "repro.db") as db:
            seed(db)
            observations["no_index"] = keys(db.query("docs", P))
            db.indexes.create("docs", "by_region", "region")
            observations["region_index"] = keys(db.query("docs", P))
            observations["region_index_or_swapped"] = keys(db.query("docs", P_OR_SWAPPED))
            db.indexes.create("docs", "by_priority", "priority")
            observations["both_indexes"] = keys(db.query("docs", P))
            observations["both_indexes_or_swapped"] = keys(db.query("docs", P_OR_SWAPPED))
            db.indexes.drop("docs", "by_region")
            observations["priority_index"] = keys(db.query("docs", P))
            observations["priority_index_or_swapped"] = keys(db.query("docs", P_OR_SWAPPED))
            db.indexes.drop("docs", "by_priority")
            observations["indexes_removed"] = keys(db.query("docs", P))
    broken = {name: found for name, found in observations.items() if found != EXPECTED_P}
    report = {"ok": not broken, "expected": EXPECTED_P, "observed": observations}
    if broken:
        report["broken"] = broken
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(run())
