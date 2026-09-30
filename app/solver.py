"""频点分配求解器（回溯搜索 + 前向检查）。

求解目标（按优先级）：
1. 满足全部约束：时段相交的干扰组频点间距 >= 最小间距；锁定组使用指定频点；
   每个组全程只用一个频点，且频点属于场次可用集合；组时段与占用时段相交
   （端点相接不算）时不得选用被占用频点。
2. 最小化与前一发布版相比“共有组”的改频数（无前一版时该目标恒为 0）。
3. 仍有并列时，按组编号升序排列的频点序列取字典序最小方案。

另提供无解分析：给出一个不可约的锁定组子集、干扰关系子集或占用记录子集，
用于向调用方说明导致无解的原因。
"""

from __future__ import annotations


class SolverLimitExceeded(Exception):
    """搜索节点数超过资源上限。"""


def intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """左闭右开时段 [start, end) 是否相交。"""
    return a_start < b_end and b_start < a_end


def forbidden_frequencies(group_intervals, occupancies):
    """按“时段相交（左闭右开，端点相接不算）”计算每组被禁用的频点。

    :param group_intervals: {group_id: (start, end)} 设备组使用时段。
    :param occupancies:     占用记录 [{frequency, start, end, ...}]。
    :return: {group_id: {frequency, ...}}；组时段与某条占用时段相交时，
             该记录占用的频点对本组禁用。
    """
    forbidden = {g: set() for g in group_intervals}
    for rec in occupancies:
        for g, (gs, ge) in group_intervals.items():
            if intervals_overlap(gs, ge, rec["start"], rec["end"]):
                forbidden[g].add(rec["frequency"])
    return forbidden


class FrequencySolver:
    """对单个场次的一次规划求解。

    :param group_ids:   参与规划的组编号。
    :param frequencies: 可用整数频点集合。
    :param edges:       生效的干扰关系 [(group_a, group_b, min_separation)]，
                        调用方需已按“时段相交”过滤。
    :param previous:    前一发布版的分配 {group_id: frequency}，仅共有组参与
                        改频数统计；无前一版时传 None（改频数目标恒为 0）。
    :param forbidden:   各组被禁用的频点集合 {group_id: {frequency, ...}}，
                        通常由频点占用按“时段相交”计算得到。
    """

    def __init__(self, group_ids, frequencies, edges, previous=None,
                 forbidden=None, max_nodes: int = 2_000_000):
        self.groups = sorted(set(group_ids))
        self.frequencies = sorted(set(frequencies))
        self.previous = dict(previous or {})
        self.forbidden = {g: set(fs) for g, fs in (forbidden or {}).items()}
        self.adj = {g: [] for g in self.groups}
        for g1, g2, sep in edges:
            if g1 in self.adj and g2 in self.adj:
                self.adj[g1].append((g2, sep))
                self.adj[g2].append((g1, sep))
        self.max_nodes = max_nodes
        self.nodes = 0

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _tick(self):
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise SolverLimitExceeded(
                f"search exceeded {self.max_nodes} nodes")

    def _initial_domains(self, locks):
        domains = {g: set(self.frequencies) for g in self.groups}
        for g in self.groups:
            domains[g] -= self.forbidden.get(g, set())
        for g, f in locks.items():
            # 锁定不可用或被占用禁用的频点时域为空，搜索会立即判无解
            # （锁定不可用频点 API 层已先行拒绝；占用禁用按无解处理）
            domains[g] = {f} & domains[g]
        return domains

    def _change_cost(self, group, value):
        prev = self.previous.get(group)
        return 1 if (prev is not None and prev != value) else 0

    def _propagate(self, domains, assignment, group, value, trail):
        """赋值 group=value 后做前向检查；与既有赋值冲突或清空某域时失败。"""
        for other, sep in self.adj[group]:
            if other in assignment:
                if abs(assignment[other] - value) < sep:
                    return False
                continue
            removed = [f for f in domains[other] if abs(f - value) < sep]
            if removed:
                for f in removed:
                    domains[other].discard(f)
                trail.append((other, removed))
                if not domains[other]:
                    return False
        return True

    @staticmethod
    def _undo(domains, trail):
        for other, removed in trail:
            domains[other].update(removed)

    def _search(self, domains, assignment, budget, lex):
        """回溯搜索一个改频数 <= budget 的解。

        lex=True 时按组编号升序选变量、频点升序试值，找到的第一个完整解
        即为“按组编号升序的频点序列”字典序最小解；lex=False 时按 MRV
        启发式选变量，仅用于可行性判断。
        """
        self._tick()
        if len(assignment) == len(self.groups):
            return dict(assignment)
        if lex:
            group = next(g for g in self.groups if g not in assignment)
        else:
            group = min((g for g in self.groups if g not in assignment),
                        key=lambda g: len(domains[g]))
        if not domains[group]:
            return None
        for value in sorted(domains[group]):
            cost = self._change_cost(group, value)
            if cost > budget:
                continue
            trail = []
            assignment[group] = value
            if self._propagate(domains, assignment, group, value, trail):
                result = self._search(domains, assignment, budget - cost, lex)
                if result is not None:
                    return result
            self._undo(domains, trail)
            del assignment[group]
        return None

    @staticmethod
    def _clone(domains):
        return {g: set(d) for g, d in domains.items()}

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def solve(self, locks=None):
        """返回 (assignment, changes)；无解返回 None。

        assignment: {group_id: frequency}；changes: 共有组改频数。
        """
        locks = dict(locks or {})
        domains = self._initial_domains(locks)
        common = sum(1 for g in self.groups if g in self.previous)

        # 改频数最多为 common，故 budget=common 等价于不限制改频；
        # 先以此判断是否存在任何可行解（无解时只需这一次搜索）。
        if self._search(self._clone(domains), {}, common, lex=False) is None:
            return None

        # 线性上探最小可行改频数 k*
        best_k = 0
        while best_k < common and self._search(
                self._clone(domains), {}, best_k, lex=False) is None:
            best_k += 1

        # 在改频数 <= k* 的约束下取字典序最小解
        result = self._search(self._clone(domains), {}, best_k, lex=True)
        changes = sum(1 for g, f in result.items()
                      if g in self.previous and self.previous[g] != f)
        return result, changes


def _minimize_core(unsat, items):
    """删除式最小化：返回移除任一元素都会变为可解的不可约子集。"""
    core = list(items)
    for item in list(core):
        trial = [x for x in core if x != item]
        if unsat(trial):
            core = trial
    return core


def analyze_unsat(group_ids, frequencies, edges, locks, previous=None,
                  occupancies=None, group_intervals=None):
    """分析无解原因，返回结构化说明。

    - 若干扰关系本身已无解（无锁定、无占用仍无解）：返回不可约的干扰关系
      子集；
    - 否则若锁定与干扰关系已无解（去掉全部占用仍无解）：返回不可约的锁定
      子集，并附上涉及这些锁定组的干扰关系；
    - 否则占用记录参与导致无解：返回（在给定锁定与干扰关系下）不可约的
      占用记录子集，并附上同样不可缺的锁定子集与相关干扰关系。
    """
    locks = dict(locks or {})
    occupancies = list(occupancies or [])
    group_intervals = dict(group_intervals or {})

    # 每条占用记录按“时段相交”禁用相关组的频点（与 forbidden_frequencies
    # 同规则，但按记录分开，便于做记录粒度的不可约最小化）
    record_bans = []
    for rec in occupancies:
        bans: dict = {}
        for g, (gs, ge) in group_intervals.items():
            if intervals_overlap(gs, ge, rec["start"], rec["end"]):
                bans[g] = {rec["frequency"]}
        record_bans.append(bans)

    def merged_forbidden(bans_list):
        forbidden: dict = {}
        for bans in bans_list:
            for g, freqs in bans.items():
                forbidden.setdefault(g, set()).update(freqs)
        return forbidden

    def unsat_with(lock_list, edge_list, bans_list):
        solver = FrequencySolver(group_ids, frequencies, edge_list, previous,
                                 forbidden=merged_forbidden(bans_list))
        return solver.solve(dict(lock_list)) is None

    lock_items = sorted(locks.items())

    if unsat_with([], edges, []):
        # 无锁定、无占用仍无解：干扰关系本身是元凶
        core_edges = _minimize_core(lambda es: unsat_with([], es, []),
                                    list(edges))
        return {
            "cause": "interferences",
            "locks": [],
            "occupancies": [],
            "interferences": [
                {"group_a": a, "group_b": b, "min_separation": s}
                for a, b, s in core_edges],
        }

    if unsat_with(lock_items, edges, []):
        # 去掉全部占用仍无解：占用不是元凶，退回锁定分析
        core_locks = _minimize_core(
            lambda ls: unsat_with(ls, edges, []), lock_items)
        core_groups = {g for g, _ in core_locks}
        related_edges = [e for e in edges if e[0] in core_groups
                         or e[1] in core_groups]
        return {
            "cause": "locks",
            "locks": [{"group_id": g, "frequency": f}
                      for g, f in core_locks],
            "occupancies": [],
            "interferences": [
                {"group_a": a, "group_b": b, "min_separation": s}
                for a, b, s in related_edges],
        }

    # 占用记录参与导致无解：在给定锁定与干扰关系下最小化占用记录子集
    core_idx = _minimize_core(
        lambda idx: unsat_with(lock_items, edges,
                               [record_bans[i] for i in idx]),
        list(range(len(occupancies))))
    core_bans = [record_bans[i] for i in core_idx]
    # 在核心占用下同样不可缺的锁定（可能为空）
    core_locks = _minimize_core(
        lambda ls: unsat_with(ls, edges, core_bans), lock_items)
    affected = ({g for bans in core_bans for g in bans}
                | {g for g, _ in core_locks})
    related_edges = [e for e in edges if e[0] in affected
                     or e[1] in affected]
    return {
        "cause": "occupancies",
        "locks": [{"group_id": g, "frequency": f} for g, f in core_locks],
        "occupancies": [occupancies[i] for i in core_idx],
        "interferences": [
            {"group_a": a, "group_b": b, "min_separation": s}
            for a, b, s in related_edges],
    }
