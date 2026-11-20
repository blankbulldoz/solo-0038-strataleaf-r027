"""回归验证:布尔筛选结果完整性、固定修订分页与既有写路径行为。

运行(仓库根目录): python3 -m verification.regression_query
退出码 0 表示全部通过;1 表示存在失败,输出为带归因类别的 JSON。

预期模型
========
期望的有序键、正文、实际文档版本与读修订全部由 verification.oracle 独立确定:
手工推导的字面量 + 独立参照评估器 + 显式提交时间线(只有文档批次推进 head,
目录与索引操作不推进)。不借用 strataleaf 的 predicates/query/aggregate 实现,
也不把 force_scan 或聚合当作唯一真值——它们与被测路径可能认同同一种错误,
因此只作为与独立预言机并列的对照,双向比对。

失败归因(category 字段)
=======================
- semantics    条件语义错误。force_scan 对每个可见文档逐一判定、不裁剪候选,
               若其结果偏离独立预期,问题出在谓词语义本身。
- access_path  访问路径漏候选。扫描结果 == 预期而自动计划(可能走索引)结果
               != 预期,说明索引候选集不是匹配结果的超集。
- pagination   分页截断。分页拼接 != 单次结果/预期,或键重复、乱序、读修订漂移。
- timeline     修订时间线不符。head、读修订或文档版本与独立时间线矛盾
               (包括索引/目录操作错误地产生文档修订)。
- behavior     既有行为回归。索引维护、唯一约束拒绝、失败批次回滚、
               游标签名与保留水位语义。

等价变换的适用边界
==================
每个叶子条件对一篇文档是确定的全函数:缺失字段参与比较求值为假(不是三值
逻辑),因此对同一组叶子做交换律、结合律、分配律、德摩根、双重否定,在缺失
字段下依然精确成立——不需要、也不允许先把缺失补成 null。超出该模型的改写
不保证成立:
- not(eq f v) 不能改写成 ne f v(字段缺失时前者为真、后者为假);
- 任何改变叶子集合或做值规范化(缺失→null、布尔↔数值)的"等价"都不成立;
- 把 OR 分支下的等值项当作索引候选生成器正是本次修复的缺陷,不是合法变换;
  索引候选只对合取(and)可达的 eq 叶子可靠。
"""
import json
import sys
import tempfile
from pathlib import Path

from strataleaf import Conflict, Database, InvalidCursor, SnapshotTooOld
from verification import oracle
from verification.oracle import (COMPOSITE_CASES, DOCS, EQUIVALENT_FORMS, EXPECTED_P,
                                 LEAF_CASES, P, REGION_EAST, SCOPE_PROD, Timeline,
                                 expected_keys, seed)

CHECKS = 0
FAILURES = []


def check(category, name, actual, expected):
    global CHECKS
    CHECKS += 1
    if actual != expected:
        FAILURES.append({"category": category, "check": name,
                         "expected": expected, "actual": actual})


def check_raises(category, name, call, error):
    global CHECKS
    CHECKS += 1
    try:
        call()
    except error:
        return
    except Exception as exc:
        FAILURES.append({"category": category, "check": name, "expected": error.__name__,
                         "actual": type(exc).__name__ + ": " + str(exc)})
        return
    FAILURES.append({"category": category, "check": name, "expected": error.__name__,
                     "actual": "success"})


def keys_of(page):
    return [record.key for record in page.records]


def bodies_of(page):
    return {record.key: record.document for record in page.records}


def versions_of(page):
    return {record.key: record.revision for record in page.records}


def plan_of(page):
    return (page.plan["access"], page.plan["index"])


INDEX_CONFIGS = [
    ("no_index", {}),
    ("region", {"by_region": "region"}),
    ("priority", {"by_priority": "priority"}),
    ("region+priority", {"by_region": "region", "by_priority": "priority"}),
    ("scope", {"by_scope": "scope"}),
    ("scope+region+priority", {"by_scope": "scope", "by_region": "region", "by_priority": "priority"}),
]

PREDICATES = ([("P", P, EXPECTED_P)]
              + [(name, predicate, EXPECTED_P) for name, predicate in EQUIVALENT_FORMS]
              + list(COMPOSITE_CASES))


def check_completeness(db, timeline):
    """结果完整性矩阵:索引组合 x 等价/组合条件,以及叶子语义与索引路径保留。"""
    seed(db)
    timeline.committed(**{key: DOCS[key] for key in sorted(DOCS)})
    check("timeline", "seed advances head once", db.revision, timeline.head)

    for config_name, indexes in INDEX_CONFIGS:
        for existing in db.indexes.list("docs"):
            db.indexes.drop("docs", existing["name"])
        for index_name, path in indexes.items():
            db.indexes.create("docs", index_name, path)
        check("timeline", config_name + ": indexes create no document revision",
              db.revision, timeline.head)
        for pred_name, predicate, expected in PREDICATES:
            label = config_name + "/" + pred_name
            check("semantics", label + ": oracle self-check", expected_keys(predicate), expected)
            scan = db.query("docs", predicate, limit=1000, force_scan=True)
            check("semantics", label + ": full scan", keys_of(scan), expected)
            auto = db.query("docs", predicate, limit=1000)
            check("access_path", label + ": auto plan result", keys_of(auto), expected)
            check("timeline", label + ": read revision", auto.revision, timeline.head)
        page = db.query("docs", P, limit=1000)
        expected_plan = ("equality_index", "by_scope") if "by_scope" in indexes else ("version_scan", None)
        check("access_path", config_name + "/P: plan shape", plan_of(page), expected_plan)
        check("semantics", config_name + "/P: bodies", bodies_of(page),
              {key: DOCS[key] for key in EXPECTED_P})
        check("timeline", config_name + "/P: document versions", versions_of(page),
              {key: timeline.head for key in EXPECTED_P})

    # 叶子语义:缺失、显式 null、结构值、布尔/数值(此时三个索引都在)。
    for leaf_name, predicate, expected in LEAF_CASES:
        auto = db.query("docs", predicate, limit=1000)
        check("semantics", "leaf/" + leaf_name, keys_of(auto), expected)
        scan = db.query("docs", predicate, limit=1000, force_scan=True)
        check("access_path", "leaf/" + leaf_name + ": scan agrees", keys_of(scan), expected)

    # 简单等值与合取查询保留原有等值索引路径。
    simple = db.query("docs", REGION_EAST, limit=1000)
    check("access_path", "simple equality keeps index path", plan_of(simple), ("equality_index", "by_region"))
    check("access_path", "simple equality keys", keys_of(simple), ["a", "d", "g"])
    conjunctive = {"and": [SCOPE_PROD, REGION_EAST]}
    conj = db.query("docs", conjunctive, limit=1000)
    check("access_path", "conjunctive keeps index path", plan_of(conj), ("equality_index", "by_scope"))
    check("access_path", "conjunctive keys", keys_of(conj), ["a", "g"])
    nested = db.query("docs", {"and": [SCOPE_PROD, {"and": [REGION_EAST]}]}, limit=1000)
    check("access_path", "nested conjunctive keys", keys_of(nested), ["a", "g"])
    for existing in db.indexes.list("docs"):
        if existing["name"] != "by_region":
            db.indexes.drop("docs", existing["name"])
    conj = db.query("docs", conjunctive, limit=1000)
    check("access_path", "conjunctive plan with region index only", plan_of(conj), ("equality_index", "by_region"))
    check("access_path", "conjunctive keys with region index only", keys_of(conj), ["a", "g"])
    check("access_path", "conjunctive scan agrees",
          keys_of(db.query("docs", conjunctive, limit=1000, force_scan=True)), ["a", "g"])

    # 移除索引不改变结果,也不产生文档修订。
    db.indexes.drop("docs", "by_region")
    check("timeline", "dropping all indexes creates no revision", db.revision, timeline.head)
    check("access_path", "P after indexes removed", keys_of(db.query("docs", P, limit=1000)), EXPECTED_P)


def check_pagination(directory, limit):
    """固定修订分页:第一页后改写/删除/新增,旧遍历仍完整,新查询读新集合。"""
    path = Path(directory) / ("paged%d.db" % limit)
    timeline = Timeline()
    db = Database(path)
    seed(db)
    timeline.committed(**{key: DOCS[key] for key in sorted(DOCS)})
    r1 = timeline.head
    db.indexes.create("docs", "by_region", "region")
    db.indexes.create("docs", "by_priority", "priority")
    check("timeline", "limit%d: indexes create no revision" % limit, db.revision, r1)
    db.retention.pin("hold", r1)

    new_key = "A" if limit == 1 else "ab"  # 排在旧断点("a"/"b")之前的匹配键
    rewritten_b = {"scope": "prod", "region": "south", "priority": False}
    new_doc = {"scope": "prod", "region": "east", "priority": False}

    pages = [db.query("docs", P, limit=limit)]
    check("timeline", "limit%d: first page read revision" % limit, pages[0].revision, r1)
    check("access_path", "limit%d: first page plan" % limit, plan_of(pages[0]), ("version_scan", None))
    cursor = pages[0].cursor

    # 第一页之后,另开句柄:改写 b 使其不再匹配、删除 a、新增排在旧断点前的匹配键。
    with Database(path) as other:
        (other.batch("mutate")
               .put("docs", "b", rewritten_b, expected=r1)
               .delete("docs", "a", expected=r1)
               .put("docs", new_key, new_doc, expected=0)
               .commit())
    timeline.committed(**{"b": rewritten_b, "a": None, new_key: new_doc})
    check("timeline", "limit%d: mutation advances head" % limit, db.revision, timeline.head)

    # 遍历中途创建/移除索引:不产生文档修订,也不改变本次遍历的含义。
    db.indexes.create("docs", "by_scope", "scope")
    check("timeline", "limit%d: mid-traversal index create no revision" % limit, db.revision, timeline.head)
    dropped = reopened = False
    while cursor:
        page = db.query("docs", P, limit=limit, cursor=cursor)
        check("timeline", "limit%d: continuation keeps read revision" % limit, page.revision, r1)
        pages.append(page)
        cursor = page.cursor
        if not dropped:
            db.indexes.drop("docs", "by_region")
            check("timeline", "limit%d: mid-traversal index drop no revision" % limit,
                  db.revision, timeline.head)
            dropped = True
        if not reopened:
            db.close()  # 历史仍保留(pin 持有),关闭重开后用同一游标继续
            db = Database(path)
            reopened = True

    keys = [record.key for page in pages for record in page.records]
    check("pagination", "limit%d: old traversal complete" % limit, keys, EXPECTED_P)
    check("pagination", "limit%d: each key once, in order" % limit,
          keys == sorted(keys) and len(set(keys)) == len(keys), True)
    check("pagination", "limit%d: old bodies preserved" % limit,
          {record.key: record.document for page in pages for record in page.records},
          {key: DOCS[key] for key in EXPECTED_P})
    check("timeline", "limit%d: old document versions" % limit,
          {record.key: record.revision for page in pages for record in page.records},
          {key: r1 for key in EXPECTED_P})

    # 新查询读取修改后的集合;显式旧修订的重放仍返回原集合。
    current = {key: timeline.body_at(key, timeline.head)
               for key in timeline.writes if timeline.body_at(key, timeline.head) is not None}
    expected_new = oracle.expected_keys(P, current)
    fresh = db.query("docs", P, limit=1000)
    check("access_path", "limit%d: fresh query uses scope index" % limit,
          plan_of(fresh), ("equality_index", "by_scope"))
    check("pagination", "limit%d: fresh query reads mutated set" % limit, keys_of(fresh), expected_new)
    check("timeline", "limit%d: fresh query revision" % limit, fresh.revision, timeline.head)
    check("timeline", "limit%d: fresh document versions" % limit, versions_of(fresh),
          {key: timeline.version_at(key, timeline.head) for key in expected_new})
    check("access_path", "limit%d: fresh scan agrees" % limit,
          keys_of(db.query("docs", P, limit=1000, force_scan=True)), expected_new)
    replay = db.query("docs", P, limit=1000, revision=r1)
    check("pagination", "limit%d: explicit old revision replays original" % limit,
          keys_of(replay), EXPECTED_P)

    # 游标固定谓词与读修订;保留水位越过旧读修订后明确拒绝。
    stale = db.query("docs", P, limit=1, revision=r1)
    check_raises("behavior", "limit%d: cursor rejects other revision" % limit,
                 lambda: db.query("docs", P, limit=1, cursor=stale.cursor, revision=timeline.head),
                 InvalidCursor)
    check_raises("behavior", "limit%d: cursor rejects other predicate" % limit,
                 lambda: db.query("docs", SCOPE_PROD, limit=1, cursor=stale.cursor), InvalidCursor)
    blocked = db.retention.compact(timeline.head)
    check("behavior", "limit%d: pin blocks compaction" % limit,
          (blocked["floor"], blocked["blocked"]), (r1, True))
    db.retention.unpin("hold")
    advanced = db.retention.compact(timeline.head)
    check("behavior", "limit%d: compaction advances past read revision" % limit,
          (advanced["floor"], advanced["blocked"]), (timeline.head, False))
    check_raises("behavior", "limit%d: stale cursor refused" % limit,
                 lambda: db.query("docs", P, limit=1, cursor=stale.cursor), SnapshotTooOld)
    check_raises("behavior", "limit%d: stale revision refused" % limit,
                 lambda: db.query("docs", P, limit=1, revision=r1), SnapshotTooOld)
    check("behavior", "limit%d: floor view still readable" % limit,
          keys_of(db.query("docs", P, limit=1000, revision=timeline.head)), expected_new)
    check("behavior", "limit%d: floor base version retained" % limit,
          db.get("docs", "e", revision=timeline.head).document, DOCS["e"])
    db.close()


def check_write_path(db, timeline):
    """既有行为:唯一约束拒绝、失败批次回滚、索引维护跟随写批次。"""
    check_raises("behavior", "unique index create conflict",
                 lambda: db.indexes.create("docs", "u_region", "region", unique=True), Conflict)
    check("behavior", "failed index leaves nothing behind", db.indexes.list("docs"), [])
    check("timeline", "failed index creates no revision", db.revision, timeline.head)

    db.catalog.create("uniq")
    db.batch().put("uniq", "k1", {"code": "x"}).put("uniq", "k2", {"code": "y"}).commit()
    timeline.committed()
    db.indexes.create("uniq", "u_code", "code", unique=True)
    check_raises("behavior", "unique put rejected",
                 lambda: db.batch().put("uniq", "k3", {"code": "x"}).commit(), Conflict)
    check("timeline", "rejected put creates no revision", db.revision, timeline.head)
    check_raises("behavior", "conflicting batch rolls back",
                 lambda: db.batch().put("uniq", "k4", {"code": "z"})
                                  .put("uniq", "k5", {"code": "x"}).commit(), Conflict)
    check("behavior", "rolled back document absent", db.get("uniq", "k4"), None)
    check("timeline", "rolled back batch creates no revision", db.revision, timeline.head)
    check_raises("behavior", "revision conflict rolls back whole batch",
                 lambda: db.batch().put("docs", "z1", {"scope": "prod"})
                                   .put("docs", "a", {"scope": "prod"}, expected=999).commit(),
                 Conflict)
    check("behavior", "conflict batch leaves no partial write", db.get("docs", "z1"), None)
    check("timeline", "conflict batch creates no revision", db.revision, timeline.head)

    db.indexes.create("docs", "by_region", "region")
    db.batch().put("docs", "n1", {"scope": "prod", "region": "east", "priority": False}).commit()
    timeline.committed(n1={"scope": "prod", "region": "east", "priority": False})
    db.batch().delete("docs", "c").commit()
    timeline.committed(c=None)
    east = db.query("docs", REGION_EAST, limit=1000)
    check("behavior", "index maintained on writes", plan_of(east), ("equality_index", "by_region"))
    check("behavior", "index sees new document", keys_of(east), ["a", "d", "g", "n1"])
    west = db.query("docs", {"field": "region", "op": "eq", "value": "west"}, limit=1000)
    check("behavior", "index reflects delete", keys_of(west), ["b", "j"])
    check("behavior", "scan agrees after writes",
          keys_of(db.query("docs", REGION_EAST, limit=1000, force_scan=True)), ["a", "d", "g", "n1"])
    check("behavior", "deep integrity inspection", db.diagnostics.inspect(deep=True)["ok"], True)


def run():
    with tempfile.TemporaryDirectory(prefix="strataleaf-regression-") as directory:
        timeline = Timeline()
        with Database(Path(directory) / "main.db") as db:
            check_completeness(db, timeline)
            check_write_path(db, timeline)
        for limit in (1, 2):
            check_pagination(directory, limit)
    report = {"ok": not FAILURES, "checks": CHECKS}
    if FAILURES:
        report["failures"] = FAILURES
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(run())
