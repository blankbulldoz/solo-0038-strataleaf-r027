"""独立预期模型:只依据原始输入、条件定义与提交时间线推导期望结果。

本模块刻意不导入 strataleaf 的 predicates/query/aggregate 任何实现,
以免与被测代码共享同一种错误。期望值有两个来源:

1. 手工逐文档推导的字面量(如 EXPECTED_P),是主预言机;
2. ref_match 参照评估器,按 README/docs 记录的语义独立实现,
   仅用于覆盖手工推导容易出错的等价变换条件;
   它本身先对字面量期望自检(ref_match(P) == EXPECTED_P)后才被信任。

语义要点(与 README「语义边界」一致):
- 缺失字段与显式 null 不同;缺失字段对任何比较操作为假,只满足 exists:false;
  绝不把缺失先补成 null 再比较。
- 布尔与数值不相等:true != 1;数值比较不把布尔当数字。
- 结构值(对象/数组)按深度相等比较,标量等值条件不会误中结构值。
"""

MISSING = object()

# 原始输入:宽松 docs 集合的十篇文档(键即有序)。
DOCS = {
    "a": {"scope": "prod", "region": "east", "priority": False},
    "b": {"scope": "prod", "region": "west", "priority": True},
    "c": {"scope": "prod", "region": "west", "priority": False},
    "d": {"scope": "test", "region": "east", "priority": True},
    "e": {"scope": "prod", "priority": True},
    "f": {"scope": "prod", "region": None, "priority": False},
    "g": {"scope": "prod", "region": "east", "priority": True},
    "h": {"scope": "prod", "region": {"zone": "east"}, "priority": False},
    "i": {"scope": "prod", "priority": False},
    "j": {"scope": "prod", "region": "west", "priority": 1},
}

SCOPE_PROD = {"field": "scope", "op": "eq", "value": "prod"}
REGION_EAST = {"field": "region", "op": "eq", "value": "east"}
PRIORITY_TRUE = {"field": "priority", "op": "eq", "value": True}

# 条件 P:scope 等于 "prod",并且 region 等于 "east" 或 priority 等于 true。
P = {"and": [SCOPE_PROD, {"or": [REGION_EAST, PRIORITY_TRUE]}]}

# 手工推导 P(逐文档):
#   a prod ∧ east            命中      c prod,west,false  两支皆假
#   b prod ∧ priority=true   命中      d scope=test       合取失败
#   e prod,region 缺失∧true  命中      f region=null≠east 且 false
#   g prod ∧ east(且 true)   命中      h region 为结构值≠"east" 且 false
#                                      i region 缺失且 false
#                                      j priority=1≠true(布尔≠数值)且 west
EXPECTED_P = ["a", "b", "e", "g"]

# 等价变换(对同一组叶子做纯布尔改写,期望与 P 相同;适用边界见 regression_query.py 模块注释)。
P_OR_SWAPPED = {"and": [SCOPE_PROD, {"or": [PRIORITY_TRUE, REGION_EAST]}]}
P_AND_SWAPPED = {"and": [{"or": [REGION_EAST, PRIORITY_TRUE]}, SCOPE_PROD]}
P_NESTED_AND = {"and": [SCOPE_PROD, {"and": [{"or": [REGION_EAST, PRIORITY_TRUE]}]}]}
P_DISTRIBUTED = {"or": [
    {"and": [SCOPE_PROD, REGION_EAST]},
    {"and": [SCOPE_PROD, PRIORITY_TRUE]},
]}
P_DE_MORGAN = {"and": [SCOPE_PROD, {"not": {"and": [{"not": REGION_EAST}, {"not": PRIORITY_TRUE}]}}]}
P_DOUBLE_NOT = {"not": {"not": P}}

# 外层没有 scope 条件的 OR:region=east(a,d,g)∪ priority=true(b,d,e,g)。
Q_NO_SCOPE = {"or": [REGION_EAST, PRIORITY_TRUE]}
EXPECTED_NO_SCOPE = ["a", "b", "d", "e", "g"]

# OR 嵌在多个 AND 中:P 再合取 (region=west ∨ priority=true)。
# {a,b,e,g} ∩ ({b,c,j} ∪ {b,d,e,g}) = {b,e,g}(a 两支皆假)。
Q_MULTI_AND = {"and": [
    SCOPE_PROD,
    {"or": [REGION_EAST, PRIORITY_TRUE]},
    {"or": [{"field": "region", "op": "eq", "value": "west"}, PRIORITY_TRUE]},
]}
EXPECTED_MULTI_AND = ["b", "e", "g"]

# NOT 包围组合条件:not(scope=test ∨ (¬region=east ∧ ¬priority=true))。
# 内层 and 命中 {c,f,h,i,j},并上 {d} 后取反 → {a,b,e,g}。
Q_NOT_COMPOSITE = {"not": {"or": [
    {"field": "scope", "op": "eq", "value": "test"},
    {"and": [{"not": REGION_EAST}, {"not": PRIORITY_TRUE}]},
]}}
EXPECTED_NOT_COMPOSITE = ["a", "b", "e", "g"]

# 一支只能扫描判定(exists 不是等值项):scope=prod ∧ (region=east ∨ priority 存在)。
# 十篇文档全部带有 priority 字段,故 prod 文档全部命中,region=east 不再新增。
Q_SCAN_BRANCH = {"and": [SCOPE_PROD, {"or": [REGION_EAST, {"field": "priority", "op": "exists", "value": True}]}]}
EXPECTED_SCAN_BRANCH = ["a", "b", "c", "e", "f", "g", "h", "i", "j"]

# 一支只能扫描判定(范围比较):scope=prod ∧ (region=east ∨ priority>0)。
# priority>0 只命中 j(布尔不算数值);region=east 命中 a,g。
Q_RANGE_BRANCH = {"and": [SCOPE_PROD, {"or": [REGION_EAST, {"field": "priority", "op": "gt", "value": 0}]}]}
EXPECTED_RANGE_BRANCH = ["a", "g", "j"]

# 叶子语义抽查(缺失 / 显式 null / 结构值 / 布尔与数值)。
LEAF_CASES = [
    ("eq_null_only_present", {"field": "region", "op": "eq", "value": None}, ["f"]),
    ("eq_structural_value", {"field": "region", "op": "eq", "value": {"zone": "east"}}, ["h"]),
    ("eq_true_not_one", PRIORITY_TRUE, ["b", "d", "e", "g"]),
    ("eq_one_not_true", {"field": "priority", "op": "eq", "value": 1}, ["j"]),
    ("missing_region", {"field": "region", "op": "exists", "value": False}, ["e", "i"]),
    ("ne_excludes_missing", {"field": "region", "op": "ne", "value": "east"}, ["b", "c", "f", "h", "j"]),
]

EQUIVALENT_FORMS = [
    ("or_swapped", P_OR_SWAPPED),
    ("and_swapped", P_AND_SWAPPED),
    ("nested_and", P_NESTED_AND),
    ("distributed", P_DISTRIBUTED),
    ("de_morgan", P_DE_MORGAN),
    ("double_not", P_DOUBLE_NOT),
]

COMPOSITE_CASES = [
    ("no_scope_or", Q_NO_SCOPE, EXPECTED_NO_SCOPE),
    ("multi_and", Q_MULTI_AND, EXPECTED_MULTI_AND),
    ("not_composite", Q_NOT_COMPOSITE, EXPECTED_NOT_COMPOSITE),
    ("scan_only_branch", Q_SCAN_BRANCH, EXPECTED_SCAN_BRANCH),
    ("range_only_branch", Q_RANGE_BRANCH, EXPECTED_RANGE_BRANCH),
]


def ref_field(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def ref_equal(left, right):
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, dict) or isinstance(right, dict):
        return (isinstance(left, dict) and isinstance(right, dict)
                and left.keys() == right.keys()
                and all(ref_equal(left[key], right[key]) for key in left))
    if isinstance(left, list) or isinstance(right, list):
        return (isinstance(left, list) and isinstance(right, list)
                and len(left) == len(right)
                and all(ref_equal(a, b) for a, b in zip(left, right)))
    return left == right


def ref_match(document, predicate):
    """参照筛选:每个叶子是文档的全函数,缺失即假(不是三值逻辑)。"""
    if predicate is None:
        return True
    if "and" in predicate:
        return all(ref_match(document, child) for child in predicate["and"])
    if "or" in predicate:
        return any(ref_match(document, child) for child in predicate["or"])
    if "not" in predicate:
        return not ref_match(document, predicate["not"])
    value = ref_field(document, predicate["field"])
    op, other = predicate["op"], predicate["value"]
    if op == "exists":
        return (value is not MISSING) == other
    if value is MISSING:
        return False
    if op == "eq":
        return ref_equal(value, other)
    if op == "ne":
        return not ref_equal(value, other)
    if op == "gt":
        if isinstance(value, bool) or isinstance(other, bool):
            return False
        if isinstance(value, (int, float)) and isinstance(other, (int, float)):
            return value > other
        return isinstance(value, str) and isinstance(other, str) and value > other
    raise ValueError("oracle does not model op: " + op)


def expected_keys(predicate, docs=None):
    return sorted(key for key, document in (docs or DOCS).items() if ref_match(document, predicate))


def seed(db, collection="docs"):
    """写入十篇文档的单批提交;返回 (revision, 时间线)。时间线只数文档提交。"""
    db.catalog.create(collection)
    batch = db.batch("seed_docs")
    for key in sorted(DOCS):
        batch.put(collection, key, DOCS[key], expected=0)
    revision = batch.commit().revision
    return revision


class Timeline:
    """独立提交时间线:只有文档批次推进 head;目录与索引操作不得推进。

    用于交叉验证读修订与每篇文档的实际版本,不读取 strataleaf 内部状态。
    """

    def __init__(self):
        self.head = 0
        self.writes = {}

    def committed(self, **operations):
        self.head += 1
        for key, body in operations.items():
            self.writes.setdefault(key, []).append((self.head, body))
        return self.head

    def version_at(self, key, revision):
        version = 0
        for rev, _ in self.writes.get(key, []):
            if rev <= revision:
                version = rev
        return version

    def body_at(self, key, revision):
        body = None
        for rev, value in self.writes.get(key, []):
            if rev <= revision:
                body = value
        return body
