#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rwsave_edit_core.py — .rwsave 存档编辑内核
============================================

采用 [`1-文档/保存功能评估.md`](../../1-文档/保存功能评估.md) 推荐的路线：

    原始 rwsave → 内存对象树(带字节留痕) → 修改 → 内存 splice → 重新 gzip → 新 rwsave

## 为什么不做「重新序列化」

评估文档否决了 `JSON → rwsave`，理由是浮点精度、bytes 摘要、NaN 编码三个坑。
本模块换了个角度绕开它们：**可编辑字段全部是定长标量**
（f32 / i32 / bool / i8 / i16 / i64）。定长意味着「改值不改长度」——
直接按记录的 offset 在 ROOT_BIN 上原地打补丁即可。

| 评估文档里的坑 | 本方案如何规避 |
| --- | --- |
| 浮点 `round(6)` 精度损失 | 不经浮点文本往返，直接写 4 字节 f32 位模式 |
| 迷雾图 / 内嵌 `.tmx` 是 bytes 摘要 | **一个字节都不碰**，迷雾与地图段原样 memcpy |
| NaN / Infinity 编码 | 按 IEEE-754 位模式原样写入，无需特殊编码 |
| 未改动部分是否字节精确 | 是。除补丁点外全部字节逐一保持不变 |

唯一硬约束：**不能改变任何变长字段**（字符串长度、数组个数、magic）。
内核在类型层面拒绝写入这类字段，而不是写坏了才报错。

## 字节留痕（Tracing）：不重写解析器

复用同目录的 `rwsave_inspector.py`（已在 6 个真实存档上字节级精确对齐），
**不重写任何读取逻辑**，只做两件事：

1. `TracingReader` 继承 `Reader`，覆写读取原语记录 `(offset, 字节数, 类型码)`；
2. 临时把模块里的 `OrderedDict` 换成 `SinkDict` —— 上游解析器写
   `u["current_health"] = r.float()` 时，`__setitem__` 就把「刚读到的那个 offset」
   绑到这个字段名上。

于是字段 offset 是**解析时顺手得到的**，不需要任何手写的字段顺序表
（那是最容易与上游失配的地方）。

### 对齐算法（为什么它不会错）

嵌套 dict 有两种构造形式，都会打乱「读一次、赋值一次」的朴素假设：

- `u["k"] = r.float()`：先读后写，1:1；
- `OrderedDict([("a", r.float()), ("b", r.float())])`：两个 float **先全部求值**，
  再依次 `__setitem__`。

两种情况下「读取顺序」与「赋值顺序」都一致，所以用**先进先出队列**即可对齐。
另外两条修正规则：

- `SinkDict.__init__` 记录构造时刻的队列水位 `mark`，
  之前的 pending 属于外层字段（如 `nleg = r.byte()`），不由本 dict 消费；
- 赋值为**容器**（list/dict）时清空 pending —— 该容器的读取已经结束，
  残留的 pending 属于外层的临时变量（如 `n = r.int()` 计数），必须丢弃，
  否则会错位到下一个标量上。

`selfcheck.py` 用 6 个存档做「offset 回读校验」，任何错位都会立刻暴露。
"""

import os
import struct
import sys
import zlib
from collections import OrderedDict

# ---------------------------------------------------------------- 依赖装配

_HERE = os.path.dirname(os.path.abspath(__file__))

# 解析器与本模块同目录（rwsave_inspector.py），整个文件夹可直接拷走使用。
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import rwsave_inspector as R
except ImportError as e:                                    # pragma: no cover
    raise ImportError(
        "找不到 rwsave_inspector.py（期望与 rwsave_edit_core.py 同目录：%s）。\n"
        "请确认整个文件夹已完整拷贝。" % _HERE) from e


class EditError(Exception):
    """编辑/写回失败。消息面向最终用户，可直接展示。"""


# ---------------------------------------------------------------- 类型码

T_BOOL = "bool"          # 1 字节 0/1
T_U8 = "u8"              # 1 字节无符号（writeByte / writeTeamIdByte）
T_I8 = "i8"              # 1 字节有符号（writeByte(-1) 落盘 0xFF）
T_I16 = "i16"            # 2 字节（writeShort）
T_I32 = "i32"            # 4 字节（writeInt / writeEnumOrdinal）
T_I64 = "i64"            # 8 字节（writeLong / writeUnitIdOrNullBaseUnit）
T_F32 = "f32"            # 4 字节浮点（writeFloat）
T_F64 = "f64"            # 8 字节浮点（UnitStats.value / config）

# 只有定长类型可写。变长类型（utf/bytes/point/unit_type）不出现在此表中。
FIXED_TYPES = {
    T_BOOL: (1, "?", None),
    T_U8:   (1, "B", (0, 255)),
    T_I8:   (1, "b", (-128, 127)),
    T_I16:  (2, "h", (-32768, 32767)),
    T_I32:  (4, "i", (-2147483648, 2147483647)),
    T_I64:  (8, "q", (-9223372036854775808, 9223372036854775807)),
    T_F32:  (4, "f", None),
    T_F64:  (8, "d", None),
}

MAGIC_SHORT = 12345
SAVE_END = "<SAVE END>"
MAGIC_SAVE = "rustedWarfareSave"

# 自愈搜索窗口：上游存在「丢弃读取」，需在有限范围内前向重定位。
# 有界是为了防止跨对象误匹配。
RESYNC_WINDOW = 64


# ---------------------------------------------------------------- TracingReader

# 把基类实现**一次性**绑成普通函数，避免 `_base.xxx(self)` 又分派回覆写版
# （那会导致无限递归）。子类里一律调 `_base.xxx(self)`。
_base = R.Reader


class TracingReader(_base):
    """在既有 Reader 上叠加「读取留痕」，不改变任何读取语义。

    每次原语读取都往 `trace` 追加 `(offset, nbytes, typecode)`；
    `nbytes < 0` 表示变长（不可写）。块内 Reader 共享同一个 trace 列表，
    因此嵌套块的偏移也在同一坐标系里。
    """

    def __init__(self, buf, offset=0, name="ROOT", trace=None):
        super().__init__(buf, offset, name)
        self.trace = trace if trace is not None else []
        self.pending = []          # 定长读取的 FIFO 队列
        self.consumed = set()      # 稀疏标记已消费的 queue 下标
        self.consumed_upto = 0     # 水位线：< 此值的下标全部视为已消费
        self.suspicious = 0        # 绑定后回读校验不通过的次数（应为 0）
        self.resyncs = 0          # 因「丢弃读取」触发自愈重定位的次数
        self._sup = 0              # 重入抑制计数，见 _prim
        self.bindings = {}         # 字段名 → {offset,size,type}

    # -- 块：必须返回同类且共享状态，否则块内偏移会丢 --
    def start_block(self, expect=None):
        # 块头（块名 + 块长）本身不是字段，整体记为一个不可写 span，
        # 避免它的 i32 长度被后续字段误当作来源。
        start = self.pos
        self._sup += 1
        try:
            name = self.utf()
            if expect is not None and name != expect:
                raise R.SaveError("[%s] 块名不符：期望 %r，实得 %r @0x%X"
                                  % (self.name, expect, name, self.pos))
            n = self.int()
        finally:
            self._sup -= 1
        self.trace.append((start, -1, "block"))
        if n < 0:
            raise R.SaveError("[%s] 块长为负 %d @0x%X" % (self.name, n, self.pos - 4))
        if self.pos + n > len(self.buf):
            raise R.SaveError("[%s] 块体越界：块名 %r，长度 %d，起始 0x%X，总长 %d"
                              % (self.name, name, n, self.pos, len(self.buf)))
        inner = TracingReader(self.buf, self.pos, "%s/%s" % (self.name, name))
        inner.pending = self.pending
        inner.consumed = self.consumed
        inner.consumed_upto = self.consumed_upto
        inner.bindings = self.bindings
        inner._sup = self._sup
        self.pos += n
        return name, n, inner

    # ---------------------------------------------------------------
    # 重入抑制
    #
    # 基类里 boolean()/i8() 内部会调用 self.byte()，nullable_utf() 会调用
    # self.boolean()……若不抑制，一次「逻辑读取」会在 pending 里留下多条记录，
    # 导致后续字段全部错位（曾表现为 team 绑到 8 字节的 i64 上）。
    # 规则：**只有最外层的读取原语才记录一条**。
    # ---------------------------------------------------------------

    def _prim(self, nbytes, tcode, fn):
        self._sup += 1
        try:
            v = fn()
        finally:
            outer = (self._sup == 1)
            self._sup -= 1
        if outer:
            self._rec(self.pos - nbytes, nbytes, tcode)
        return v

    def _span(self, tcode, fn):
        """复合读取：整体记一条 span，不进 pending（不可写）。"""
        start = self.pos
        self._sup += 1
        try:
            v = fn()
        finally:
            self._sup -= 1
        self.trace.append((start, self.pos - start, tcode))
        return v

    def byte(self):
        return self._prim(1, T_U8, lambda: _base.byte(self))

    def i8(self):
        return self._prim(1, T_I8, lambda: _base.i8(self))

    def boolean(self):
        return self._prim(1, T_BOOL, lambda: _base.boolean(self))

    def short(self):
        return self._prim(2, T_I16, lambda: _base.short(self))

    def ushort(self):
        return self._prim(2, T_U8, lambda: _base.ushort(self))

    def int(self):
        return self._prim(4, T_I32, lambda: _base.int(self))

    def long(self):
        return self._prim(8, T_I64, lambda: _base.long(self))

    def float(self):
        return self._prim(4, T_F32, lambda: _base.float(self))

    def double(self):
        return self._prim(8, T_F64, lambda: _base.double(self))

    def utf(self):
        return self._span("utf", lambda: _base.utf(self))

    def nullable_utf(self):
        return self._span("utf?", lambda: _base.nullable_utf(self))

    def nullable_int(self):
        return self._span("int?", lambda: _base.nullable_int(self))

    def bytes_with_length(self):
        return self._span("bytes", lambda: _base.bytes_with_length(self))

    def point(self):
        return self._span("point", lambda: _base.point(self))

    def unit_type(self):
        return self._span("unit_type", lambda: _base.unit_type(self))

    def magic(self, label=""):
        # 对齐标记只用于校验，不是字段，不留痕
        self._sup += 1
        try:
            return _base.magic(self, label)
        finally:
            self._sup -= 1

    # -- 内部 --
    def _rec(self, off, n, t):
        self.trace.append((off, n, t))
        self.pending.append((off, n, t))

    def take_pending(self, mark, cursor=None, expect=None):
        """取出自 `cursor` 之后第一个合适的定长读取。

        正常情况就是**严格 FIFO**（下一个未消费的读取）——解析器顺序消费字节，
        读取顺序与赋值顺序天然一致。

        但上游解析器存在**丢弃读取**（discarded read）：例如
        `parse_projectile` 里 `r.boolean()   # 写端恒 false`，
        读出的值没有赋给任何字段。这类调用会在队列里留下「孤儿」条目，
        使其后所有字段错位（曾表现为 Projectile 的 12 个字段被回读校验拒绝）。

        因此当 FIFO 候选解不出期望值时，在**有界窗口**内前向搜索第一个
        能解出该值的记录，并消费中间的孤儿 —— 这就是自愈。
        窗口有界（RESYNC_WINDOW）是为了避免跨对象误匹配。

        返回 (index, record, next_cursor)。
        """
        i = mark if cursor is None else max(mark, cursor)
        while i < len(self.pending) and (i < self.consumed_upto or i in self.consumed):
            i += 1
        if i >= len(self.pending):
            return None, None, i

        rec = self.pending[i]
        if expect is None or rec[1] < 0 or _match(self.buf, rec[0], rec[2], expect):
            self.consumed.add(i)
            return i, rec, i + 1

        # FIFO 候选对不上 → 有界前向搜索（孤儿读取导致的错位）
        limit = min(len(self.pending), i + RESYNC_WINDOW)
        j = i + 1
        while j < limit:
            if j < self.consumed_upto or j in self.consumed:
                j += 1
                continue
            cand = self.pending[j]
            if cand[1] >= 0 and _match(self.buf, cand[0], cand[2], expect):
                for k in range(i, j):
                    self.consumed.add(k)              # 丢弃中间的孤儿条目
                self.consumed.add(j)
                self.resyncs += 1
                return j, cand, j + 1
            j += 1

        # 窗口内找不到：仍按 FIFO 走，交给调用方的回读校验去暴露问题
        self.consumed.add(i)
        self.suspicious += 1
        return i, rec, i + 1

    def clear_pending(self):
        """丢弃全部 pending（容器赋值后调用）。

        用水位线而非集合全量标记 —— pending 会随文件长度增长到数十万，
        逐个标记会让整体退化成 O(n²)。
        """
        n = len(self.pending)
        if n > self.consumed_upto:
            self.consumed_upto = n


def _match(buf, off, tcode, want):
    """校验 offset 处的字节能否解出期望值。浮点按位模式比，避免 NaN/±0 陷阱。

    注意：这只判断**值**是否吻合，不判断类型 —— 因为 Python 侧
    `byte/short/int/long` 都是 int，仅凭值无法反推类型。
    真正的类型正确性由「严格 FIFO 对齐」+ 调用方的重复 offset 检测保证。
    """
    if tcode not in FIXED_TYPES:
        return False
    fmt = FIXED_TYPES[tcode][1]
    try:
        got = struct.unpack_from(">" + fmt, buf, off)[0]
    except Exception:                                        # noqa: BLE001
        return False
    if tcode == T_BOOL:
        return isinstance(want, bool) and bool(got) == want
    if tcode in (T_F32, T_F64):
        if isinstance(want, bool) or not isinstance(want, (int, float)):
            return False
        try:
            return struct.pack(">" + fmt, got) == struct.pack(">" + fmt, float(want))
        except (TypeError, ValueError, OverflowError):
            return False
    if isinstance(want, bool):
        return False
    if not isinstance(want, int):
        return False
    return int(got) == want


# ---------------------------------------------------------------- SinkDict

class SinkDict(OrderedDict):
    """上游解析器的结果字典；在赋值瞬间把刚读到的字节 offset 绑到字段名上。

    ★ 每个 SinkDict 拥有**自己的** bindings 字典 ★
    这一点很关键：单位字段与嵌套字段会重名，例如
        u["rotation"]                   ← BaseUnit 的机身朝向
        u["movement_levels"][0]["rotation"]  ← 运动层的旋转
    若共用一张扁平表，后者会把前者覆盖掉（曾表现为 rotation 绑到错误 offset，
    而回读校验把它拒了）。按 dict 隔离后，只有**顶层单位字典**的绑定算可编辑字段。

    只对**标量**赋值绑定 offset；容器赋值清空 pending（见模块文档的说明）。
    """

    _reader = None            # 由工厂函数在构造时注入

    def __init__(self, *a, **kw):
        # 注意用 type(self)：_reader 挂在工厂生成的子类上，基类上是 None
        rd = type(self)._reader
        super().__init__()
        if a and isinstance(a[0], (list, tuple)) and a[0] and isinstance(a[0][0], tuple):
            # OrderedDict([("k", v), ...]) 形式：**实参先全部求值**，
            # 所以这些 value 的读取都发生在 __init__ 之前。
            # 游标要回退 len(pairs) 个槽位，否则会跳过它们导致整体错位。
            n = len(a[0])
            cur = max(0, len(rd.pending) - n) if rd else 0
        else:
            cur = len(rd.pending) if rd else 0
        object.__setattr__(self, "_rd", rd)
        object.__setattr__(self, "_sealed", False)
        object.__setattr__(self, "_cursor", cur)
        object.__setattr__(self, "_binds", {})

        if a and isinstance(a[0], (list, tuple)):
            pairs = a[0]
            super().update(dict(pairs))
            for k, v in pairs:
                self._bind(k, v)
        elif a and isinstance(a[0], dict):
            super().update(a[0])

    @property
    def bindings(self):
        """本字典**自身**写入的字段绑定（不含嵌套字典的）。"""
        return object.__getattribute__(self, "_binds")

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._bind(key, value)

    def _bind(self, key, value):
        rd = object.__getattribute__(self, "_rd")
        if rd is None or object.__getattribute__(self, "_sealed"):
            return
        if isinstance(value, (list, dict, tuple, set, bytes, str)) or value is None:
            # 容器 / 字符串：其读取已结束，残留 pending 属于外层临时变量
            rd.clear_pending()
            cur = len(rd.pending)
        else:
            cur = object.__getattribute__(self, "_cursor")
            # expect 仅用于事后标记可疑绑定，不参与选择（严格 FIFO）
            _, rec, nxt = rd.take_pending(cur, expect=value, cursor=cur)
            cur = nxt
            if rec is not None:
                off, nbytes, tcode = rec
                if nbytes >= 0:
                    object.__getattribute__(self, "_binds")[key] = {
                        "offset": off, "size": nbytes, "type": tcode}
        object.__setattr__(self, "_cursor", cur)


def _sink_factory(reader):
    """返回绑定到 `reader` 的 OrderedDict 替代品。"""
    class _Bound(SinkDict):
        pass
    _Bound._reader = reader
    return _Bound


# ---------------------------------------------------------------- 字段元数据

FIELD_META = {
    # ---- 生命 / 护盾 / 能量 ----
    "current_health":        ("当前血量", "生命", "f", False),
    "max_health":            ("最大血量", "生命", "f", False),
    "unit_armor":            ("护甲", "生命", "f", False),
    "shield":                ("护盾", "生命", "f", False),
    "unit_shield_max":       ("护盾上限", "生命", "f", False),
    "energy":                ("能量", "生命", "f", False),
    "unit_energy_max":       ("能量上限", "生命", "f", False),
    "current_energy":        ("当前能量", "生命", "f", False),
    "ammo":                  ("弹药", "生命", "i", False),

    # ---- 位置 ----
    "pos_x":                 ("位置 X", "位置", "f", False),
    "pos_y":                 ("位置 Y", "位置", "f", False),
    "pos_z":                 ("高度 Z", "位置", "f", False),
    "world_x":               ("世界坐标 X（恒 0）", "位置", "f", False),
    "world_y":               ("世界坐标 Y（恒 0）", "位置", "f", False),
    "velocity_x":            ("速度 X", "位置", "f", False),
    "velocity_y":            ("速度 Y", "位置", "f", False),
    "radius":                ("碰撞半径", "位置", "f", False),
    "display_radius":        ("显示半径", "位置", "f", False),

    # ---- 朝向 ----
    "rotation":              ("机身朝向", "朝向", "f", False),
    "direction":             ("朝向角", "朝向", "f", False),
    "target_rotation":       ("目标朝向", "朝向", "f", False),
    "turret_rotation":       ("炮塔转角", "朝向", "f", False),
    "turret_turn_speed":     ("炮塔转速", "朝向", "f", False),
    "navigation_angle":      ("导航角", "朝向", "f", False),
    "movement_level0_rotation": ("运动层 0 旋转", "朝向", "f", False),

    # ---- 状态开关 ----
    "is_active":             ("激活", "状态", "b", False),
    "is_alive":              ("存活", "状态", "b", True),
    "is_dead":               ("已死亡", "状态", "b", True),
    "is_unit_paralyzed":     ("被瘫痪", "状态", "b", False),
    "is_unit_disabled":      ("被禁用", "状态", "b", False),
    "is_moving":             ("移动中", "状态", "b", False),
    "is_rotating":           ("转向中", "状态", "b", False),
    "is_unit_invulnerable":  ("无敌", "状态", "b", False),
    "is_unit_untargetable":  ("不可选中", "状态", "b", False),
    "is_unit_capturable":    ("可占领", "状态", "b", False),
    "is_ai_unit":            ("AI 单位", "状态", "b", False),
    "is_builder":            ("是建造者", "状态", "b", False),
    "is_targetable":         ("可被攻击", "状态", "b", False),
    "is_destroyed":          ("已销毁", "状态", "b", True),
    "is_static":             ("静态", "状态", "b", False),
    "is_pathing_active":     ("寻路中", "状态", "b", False),
    "is_path_incomplete":    ("路径不完整", "状态", "b", False),
    "is_transport_attached": ("已被运输", "状态", "b", False),
    "in_formation":          ("在编队中", "状态", "b", False),
    "should_maintain_formation": ("维持编队", "状态", "b", False),
    "is_secondary_recharging": ("副武器装填中", "状态", "b", False),
    "is_target_search_pending": ("待搜索目标", "状态", "b", False),

    # ---- 队伍 ----
    "team":                  ("所属队伍", "队伍", "i", True),

    # ---- 战斗 ----
    "kill_count":            ("击杀数", "战斗", "i", False),
    "total_damage_dealt":    ("累计造成伤害", "战斗", "f", False),
    "attack_mode":           ("攻击模式", "战斗", "i", True),
    "target_unit_id":        ("当前目标 ID", "战斗", "i", True),
    "attack_target_unit_id": ("攻击目标 ID", "战斗", "i", True),
    "unit_target1_id":       ("目标 1 ID", "战斗", "i", True),

    # ---- 建造 ----
    "build_progress":        ("建造进度", "建造", "f", False),
    "paid_build_progress":   ("已付建造进度", "建造", "f", False),
    "pending_credits":       ("待付费用", "建造", "f", False),

    # ---- 时间戳 ----
    "unit_creation_time":    ("创建时刻(ms)", "时间", "i", False),
    "time_alive_stamp":      ("存活时长戳", "时间", "i", False),
    "custom_timer_stamp":    ("自定义计时戳", "时间", "i", False),

    # ---- 路径 ----
    "waypoint_timer":        ("路径点计时", "路径", "f", False),
    "path_target_x":         ("路径目标 X", "路径", "f", False),
    "path_target_y":         ("路径目标 Y", "路径", "f", False),
    "path_target_radius":    ("路径目标半径", "路径", "i", False),
    "last_path_target_x":    ("上次路径 X", "路径", "f", False),
    "last_path_target_y":    ("上次路径 Y", "路径", "f", False),
    "path_retry_timer":      ("重试计时", "路径", "f", False),
    "previous_path_retry_timer": ("上次重试计时", "路径", "f", False),
    "repath_cooldown":       ("重规划冷却", "路径", "f", False),
    "movement_level0_target_x": ("运动层 0 目标 X", "路径", "f", False),
    "movement_level0_velocity_y": ("运动层 0 速度 Y", "路径", "f", False),
    "long_range_pathing":    ("远距寻路", "路径", "i", True),
    "body_movement_freeze_timer": ("移动冻结计时", "路径", "f", False),
    "legacy_movement_timer": ("旧移动计时", "路径", "f", False),
    "spawn_exit_lock_timer": ("出仓锁定计时", "路径", "f", False),
    "blocked_recovery_time": ("阻塞恢复计时", "路径", "f", False),
    "move_throttle":         ("移动节流", "路径", "f", False),
    "transport_offset_x":    ("运输偏移 X", "路径", "f", False),
    "transport_offset_y":    ("运输偏移 Y", "路径", "f", False),
    "transport_recovery_time": ("运输恢复计时", "路径", "f", False),
    "transport_recovery_delay": ("运输恢复延迟", "路径", "f", False),
    "transport_attack_assist_timer": ("运输助攻计时", "路径", "f", False),
    "transported_by_id":     ("运输者 ID", "路径", "i", True),
    "transport_container_id": ("运输容器 ID", "路径", "i", True),
    "parent_entity_id":      ("父实体 ID", "路径", "i", True),

    # ---- 编队 ----
    "formation_slot_index":  ("编队槽位", "编队", "i", False),
    "formation_slot_angle":  ("编队槽位角", "编队", "f", False),
    "waypoint_sync_group_id": ("路径点同步组", "编队", "i", False),
    "last_transport_path_update_tick": ("运输路径更新 tick", "编队", "i", False),

    # ---- 杂项 / 语义未确认 ----
    "draw_layer":            ("绘制层", "杂项", "i", True),
    "field_bS":              ("字段 bS（语义未确认）", "杂项", "f", True),
    "field_bs":              ("字段 bs（哨兵值 -9999）", "杂项", "i", True),
    "frame_animation_timer": ("动画计时", "杂项", "f", False),
    "frame_animation_delay": ("动画延迟", "杂项", "f", False),
    "transport_unload_timer": ("卸载计时", "杂项", "f", False),
    "field_o":               ("字段 o（语义未确认）", "杂项", "f", True),
    "field_s":               ("字段 s（语义未确认）", "杂项", "f", True),
    "field_r":               ("字段 r（布尔）", "杂项", "b", True),
    "field_v":               ("字段 v（布尔）", "杂项", "b", True),
    "field_q":               ("字段 q（语义未确认）", "杂项", "f", True),
}

# 明确禁止写入：改了会改变字节长度 / 破坏对齐。
FORBIDDEN = {
    "waypoint_count":       "数组长度计数，改动会使对象字节长度变化",
    "waypoint_count_dup":   "与 waypoint_count 重复的计数，必须保持一致",
    "active_path_count":    "路径点数组长度计数",
    "active_path_count_2":  "与 active_path_count 重复的计数",
    "total_path_positions": "路径总长度计数",
    "tech_level":           "决定 movement_levels 数组长度",
    "transported_units":    "数组长度由前导计数决定",
    "base_ext_version":     "写入端硬编码为 26",
    "ext_version":          "写入端硬编码为 12",
    "custom_unit_version":  "写入端硬编码为 11",
    "has_unit_effects":     "条件块开关，改动会错位",
    "has_action_tag":       "条件块开关，改动会错位",
    "unit_flags":           "单位标志位，改动可能触发未实现的游戏逻辑",
    "unit_variables":       "变长容器",
    "unit_custom_effects":  "变长容器",
    "unit_custom_components": "变长容器",
    "unit_effects":         "变长容器",
    "waypoints":            "变长容器（路径点列表）",
    "movement_levels":      "变长容器（运动层）",
    "leg_instances":        "变长容器（腿部实例）",
    "factory_queue":        "变长容器（工厂队列）",
    "unit_stats":           "变长容器（属性覆盖表）",
    "price":                "变长容器（价格）",
    "additional_cost":      "变长容器（额外费用）",
    "team_name":            "变长字符串",
    "current_action_handler": "变长字符串",
}

# 编辑器分组顺序（网页端按此顺序渲染）
GROUP_ORDER = ["生命", "位置", "朝向", "状态", "队伍", "战斗", "建造",
               "时间", "路径", "编队", "杂项", "其它"]


# ---------------------------------------------------------------- 加载

def load_editable(path):
    """解析存档，返回带字段字节偏移的编辑视图。"""
    with open(path, "rb") as f:
        buf = f.read()
    return load_editable_from_bytes(buf, os.path.basename(path))


def load_editable_from_bytes(buf, name="(buffer)"):
    """主入口：跑一遍既有解析器 + 留痕，产出可编辑视图。"""
    meta = _read_header(buf)
    root = meta["root"]

    saved_reader, saved_od = R.Reader, R.OrderedDict
    r = TracingReader(root, 0, "ROOT")
    R.Reader = TracingReader
    R.OrderedDict = _sink_factory(r)
    try:
        cu = R.parse_custom_units(r)
        gs = R.parse_game_setup(r, meta["format_version"])
        ms = R.parse_map_section(r, meta["format_version"])
        gl = R.parse_global_state(r, meta["format_version"])
        tm = R.parse_team_section(r)
        n_ext = sum(1 for t in tm["teams"] if t.get("exists"))
        sh = R.parse_unit_shells(r)
        mid = R.parse_mid_scalars(r)
        stats = R.parse_stats(r)
        ext = [R.parse_extended_team_state(r) for _ in range(n_ext)]

        # 单位数据段：**逐对象**收集绑定。
        # 不复用 R.parse_unit_data 是因为需要拿到「每个对象各自的」bindings，
        # 但读取逻辑完全照搬它的实现（对象数据 + magic short），不重写语义。
        _parse_unit_data_traced(r, sh["shells"])
        ud = _LAST_UD

        remaining = len(root) - r.pos
        if remaining != 0:
            raise EditError("解析结束后仍剩 %d 字节未消费，存档结构与已知格式不符"
                            % remaining)
    finally:
        R.Reader, R.OrderedDict = saved_reader, saved_od

    units = [_build_unit_entry(o, s, root, b)
             for o, s, b in zip(ud["objects"], sh["shells"], ud["bindings"])]

    return {
        "name": name,
        "raw": buf,
        "root": root,
        # 打补丁的**唯一基准**。`root` 会被 apply_edits 就地改写，
        # 而网页端的 doc 是跨请求缓存的——若在改过的 root 上叠加补丁，
        # 上一轮残留的字节不在本轮 spans 内，自检就会误报「非补丁字节被改动」。
        "root_pristine": root,
        "meta": meta,
        "summary": {
            "raw_size": meta["raw_size"],
            "root_size": meta["root_size"],
            "version_code": meta["version_code"],
            "format_version": meta["format_version"],
            "is_demo": meta["is_demo"],
            "game_time_millis": gl.get("game_time_millis"),
            "map_path": ms.get("map_path"),
            "map_bytes": len(ms.get("map_data") or b""),
            "player_team_id": tm.get("player_team_id"),
            "max_team_id": tm.get("max_team_id"),
            "object_count": len(ud["objects"]),
            "custom_unit_count": cu.get("count"),
            "team_count": n_ext,
            "unit_group_markers": mid.get("unit_group_markers"),
        },
        "teams": _team_rows(tm, stats, ext),
        "stats": {"team_count": stats.get("team_count"),
                  "killed": sum(x.get("killed_units", 0) for x in stats.get("rows", [])),
                  "lost": sum(x.get("lost_units", 0) for x in stats.get("rows", []))},
        "units": units,
    }


def _read_header(buf):
    """等价于 R.open_save(path)，但直接吃 bytes。

    这里刻意用 `_base`（原Reader）而不是 TracingReader：
    外层文件头不含可编辑字段，且不应受留痕逻辑影响。
    """
    import gzip as _gzip
    rd = _base(buf, 0, "文件头")
    magic = rd.utf()
    if magic == "rustedWarfareReplay":
        raise EditError("这是录像回放文件（rustedWarfareReplay），不是存档")
    if magic != MAGIC_SAVE:
        raise EditError("文件头标识不符：%r" % magic)

    version_code = rd.int()
    fv = rd.int()
    if fv > R.FORMAT_VERSION_MAX:
        raise EditError("存档格式版本 %d 高于本工具支持的 %d（游戏自身也会拒绝加载）"
                        % (fv, R.FORMAT_VERSION_MAX))
    is_demo = rd.boolean() if fv >= 5 else False

    root = None
    comp_start = comp_end = None
    block_name = None
    if fv >= 23:
        block_name, comp_len, inner = rd.start_block("saveCompression")
        comp_start, comp_end = inner.pos, inner.pos + comp_len
        try:
            root = _gzip.decompress(buf[comp_start:comp_end])
        except Exception as e:                              # noqa: BLE001
            raise EditError("gzip 解压失败（偏移 0x%X，长度 %d）：%s"
                            % (comp_start, comp_len, e)) from None
    else:
        root = buf[rd.pos:]

    trailer = {}
    trailer["offset"] = comp_end if comp_end is not None else len(buf)
    tr = _base(buf, trailer["offset"], "文件尾")
    try:
        trailer["magic"] = tr.ushort()
        trailer["marker"] = tr.utf()
        trailer["exact"] = (tr.pos == len(buf))
    except Exception as e:                                  # noqa: BLE001
        trailer["error"] = str(e)
        trailer["exact"] = False

    return {
        "raw": buf, "raw_size": len(buf), "magic": magic,
        "version_code": version_code, "format_version": fv, "is_demo": is_demo,
        "compression_block": block_name,
        "compressed_range": (comp_start, comp_end),
        "root": root, "root_size": len(root), "trailer": trailer,
    }


# ---------------------------------------------------------------- 单对象条目

_LAST_UD = None


def _parse_unit_data_traced(r, shells):
    """单位数据段：逐对象解析，并保留每个对象自身的字段绑定。

    读取顺序严格照搬 `rwsave_inspector.parse_unit_data`：
        magic short → N × (对象数据 + magic short)
    每个对象解析后取回「该对象顶层字典」的 bindings（嵌套字典的绑定已被隔离）。
    """
    global _LAST_UD
    marker = r.magic("start of unit data")
    objs, binds = [], []
    for sh in shells:
        start = r.pos
        u = R.parse_one_unit(r, sh)
        u["index"] = sh["index"]
        u["object_id"] = sh["object_id"]
        u["type_name"] = sh.get("type_name")
        u["kind"] = sh["kind"]
        u["kind_name"] = sh.get("kind_name")
        u["offset"] = start
        u["magic_offset"] = r.magic("对象#%d 结束" % sh["index"])
        u["length"] = u["magic_offset"] - start
        objs.append(u)
        binds.append(getattr(u, "bindings", {}) or {})
    _LAST_UD = {"marker_offset": marker, "objects": objs,
                "bindings": binds, "end": r.pos}
    return _LAST_UD


def _build_unit_entry(obj, shell, root, bindings):
    """把一个对象转成编辑条目。offset 来自主解析时的留痕，无需二次解析。"""
    kind = shell.get("kind")
    start = obj.get("offset")
    magic_offset = obj.get("magic_offset")
    end = magic_offset if magic_offset is not None else start

    entry = {
        "index": shell.get("index"),
        "object_id": shell.get("object_id"),
        "kind": kind,
        "kind_name": shell.get("kind_name"),
        "type_name": shell.get("type_name") or shell.get("unit_name"),
        "offset": start,
        "end": end,
        "length": (end - start) if (start is not None and end is not None) else None,
        "fields": {},
    }

    # 快捷字段：直接取自已解析的对象树（用于列表展示）
    entry["team"] = obj.get("team")
    entry["current_health"] = obj.get("current_health")
    entry["max_health"] = obj.get("max_health")
    entry["is_dead"] = obj.get("is_dead")

    for name, info in (bindings or {}).items():
        if name in FORBIDDEN or info["type"] not in FIXED_TYPES:
            # 命中禁区：仍然记录下来，让网页端能**显示**它并说明为什么只读
            if name in FORBIDDEN and info["type"] in FIXED_TYPES \
                    and obj.get(name) is not None:
                entry.setdefault("locked_fields", []).append({
                    "name": name, "value": obj.get(name),
                    "type": info["type"],
                    "label": FIELD_META.get(name, (name,))[0],
                    "reason": FORBIDDEN[name],
                })
            continue
        val = obj.get(name)
        if val is None and name not in ("is_alive", "is_dead"):
            continue
        # offset 回读校验：确保绑定的字节确实能解出这个值（防错位）
        if not _verify(root, info, val):
            entry.setdefault("rejected", []).append(name)
            continue
        label, group, ui, danger = FIELD_META.get(
            name, (name, "其它", "b" if info["type"] == T_BOOL
                   else ("i" if info["type"].startswith("i") or info["type"] == T_U8
                         else "f"), True))
        entry["fields"][name] = {
            "name": name, "label": label, "group": group, "ui": ui,
            "danger": danger, "offset": info["offset"], "size": info["size"],
            "type": info["type"], "value": val,
        }

    # 重复 offset 检测：同一段字节被两个字段绑定 = 对齐错位的强征兆。
    # 这是本模块最重要的一道防错位断言（曾用它抓到 team 误绑到 i64 的 bug）。
    by_off = {}
    for name, f in entry["fields"].items():
        by_off.setdefault(f["offset"], []).append(name)
    dups = {off: ns for off, ns in by_off.items() if len(ns) > 1}
    if dups:
        entry["duplicate_offsets"] = {hex(k): v for k, v in dups.items()}
    return entry


def _verify(root, info, val):
    """按 offset 回读字节，确认能解出与解析结果相同的值。"""
    off, n, t = info["offset"], info["size"], info["type"]
    if off < 0 or off + n > len(root):
        return False
    try:
        fmt = FIXED_TYPES[t][1]
        got = struct.unpack_from(">" + fmt, root, off)[0]
    except Exception:                                        # noqa: BLE001
        return False
    if t == T_BOOL:
        return bool(got) == bool(val)
    if t in (T_F32, T_F64):
        # 用位模式比较，避免 -0.0/0.0 与 NaN 的比较陷阱
        try:
            return struct.pack(">" + fmt, got) == struct.pack(">" + fmt, float(val))
        except (TypeError, ValueError):
            return False
    return int(got) == int(val)


def _team_rows(tm, stats, ext):
    rows = []
    stats_rows = {x.get("index"): x for x in stats.get("rows", [])}
    ei = 0
    for i, slot in enumerate(tm.get("teams", [])):
        st = slot.get("state") or {}
        s = stats_rows.get(i) or {}
        e = None
        if slot.get("exists"):
            e = ext[ei] if ei < len(ext) else None
            ei += 1
        rows.append({
            "index": i,
            "exists": bool(slot.get("exists")),
            "team_id": st.get("team_id", slot.get("team_id")),
            "name": st.get("team_name"),
            "credits": st.get("credits"),
            "color_id": st.get("team_color_id"),
            "network_id": st.get("network_team_id"),
            "killed": s.get("killed_units"),
            "lost": s.get("lost_units"),
            "wiped_out": (e or {}).get("is_team_wiped_out"),
            "fog_w": ((e or {}).get("fog") or {}).get("width"),
            "fog_h": ((e or {}).get("fog") or {}).get("height"),
        })
    return rows


# ---------------------------------------------------------------- 写入

def encode_value(typecode, value, field_name=""):
    """把 Python 值编码成字节。定长类型，长度必然与原字段一致。"""
    if typecode not in FIXED_TYPES:
        raise EditError("字段 %r 的类型 %r 不可写（变长字段会改变字节布局）"
                        % (field_name, typecode))
    _, fmt, rng = FIXED_TYPES[typecode]

    if typecode == T_BOOL:
        if isinstance(value, str):
            v = value.strip().lower() in ("1", "true", "yes", "on", "是", "真", "开")
        else:
            v = bool(value)
        return struct.pack(">?", v)

    if typecode in (T_F32, T_F64):
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise EditError("字段 %r 需要数值，收到 %r" % (field_name, value)) from None
        try:
            return struct.pack(">" + fmt, f)
        except (OverflowError, ValueError):
            raise EditError("字段 %r 的数值 %r 超出可表示范围" % (field_name, value)) from None

    if isinstance(value, bool):
        iv = int(value)
    elif isinstance(value, str):
        s = value.strip()
        try:
            iv = int(s, 16) if s.lower().startswith(("0x", "-0x")) else int(s)
        except ValueError:
            raise EditError("字段 %r 需要整数，收到 %r" % (field_name, value)) from None
    else:
        try:
            iv = int(value)
        except (TypeError, ValueError):
            raise EditError("字段 %r 需要整数，收到 %r" % (field_name, value)) from None

    if rng and not (rng[0] <= iv <= rng[1]):
        raise EditError("字段 %r 的值 %d 超出 %s 的范围 [%d, %d]"
                        % (field_name, iv, typecode, rng[0], rng[1]))
    if typecode == T_U8 and iv < 0:
        iv &= 0xFF                     # writeByte(-1) 落盘 0xFF，读端按 i8 解
    try:
        return struct.pack(">" + fmt, iv)
    except struct.error as e:
        raise EditError("字段 %r 编码失败：%s" % (field_name, e)) from None


def decode_value(typecode, b):
    """把字节解回 Python 值（encode_value 的逆运算）。

    为什么需要它：网页输入框提交的是**字符串**（`'771'`），
    而回读校验拿到的是解析器解出的**浮点** `771.0`。
    直接拿 `'771' != 771.0` 比较必然不等，会把**完全正确的产物**
    误判为失败并删除（详见 `rwsave_web._verify_out`）。
    """
    if typecode not in FIXED_TYPES:
        raise EditError("类型 %r 不可解码" % (typecode,))
    _, fmt, _rng = FIXED_TYPES[typecode]
    return struct.unpack(">" + fmt, b)[0]


def apply_edits(doc, edits):
    """在 ROOT_BIN 上原地打补丁。

    edits: [{"unit": 对象下标, "field": 字段名, "value": 新值}, ...]
    全部校验通过才落盘，任何一条不合法则整体放弃（不产生半成品）。
    """
    # ⚠️ 必须从**原始** root 重建，不能在已改动的 doc["root"] 上叠加：
    # 网页端的 doc 跨请求缓存，保存失败后待提交编辑并未清空，
    # 若在旧补丁之上再打一次，残留字节不在本轮 spans 内 → 自检误报。
    base = doc.get("root_pristine")
    if base is None:                       # 兼容手工构造的 doc
        base = doc["root"]
    root = bytearray(base)
    units = doc["units"]

    # 先整体校验
    plan = []
    for e in edits:
        try:
            ui = int(e["unit"])
        except (KeyError, TypeError, ValueError):
            raise EditError("编辑项缺少合法的 unit 下标：%r" % (e,)) from None
        fname = e.get("field")
        if not fname:
            raise EditError("编辑项缺少 field：%r" % (e,))
        if not (0 <= ui < len(units)):
            raise EditError("对象下标 %d 越界（共 %d 个对象）" % (ui, len(units)))
        f = units[ui]["fields"].get(fname)
        if f is None:
            raise EditError("对象 #%d 没有可编辑字段 %r（%s）"
                            % (ui, fname, _reject_reason(units[ui], fname)))
        b = encode_value(f["type"], e.get("value"), fname)
        if len(b) != f["size"]:
            raise EditError("字段 %r 编码长度 %d 与原字段 %d 不一致，已放弃"
                            % (fname, len(b), f["size"]))
        plan.append((ui, fname, f, b, e.get("value")))

    # 同字段重复编辑 → 后者覆盖前者
    merged = {}
    for ui, fname, f, b, v in plan:
        merged[(ui, fname)] = (f, b, v)
    for (ui, fname), (f, b, _v) in merged.items():
        off = f["offset"]
        root[off:off + f["size"]] = b

    doc["root"] = bytes(root)
    # `new` 必须回解成**字段类型对应的真实值**（`'771'` → 771.0），否则回读校验
    # 拿字符串和浮点比，必然不等，把好产物当坏产物删掉。
    return [{"unit": ui, "field": fn,
             "offset": f["offset"], "size": f["size"],
             "type": f["type"], "old": f["value"],
             "new": decode_value(f["type"], b)}
            for (ui, fn), (f, b, _v) in merged.items()]


def _reject_reason(unit, fname):
    if fname in FORBIDDEN:
        return "禁止写入：%s" % FORBIDDEN[fname]
    if unit.get("offset_error"):
        return "该对象无法定位字段：%s" % unit["offset_error"]
    if fname in (unit.get("rejected") or []):
        return "offset 回读校验未通过（可能字段顺序与解析器失配）"
    return "该对象不含此字段（可能被写入端硬编码或分支跳过）"


def save(doc, out_path):
    """把打好补丁的 ROOT_BIN 重新 gzip 并拼装成完整 .rwsave。

    外层结构（GameSaver.java:153-272）：
        UTF "rustedWarfareSave" / i32 versionCode / i32 formatVersion / bool isDemo
        [block] UTF "saveCompression" + i32 长度 + gzip 体
        u16 magic(12345) / UTF "<SAVE END>"
    """
    meta = doc["meta"]
    fv = meta["format_version"]
    gz = gzip_compress(doc["root"])

    out = bytearray()
    out += _utf(MAGIC_SAVE)
    out += struct.pack(">i", meta["version_code"])
    out += struct.pack(">i", fv)
    if fv >= 5:
        out += struct.pack(">?", bool(meta["is_demo"]))
    if fv >= 23:
        out += _utf("saveCompression")
        out += struct.pack(">i", len(gz))
        out += gz
    else:
        out += doc["root"]
    out += struct.pack(">H", MAGIC_SHORT)
    out += _utf(SAVE_END)

    with open(out_path, "wb") as f:
        f.write(bytes(out))
    return {"path": out_path, "size": len(out),
            "root_size": len(doc["root"]), "gzip_size": len(gz)}


def gzip_compress(data):
    """生成与 Java GZIPOutputStream 兼容的 gzip 字节。

    Java 的 GZIPOutputStream 头固定 10 字节：
        1f 8b  CM=08  FLG=00  MTIME=00000000  XFL  OS
    读端 GZIPInputStream 对这些字段宽容；这里显式构造
    （mtime=0、OS=0），与原存档风格一致。
    """
    co = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = co.compress(data) + co.flush()
    header = bytes([0x1F, 0x8B, 0x08, 0x00, 0, 0, 0, 0, 0x00, 0x00])
    trailer = struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)
    return header + body + trailer


def _utf(s):
    """DataOutputStream.writeUTF：u16 长度 + UTF-8 字节。"""
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


# ---------------------------------------------------------------- 展示辅助

def unit_rows(doc):
    """给网页端列表用的扁平行（与 8-网页版功能/save_store.py 字段口径一致）。"""
    rows = []
    for i, u in enumerate(doc["units"]):
        f = u["fields"]

        def g(name, default=None):
            v = f.get(name)
            return v["value"] if v else default

        hp, mhp = g("current_health"), g("max_health")
        ratio = None
        if isinstance(hp, (int, float)) and mhp:
            try:
                ratio = round(float(hp) / float(mhp), 4)
            except (ZeroDivisionError, ValueError, OverflowError):
                ratio = None
        rows.append({
            "i": i,
            "object_id": u["object_id"],
            "type_name": u["type_name"],
            "kind": u["kind"],
            "kind_name": u["kind_name"],
            "team": g("team", u.get("team")),
            "hp": hp, "max_hp": mhp, "hp_ratio": ratio,
            "shield": g("shield"), "armor": g("unit_armor"),
            "pos_x": g("pos_x"), "pos_y": g("pos_y"),
            "rotation": g("rotation"), "kill_count": g("kill_count"),
            "is_dead": g("is_dead"), "is_active": g("is_active"),
            "editable": len(f),
            "offset_error": u.get("offset_error"),
        })
    return rows