#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rwsave_inspector.py — 《铁锈战争》(Rusted Warfare) 存档查看器
=========================================================

一个**单文件、零第三方依赖**的 `.rwsave` 存档解析工具。
把存档拖给 Python，它会把能读出来的所有信息全部打印出来，
并可导出为 JSON / CSV / 可读报告。

    pip 无需安装任何东西，Python 3.7+ 直接运行即可。

快速开始
--------
    python rwsave_inspector.py "我的存档.rwsave"
    python rwsave_inspector.py "我的存档.rwsave" --export out/
    python rwsave_inspector.py "存档目录/" --export out/     # 批量
    python rwsave_inspector.py "我的存档.rwsave" --units     # 只看单位
    python rwsave_inspector.py "我的存档.rwsave" --teams     # 只看队伍
    python rwsave_inspector.py "我的存档.rwsave" --map       # 导出地图 tmx
    python rwsave_inspector.py "我的存档.rwsave" --json      # 全量 JSON 到 stdout

它是**只读**的：只读取存档文件，绝不修改。

--------------------------------------------------------------------------
存档格式是怎么读出来的
--------------------------------------------------------------------------
`.rwsave` 的结构由游戏写端的 `GameSaver.writeSaveToStream()` 决定，
整体是「Java DataOutputStream 的大端序 + 分块(block) + 一层 gzip」：

    ┌ 文件头：UTF "rustedWarfareSave" / i32 版本码 / i32 格式版本 / bool isDemo
    ├ 块 "saveCompression"：i32 长度 + gzip 压缩体
    │   └ 下面所有内容都在这个 gzip 里，本工具解压后称为 ROOT_BIN
    │      ├ 块 customUnitsBlock → 块 customUnits → 自定义单位定义表
    │      ├ 块 gameSetup                    对局规则（钱、迷雾、AI 难度…）
    │      ├ 地图路径 / 远程地图标记 / 内嵌 .tmx 地图文件
    │      ├ 全局状态    游戏时长、镜头位置、缩放、雾开关、任务引擎
    │      ├ 队伍表      玩家昵称、颜色、金钱…
    │      ├ 单位外壳表  对象的「类型 + ID」清单
    │      ├ 中段        当前 ID、编队 ID、编队组、统计
    │      ├ 队伍扩展段  每队的战争迷雾图、自定义资源
    │      ├ 单位数据段  每个单位的完整状态（坐标/血量/朝向/目标…）
    │      └ 每个单位与每个分段之间都有 12345 的「对齐标记」
    └ 文件尾：magic short + UTF "<SAVE END>"

**怎么保证没读错**：本工具不是「尽力解析」，而是每一步都做硬校验 ——
每个块必须正好读满声明的长度，每处对齐标记必须等于 12345，
最后读完时必须**一个字节都不剩**。任何不符立即报错并指出出错偏移。
所以只要它没报错，就说明整档字节已被完整、无歧义地解释。

--------------------------------------------------------------------------
已知限制
--------------------------------------------------------------------------
- 仅支持 `formatVersion <= 96`（与游戏本身的上限一致）。更新的存档会被明确拒绝。
- `Projectile` / `EffectEmitter` 两类对象的布局按源码实现，但实测样例中未出现过，
  遇到时若报错属预期内。
- 地图以 Tiled `.tmx` 格式内嵌在存档里，可用 `--map` 导出后用 Tiled 打开。

--------------------------------------------------------------------------
许可与出处
--------------------------------------------------------------------------
格式逆向基于开源重构版 Rusted Warfare 的反混淆源码（RWX 项目）。
本工具仅作存档查看用途，不修改任何游戏文件。
"""

import argparse
import csv
import gzip
import json
import os
import struct
import sys
import unicodedata
from collections import Counter, OrderedDict

__version__ = "1.0"
__all__ = ["open_save", "parse_save", "SaveError"]

# ======================================================================
# 常量
# ======================================================================

MAGIC_SAVE = "rustedWarfareSave"
MAGIC_REPLAY = "rustedWarfareReplay"
SAVE_END = "<SAVE END>"
MAGIC_SHORT = 12345          # 对齐标记（大端 int16）
FORMAT_VERSION_MAX = 96      # 游戏自身支持的最高格式版本

# 单位外壳 kind（由写端 GameSaver 的 writeByte 决定）
SHELL_ENUM = 1               # 原版枚举单位：后跟 i32 枚举序号
SHELL_BASIC = 2              # 特殊对象：后跟 u8 子类型（抛射物/焦痕/特效）
SHELL_CUSTOM = 3             # 自定义单位（模组单位）：后跟 UTF 单位名

BASIC_SUBNAME = {1: "ScorchMark", 2: "Projectile", 3: "EffectEmitter"}

# UnitTypeEnum 的 ordinal → 名称（按 Java 枚举声明顺序）
UNIT_TYPE_ENUM_NAMES = {
    0: "extractor", 1: "landFactory", 2: "airFactory", 3: "seaFactory",
    4: "commandCenter", 5: "turret", 6: "antiAirTurret", 7: "builder",
    8: "tank", 9: "hoverTank", 10: "artillery", 11: "helicopter",
    12: "airShip", 13: "gunShip", 14: "missileShip", 15: "gunBoat",
    16: "megaTank", 17: "laserTank", 18: "hovercraft", 19: "ladybug",
    20: "battleShip", 21: "tankDestroyer", 22: "heavyTank",
    23: "heavyHoverTank", 24: "laserDefence", 25: "dropship", 26: "tree",
    27: "repairbay", 28: "NukeLaucher", 29: "AntiNukeLaucher",
    30: "mammothTank", 31: "experimentalTank", 32: "experimentalLandFactory",
    33: "crystalResource", 34: "wall_v", 35: "fabricator",
    36: "attackSubmarine", 37: "builderShip", 38: "amphibiousJet",
    39: "supplyDepot", 40: "experimentalHoverTank", 41: "turret_artillery",
    42: "turret_flamethrower", 43: "fogRevealer", 44: "spreadingFire",
    45: "antiAirTurretT2", 46: "turretT2", 47: "turretT3",
    48: "damagingBorder", 49: "zoneMarker", 50: "editorOrBuilder",
    51: "dummyNonUnitWithTeam",
}


class SaveError(Exception):
    """存档无法解析。消息里会带上出错偏移，便于排查。"""


# ======================================================================
# 读取器：镜像 Java 的 GameInputStream
# ======================================================================

class Reader:
    """按 Java `DataInputStream` 语义读取的游标。

    全部为大端序。方法名对应游戏读端的同名方法，便于对照源码。
    """

    def __init__(self, buf, offset=0, name="ROOT"):
        self.buf = buf
        self.pos = offset
        self.name = name

    def _need(self, n):
        if n < 0:
            raise SaveError("[%s] 负长度 %d @0x%X" % (self.name, n, self.pos))
        if self.pos + n > len(self.buf):
            raise SaveError("[%s] 数据不足：需要 %d 字节 @0x%X，但总长仅 %d"
                            % (self.name, n, self.pos, len(self.buf)))

    # -- 基本类型 -------------------------------------------------------
    def byte(self):
        self._need(1)
        v = self.buf[self.pos]; self.pos += 1
        return v

    def i8(self):
        """有符号字节。游戏用 0xFF 表示「-1」（空对象/空引用），必须按 i8 读。"""
        v = self.byte()
        return v - 256 if v > 127 else v

    def boolean(self):
        v = self.byte()
        if v not in (0, 1):
            raise SaveError("[%s] 布尔值非法 0x%02X @0x%X" % (self.name, v, self.pos - 1))
        return v == 1

    def short(self):
        self._need(2)
        v = struct.unpack_from(">h", self.buf, self.pos)[0]; self.pos += 2
        return v

    def ushort(self):
        self._need(2)
        v = struct.unpack_from(">H", self.buf, self.pos)[0]; self.pos += 2
        return v

    def int(self):
        self._need(4)
        v = struct.unpack_from(">i", self.buf, self.pos)[0]; self.pos += 4
        return v

    def long(self):
        self._need(8)
        v = struct.unpack_from(">q", self.buf, self.pos)[0]; self.pos += 8
        return v

    def float(self):
        self._need(4)
        v = struct.unpack_from(">f", self.buf, self.pos)[0]; self.pos += 4
        return v

    def double(self):
        self._need(8)
        v = struct.unpack_from(">d", self.buf, self.pos)[0]; self.pos += 8
        return v

    # -- 变长 -----------------------------------------------------------
    def utf(self):
        """Java `DataInputStream.readUTF`：2 字节长度 + UTF-8 字节。"""
        self._need(2)
        n = struct.unpack_from(">H", self.buf, self.pos)[0]; self.pos += 2
        self._need(n)
        raw = self.buf[self.pos:self.pos + n]; self.pos += n
        return raw.decode("utf-8", "replace")

    def nullable_utf(self):
        """先 1 字节存在标志，再是 UTF。"""
        return self.utf() if self.boolean() else None

    def nullable_int(self):
        return self.int() if self.boolean() else None

    def bytes_with_length(self):
        """i32 长度 + 原始字节（长度不含自身这 4 字节）。"""
        n = self.int()
        self._need(n)
        v = self.buf[self.pos:self.pos + n]; self.pos += n
        return v

    def point(self):
        """可空坐标：1 字节存在标志 + 2 个 f32。"""
        if not self.boolean():
            return None
        return (self.float(), self.float())

    # -- 分块与对齐标记 --------------------------------------------------
    def start_block(self, expect=None):
        """进入一个块：UTF 块名 + i32 块长，之后只能读块内那么多字节。"""
        name = self.utf()
        if expect is not None and name != expect:
            raise SaveError("[%s] 块名不符：期望 %r，实得 %r @0x%X"
                            % (self.name, expect, name, self.pos))
        n = self.int()
        if n < 0:
            raise SaveError("[%s] 块长为负 %d @0x%X" % (self.name, n, self.pos - 4))
        if self.pos + n > len(self.buf):
            raise SaveError("[%s] 块体越界：块名 %r，长度 %d，起始 0x%X，总长 %d"
                            % (self.name, name, n, self.pos, len(self.buf)))
        inner = Reader(self.buf, self.pos, "%s/%s" % (self.name, name))
        self.pos += n
        return name, n, inner

    def magic(self, label=""):
        """读对齐标记，必须等于 12345。"""
        off = self.pos
        v = self.ushort()
        if v != MAGIC_SHORT:
            raise SaveError("[%s] 对齐标记错误%s：期望 %d，实得 %d @0x%X"
                            % (self.name, ("（%s）" % label) if label else "",
                               MAGIC_SHORT, v, off))
        return off

    def unit_type(self):
        """读单位类型引用。

            i32 = -1  → 空
            i32 = -2  → 自定义单位，**后面还跟着一个 UTF 单位名**
            i32 >= 0  → 原版单位枚举的序号
        """
        tag = self.int()
        if tag == -1:
            return {"tag": -1, "name": None}
        if tag == -2:
            return {"tag": -2, "name": self.utf()}
        return {"tag": tag, "name": UNIT_TYPE_ENUM_NAMES.get(tag, "UnitTypeEnum#%d" % tag)}


# ======================================================================
# 原版单位枚举 → 具体类 → 自有前缀字段
# ----------------------------------------------------------------------
# 这张表从 RWX 的 Java 源码自动生成（见文档「表是怎么来的」）。
# 含义：kind=1 的外壳只给出枚举序号，真正的前缀字段由该序号对应的
# Java 类自己写。不同类前缀不同，例如：
#     tree(26)          → i32 treeType, i32 frameIndex, f32, bool, f32, u8, f32, i32
#     commandCenter(4)  → f32 resourceGenerationTimer, i32, 工厂队列, i32
#     fogRevealer(43)   → u8, i32 sightRange, f32 lifeTimer
#
# 标记含义：
#     ("writeXxx", "参数")  普通字段，按方法名读对应字节
#     ("__nested__", "目标") 调用了另一个对象的序列化方法（工厂队列）
#     ("__loop__", (次数, [调用])) 迭代重复，次数由紧邻前的 writeInt 给出
#
# 每项为 (前缀层列表, 通用继承链入口)。通用链入口是
# OrderableUnit / BaseUnit / PositionedObject / GameObject 四者中最先遇到的那个，
# 它之后的字段由本工具的 parse_base_unit() 等函数统一处理。
UNIT_TYPE_ENUM_INFO = {
    0: (('ResourceExtractor', [('ResourceExtractor', [('writeFloat', 'this.resourceGenerationTimer'), ('writeBoolean', 'this.extractorLevel > 1'), ('writeInt', 'this.extractorLevel')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    1: (('LandFactory', [('LandFactory', [('writeBoolean', 'this.isUpgraded'), ('writeByte', '0')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    2: (('AirFactory', [('AirFactory', [('writeInt', 'this.factoryLevel')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    3: (('SeaFactory', [('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    4: (('CommandCenter', [('CommandCenter', [('writeFloat', 'this.resourceGenerationTimer')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    5: (('TurretFactory', [('TurretFactory', [('writeBoolean', 'this.isUpgraded'), ('writeBoolean', 'this.turretType == 1'), ('writeStringUTF', 'this.turretImplementation.c()'), ('writeInt', 'this.turretType')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    6: (('AntiAirTurret', [('TurretFactory', [('writeBoolean', 'this.isUpgraded'), ('writeBoolean', 'this.turretType == 1'), ('writeStringUTF', 'this.turretImplementation.c()'), ('writeInt', 'this.turretType')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    7: (None, 'OrderableUnit'),
    8: (None, 'OrderableUnit'),
    9: (None, 'OrderableUnit'),
    10: (None, 'OrderableUnit'),
    11: (('Helicopter', [('Helicopter', [('writeFloat', 'this.bobPhase'), ('writeFloat', 'this.rotorSpeed')]), ('AirUnit', [('writeFloat', 'this.h'), ('writeBoolean', 'this.i')])]), 'OrderableUnit'),
    12: (('AirShip', [('AirUnit', [('writeFloat', 'this.h'), ('writeBoolean', 'this.i')])]), 'OrderableUnit'),
    13: (('Gunship', [('AirUnit', [('writeFloat', 'this.h'), ('writeBoolean', 'this.i')])]), 'OrderableUnit'),
    14: (('MissileShip', [('WaterUnit', [('writeFloat', 'this.sinkVelocity'), ('writeBoolean', 'this.hasSunk')])]), 'OrderableUnit'),
    15: (('GunBoat', [('WaterUnit', [('writeFloat', 'this.sinkVelocity'), ('writeBoolean', 'this.hasSunk')])]), 'OrderableUnit'),
    16: (None, 'OrderableUnit'),
    17: (None, 'OrderableUnit'),
    18: (('HovercraftUnit', [('HovercraftUnit', [('writeFloat', 'this.animationTimer'), ('writeFloat', 'this.unloadTimer'), ('writeBoolean', 'this.isUnloading'), ('writeInt', 'this.transportedUnits.size()'), ('__loop__', (None, [('writeUnitIdOrNullBaseUnit', '(BaseUnit) it.next()')]))])]), 'OrderableUnit'),
    19: (None, 'OrderableUnit'),
    20: (('BattleShip', [('WaterUnit', [('writeFloat', 'this.sinkVelocity'), ('writeBoolean', 'this.hasSunk')])]), 'OrderableUnit'),
    21: (None, 'OrderableUnit'),
    22: (None, 'OrderableUnit'),
    23: (None, 'OrderableUnit'),
    24: (('LaserDefense', [('LaserDefense', [('writeBoolean', 'this.isUpgraded'), ('writeFloat', 'this.currentEnergy'), ('writeBoolean', 'this.isRecharging'), ('writeFloat', 'this.laserEffectTimer')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    25: (('Dropship', [('Dropship', [('writeFloat', 'this.e'), ('writeFloat', 'this.f'), ('writeBoolean', 'this.g'), ('writeInt', 'this.transportedUnits.size()'), ('__loop__', (None, [('writeUnitIdOrNullBaseUnit', '(BaseUnit) it.next()')]))]), ('AirUnit', [('writeFloat', 'this.h'), ('writeBoolean', 'this.i')])]), 'OrderableUnit'),
    26: (('Tree', [('Tree', [('writeInt', 'this.treeType'), ('writeInt', 'this.frameIndex'), ('writeFloat', 'this.g'), ('writeBoolean', 'this.h'), ('writeFloat', 'this.i'), ('writeByte', '2'), ('writeFloat', 'this.scale'), ('writeInt', 'this.subType')])]), 'BaseUnit'),
    27: (('RepairBay', [('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    28: (('NukeLauncher', [('NukeLauncher', [('writeInt', 'this.nukeCount')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    29: (('AntiNukeLauncher', [('AntiNukeLauncher', [('writeBoolean', 'this.isActive'), ('writeInt', 'this.antiNukeCount')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    30: (None, 'OrderableUnit'),
    31: (None, 'OrderableUnit'),
    32: (('ExperimentalLandFactory', [('ExperimentalLandFactory', [('writeBoolean', 'this.isUpgraded'), ('writeByte', '0')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    33: (('CrystalResource', [('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    34: (('WallVertical', [('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    35: (('PowerFabricator', [('PowerFabricator', [('writeFloat', 'this.powerGenerationTimer'), ('writeBoolean', 'this.buildingAnimationState == 2')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    36: (('AttackSubmarine', [('AttackSubmarine', [('writeBoolean', 'this.a'), ('writeFloat', 'this.c')]), ('WaterUnit', [('writeFloat', 'this.sinkVelocity'), ('writeBoolean', 'this.hasSunk')])]), 'OrderableUnit'),
    37: (('BuilderShip', [('WaterUnit', [('writeFloat', 'this.sinkVelocity'), ('writeBoolean', 'this.hasSunk')])]), 'OrderableUnit'),
    38: (('AmphibiousJet', [('AmphibiousJet', [('writeBoolean', 'this.hasWaterEffect'), ('writeFloat', 'this.t'), ('writeFloat', 'this.u')]), ('AirUnit', [('writeFloat', 'this.h'), ('writeBoolean', 'this.i')])]), 'OrderableUnit'),
    39: (('SupplyDepot', [('SupplyDepot', [('writeInt', 'this.depotLevel')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    40: (('ExperimentalHoverTank', [('ExperimentalHoverTank', [('writeObjectId', 'this.chargedProjectile')])]), 'OrderableUnit'),
    41: (('TurretFactory', [('TurretFactory', [('writeBoolean', 'this.isUpgraded'), ('writeBoolean', 'this.turretType == 1'), ('writeStringUTF', 'this.turretImplementation.c()'), ('writeInt', 'this.turretType')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    42: (('TurretFactory', [('TurretFactory', [('writeBoolean', 'this.isUpgraded'), ('writeBoolean', 'this.turretType == 1'), ('writeStringUTF', 'this.turretImplementation.c()'), ('writeInt', 'this.turretType')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    43: (('FogRevealer', [('FogRevealer', [('writeByte', '0'), ('writeInt', 'this.sightRange'), ('writeFloat', 'this.lifeTimer')])]), 'OrderableUnit'),
    44: (('FireUnit', [('FireUnit', [('writeInt', 'this.fireType'), ('writeInt', 'this.frameIndex'), ('writeFloat', 'this.frameTimer'), ('writeByte', '0')])]), 'BaseUnit'),
    45: (('AntiAirTurret', [('TurretFactory', [('writeBoolean', 'this.isUpgraded'), ('writeBoolean', 'this.turretType == 1'), ('writeStringUTF', 'this.turretImplementation.c()'), ('writeInt', 'this.turretType')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    46: (('TurretFactory', [('TurretFactory', [('writeBoolean', 'this.isUpgraded'), ('writeBoolean', 'this.turretType == 1'), ('writeStringUTF', 'this.turretImplementation.c()'), ('writeInt', 'this.turretType')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    47: (('TurretFactory', [('TurretFactory', [('writeBoolean', 'this.isUpgraded'), ('writeBoolean', 'this.turretType == 1'), ('writeStringUTF', 'this.turretImplementation.c()'), ('writeInt', 'this.turretType')]), ('FactoryWithQueue', [('writeInt', 'this.buildingAnimationState'), ('__nested__', 'this.queueManager')]), ('BaseBuilding', [('writeInt', 'this.buildingAnimationState')])]), 'OrderableUnit'),
    48: (('DamageZone', [('DamageZone', [('writeByte', '0'), ('writeFloat', 'this.currentRadius'), ('writeFloat', 'this.contractionSpeed'), ('writeFloat', 'this.c'), ('writeFloat', 'this.targetRadius'), ('writeFloat', 'this.targetX'), ('writeFloat', 'this.targetY'), ('writeBoolean', 'this.isFirstUpdate'), ('writeFloat', 'this.damageMultiplier')])]), 'OrderableUnit'),
    49: (('DamageZone', [('DamageZone', [('writeByte', '0'), ('writeFloat', 'this.currentRadius'), ('writeFloat', 'this.contractionSpeed'), ('writeFloat', 'this.c'), ('writeFloat', 'this.targetRadius'), ('writeFloat', 'this.targetX'), ('writeFloat', 'this.targetY'), ('writeBoolean', 'this.isFirstUpdate'), ('writeFloat', 'this.damageMultiplier')])]), 'OrderableUnit'),
    50: (('EditorOrBuilder', [('EditorOrBuilder', [('writeByte', '1'), ('writeEnumOrdinal', 'this.G'), ('writeStringNullable', 'this.H')])]), 'OrderableUnit'),
    51: (None, 'OrderableUnit'),
}


def enum_layout(ordinal):
    """返回 (类名, 前缀层列表, 通用链入口)。未知序号返回 (None, None, None)。"""
    info = UNIT_TYPE_ENUM_INFO.get(ordinal)
    if not info:
        return UNIT_TYPE_ENUM_NAMES.get(ordinal), None, None
    prefix, chain_entry = info
    if not prefix:
        return UNIT_TYPE_ENUM_NAMES.get(ordinal), None, chain_entry
    return prefix[0], prefix[1], chain_entry


# ======================================================================
# 各分段解析
# ======================================================================

def parse_custom_units(r):
    """自定义单位定义表（模组单位的名字、哈希、标题）。

    注意内层块有个 ver 字段；ver>=2 时每条会多两个 bool。
    """
    outer_name, outer_len, outer = r.start_block("customUnitsBlock")
    _, inner_len, inner = outer.start_block("customUnits")

    ver = inner.int()
    count = inner.int()
    entries = []
    for _ in range(count):
        e = OrderedDict()
        e["name"] = inner.utf()
        e["config_hash"] = inner.int()
        e["flag"] = inner.byte()
        e["title"] = inner.nullable_utf()
        e["a"] = inner.long()
        e["b"] = inner.long()
        if ver >= 2:
            e["extra"] = [inner.boolean(), inner.boolean()]
        entries.append(e)

    # 硬校验：内层与外层都必须正好读满
    inner_begin = inner.pos - inner_len
    if inner.pos - inner_begin != inner_len:
        raise SaveError("customUnits 内层块未读满，剩 %d 字节" % (inner_len - (inner.pos - inner_begin)))
    outer_begin = outer.pos - outer_len
    if inner.pos - outer_begin != outer_len:
        raise SaveError("customUnitsBlock 外层块未读满，声明 %d 实读 %d"
                        % (outer_len, inner.pos - outer_begin))
    return {"outer_len": outer_len, "inner_len": inner_len, "ver": ver,
            "count": count, "entries": entries, "end": inner.pos}


def parse_game_setup(r, fv):
    """对局规则块。"""
    if fv < 56:
        return None
    _, blen, b = r.start_block("gameSetup")
    out = OrderedDict()
    out["network_game_active"] = b.boolean()
    out["singleplayer_server"] = b.boolean()
    if fv >= 94:
        out["has_rules"] = b.boolean()
    else:
        out["has_rules"] = out["network_game_active"] or out["singleplayer_server"]

    if out["has_rules"]:
        rules = OrderedDict()
        rules["flags"] = b.byte()
        rs_ver = b.byte()
        rules["room_version"] = rs_ver
        rules["fog_mode"] = b.int()
        rules["starting_credits"] = b.int()
        rules["revealed_map"] = b.boolean()
        rules["ai_difficulty"] = b.int()
        rules["starting_units"] = b.int()
        rules["income_multiplier"] = b.float()
        rules["no_nukes"] = b.boolean()
        rules["flag_j"] = b.boolean()
        rules["shared_control"] = b.boolean()
        if rs_ver >= 1:
            rules["team_lock"] = b.boolean()
        if rs_ver >= 2:
            rules["fixed_ally_teams"] = b.boolean()
        if rs_ver >= 3:
            rules["allow_spectators"] = b.boolean()
            rules["room_lock"] = b.boolean()
        rules["random_seed"] = b.int()
        rules["current_unit_cap"] = b.int()
        rules["max_unit_cap"] = b.int()
        out["rules"] = rules
    else:
        out["rules"] = None

    begin = b.pos - blen
    if b.pos - begin != blen:
        raise SaveError("gameSetup 块未读满，声明 %d 实读 %d" % (blen, b.pos - begin))
    return out


def parse_map_section(r, fv):
    """地图：路径标记、远程地图标记、内嵌的 .tmx 文件。"""
    out = OrderedDict()
    out["map_path"] = r.utf()
    out["has_remote_map"] = r.boolean() if fv >= 72 else False
    out["map_data"] = r.bytes_with_length() if out["has_remote_map"] else None
    return out


def parse_global_state(r, fv):
    """全局对局状态：时长、镜头、雾开关、任务引擎。"""
    g = OrderedDict()
    g["game_time_millis"] = r.int()
    g["view_center_x"] = r.float()
    g["view_center_y"] = r.float()
    g["target_zoom"] = r.float()
    if fv >= 18:
        g["next_formation_id"] = r.int()
    r.int()                                    # 保留字段，读端丢弃
    if fv >= 19:
        r.magic("end of setup")
    # 鼠标框选区域：写端恒为「无」，所以后面没有数据
    has_cursor = r.boolean()
    if has_cursor:
        w, h = r.int(), r.int()
        for _ in range(w * h):
            r.byte()
        g["cursor_selection"] = (w, h)
    if fv >= 86:
        g["is_game_started"] = r.boolean()
        g["fog_enabled"] = r.boolean()
        g["fog_periodic"] = r.boolean()
        g["fog_render"] = r.boolean()
    g["has_mission_engine"] = r.boolean()
    g["mission"] = parse_mission(r) if g["has_mission_engine"] else None
    if fv >= 19:
        r.magic("start of teams")
    return g


def parse_mission(r):
    """任务引擎状态（战役/生存模式的关卡进度）。"""
    m = OrderedDict()
    m["show_intro"] = r.boolean()
    for k in ("r", "u", "v", "w", "x"):
        m[k] = r.int()
    for k in ("z", "A", "B"):
        m[k] = r.float()
    m["is_defeat_wave"] = r.boolean()
    ver = r.int()
    m["mission_state_version"] = ver

    n = r.int()
    triggers = []
    for _ in range(n):
        t = OrderedDict()
        t["unique_id"] = r.utf()
        t["is_active"] = r.boolean()
        t["activation_time"] = r.int()
        t["l"] = r.int()
        t["has_completed"] = r.boolean()
        t["n"] = r.int()
        if ver >= 3:
            t["b"] = r.int()
            if ver >= 5:
                t["c"] = r.int()
        triggers.append(t)
    m["triggers"] = triggers

    if ver >= 5:
        m["y"] = r.int()
    if ver >= 6:
        m["survival_waves_classic"] = r.boolean()
    # ver>=7 的区域控制 / 传送门分支在实测样例中未出现，遇到会明确报错而不是静默跳过
    if ver >= 7:
        raise SaveError("遇到 missionStateVersion=%d（含区域控制数据），"
                        "本工具尚未实现该分支" % ver)
    return m


def parse_basic_team_state(r, sv):
    """单个队伍的基本状态。"""
    t = OrderedDict()
    t["team_id"] = r.byte()
    t["credits"] = r.int()
    t["team_color_id"] = r.int()
    t["team_name"] = r.nullable_utf()
    t["is_team_observer"] = r.boolean()
    if sv > 26:
        t["network_team_id"] = r.int()
        t["team_last_ping_time"] = r.long()
    if sv >= 55:
        t["is_team_spectator"] = r.boolean()
        t["team_ping_time"] = r.int()
    if sv >= 91:
        t["team_sort_index"] = r.int()
        t["const_byte"] = r.byte()
    if sv >= 97:
        t["connection_active"] = r.boolean()
        t["network_active"] = r.boolean()
    if sv >= 125:
        t["is_team_victory"] = r.boolean()
        t["surrender_triggered"] = r.boolean()
        t["surrender_vote_time"] = r.int()
    if sv >= 149:
        t["team_ai_hint"] = r.nullable_utf()
        t["host_team_flag"] = r.int()
    if sv >= 156:
        t["ai_difficulty_override"] = r.nullable_int()
        t["starting_units_override"] = r.nullable_int()
        t["ai_level_override"] = r.nullable_int()
        t["player_color_override"] = r.nullable_int()
        t["assigned_team_color_index"] = r.int()
    return t


def parse_team_section(r, magic_already_read=True, sv=999999):
    """队伍表。**队伍数量来自存档本身，不是固定 10。**

    写端在进入本段之前已经写过「start of teams」对齐标记
    （`GameSaver.writeSaveToStream` 的 writeMagicShort()），它由
    `parse_global_state()` 读掉，所以默认 `magic_already_read=True`。
    """
    out = OrderedDict()
    out["magic_offset"] = None if magic_already_read else r.magic("start of teams")
    out["player_team_id"] = r.int()
    out["max_team_id"] = r.int()

    teams = []
    for i in range(out["max_team_id"]):
        slot = OrderedDict()
        slot["index"] = i
        slot["offset"] = r.pos
        slot["is_ai_controller"] = r.boolean()
        slot["is_ai_player"] = r.boolean()
        not_null = r.boolean()
        slot["exists"] = not_null
        if not_null:
            slot["state"] = parse_basic_team_state(r, sv)
            slot["state_end"] = r.pos
        teams.append(slot)
    out["teams"] = teams
    return out


def parse_unit_shells(r):
    """单位外壳表：每个对象的「类型 + 唯一 ID」。

    注意这只是一份清单；坐标血量等在后面的「单位数据段」里，按同序对应。
    """
    count = r.int()
    shells = []
    for i in range(count):
        sh = OrderedDict()
        sh["index"] = i
        sh["offset"] = r.pos
        kind = r.byte()
        sh["kind"] = kind
        if kind == SHELL_ENUM:                      # 原版单位：写枚举序号
            sh["kind_name"] = "原版单位"
            sh["ordinal"] = r.int()
            sh["type_name"] = UNIT_TYPE_ENUM_NAMES.get(
                sh["ordinal"], "UnitTypeEnum#%d" % sh["ordinal"])
        elif kind == SHELL_CUSTOM:                  # 模组单位：写单位名
            sh["kind_name"] = "自定义单位"
            sh["unit_name"] = r.utf()
            sh["type_name"] = sh["unit_name"]
        elif kind == SHELL_BASIC:                   # 非单位对象
            sub = r.byte()
            sh["kind_name"] = "特殊对象"
            sh["sub_type"] = sub
            sh["sub_name"] = BASIC_SUBNAME.get(sub, "basic#%d" % sub)
            sh["type_name"] = sh["sub_name"]
        else:
            raise SaveError("未知的外壳类型 %d @0x%X" % (kind, sh["offset"]))
        sh["object_id"] = r.long()
        sh["end"] = r.pos
        shells.append(sh)
    return {"count": count, "shells": shells}


def parse_mid_scalars(r):
    """中段：当前 ID、编队计数、编队组（Ctrl+数字）、建造队列 ID。"""
    m = OrderedDict()
    m["current_unit_id"] = r.long()
    r.int()                                       # FormationEngine 保留字段
    m["next_formation_id"] = r.int()

    ngroups = r.int()
    groups = []
    for i in range(ngroups):
        g = OrderedDict()
        g["index"] = i
        g["radius"] = r.float()
        g["last_click_time"] = r.long()
        cnt = r.int()
        g["unit_ids"] = [r.long() for _ in range(cnt)]
        g["const_byte"] = r.byte()
        groups.append(g)
    m["unit_groups"] = ngroups
    m["groups"] = groups
    m["renderer_const_byte"] = r.byte()
    m["game_ui_version"] = r.byte()
    m["build_queue_id"] = r.int()
    return m


def parse_stats(r):
    """统计块：每队的击杀/损失计数。"""
    _, blen, b = r.start_block("stats")
    version = b.byte()
    n = b.int()
    rows = []
    for i in range(n):
        d = OrderedDict()
        d["index"] = i
        d["version"] = b.byte()
        b.magic("stats start")
        for k in ("a", "b", "killed_units", "killed_buildings", "killed_experimental",
                  "lost_units", "lost_buildings", "lost_experimental", "i", "j"):
            d[k] = b.int()
        d["k"] = b.long()
        d["history"] = parse_team_history(b)
        rows.append(d)
    begin = b.pos - blen
    if b.pos - begin != blen:
        raise SaveError("stats 块未读满，声明 %d 实读 %d" % (blen, b.pos - begin))
    return {"len": blen, "version": version, "team_count": n, "rows": rows}


def parse_team_history(b):
    """队伍历史（记录单位死亡位置的分组网格）。"""
    h = OrderedDict()
    h["present"] = b.boolean()
    if not h["present"]:
        return h
    b.magic("team history")
    h["version"] = b.byte()
    h["a"] = b.int()
    h["has_list"] = b.boolean()
    if not h["has_list"]:
        return h
    n = b.byte()
    h["group_count"] = n
    groups = []
    for _ in range(n):
        size = b.short()
        px = py = 0
        pts = []
        for _ in range(size):
            dx = b.int(); dy = b.int()
            px += dx; py += dy          # 增量编码
            pts.append((px, py))
        groups.append({"size": size, "points": pts})
    h["groups"] = groups
    h["total_points"] = sum(g["size"] for g in groups)
    return h


def parse_stored_resources(r):
    """自定义资源字典。空时写的是 0xFF（必须按 i8 读成 -1）。"""
    kind = r.i8()
    if kind == -1:
        return {"kind": -1, "empty": True, "entries": []}
    n = r.short()
    entries = []
    for _ in range(n):
        entries.append((r.utf(), r.double()))
    return {"kind": kind, "empty": False, "entries": entries}


def parse_fog_state(r):
    """战争迷雾图：宽 × 高 个字节。"""
    present = r.boolean()
    if not present:
        return {"present": False, "width": 0, "height": 0, "data": b""}
    w = r.int(); h = r.int()
    n = w * h
    if n < 0 or n > (1 << 26):
        raise SaveError("迷雾图尺寸异常 %dx%d" % (w, h))
    data = r.buf[r.pos:r.pos + n]
    r.pos += n
    return {"present": True, "width": w, "height": h, "data": data}


def parse_extended_team_state(r, sv=999999):
    """队伍扩展状态：基本状态 + 迷雾图 + 自定义资源 + 动画组。"""
    t = OrderedDict()
    t["basic"] = parse_basic_team_state(r, sv)
    if sv >= 44:
        ext_ver = r.byte()
        t["ext_version"] = ext_ver
        t["is_team_wiped_out"] = r.boolean()
        if ext_ver >= 1:
            t["is_team_defeated_tech"] = r.boolean()
        has_fog = r.boolean()
        t["fog"] = parse_fog_state(r) if has_fog else {"present": False, "width": 0, "height": 0, "data": b""}
        if ext_ver >= 2:
            t["custom_resources"] = parse_stored_resources(r)
        t["team_animation_set"] = r.nullable_utf()
        if ext_ver >= 3:
            t["is_team_locked"] = r.boolean()
    return t


# ======================================================================
# 单位数据段
# ======================================================================
# 每个对象的数据按 Java 继承链顺序落盘：子类先写自己的字段，
# 末尾 super.a() 交给父类继续写。所以磁盘顺序 = 自底向上逆序。
#
#   自定义单位：CustomUnit → OrderableUnit → BaseUnit → PositionedObject → GameObject
#   原版单位  ：<该枚举的类> → OrderableUnit/BaseUnit → … → GameObject
#   焦痕等    ：ScorchMark → GameObject

def read_enum_prefix(r, layers, label):
    """按自动生成的前缀表读取原版单位的自有字段。"""
    pre = OrderedDict()
    for lname, calls in layers:
        bucket = OrderedDict()
        for idx, (meth, arg) in enumerate(calls):
            key = "%02d_%s" % (idx, meth)
            if meth == "__loop__":
                prev = bucket.get("%02d_writeInt" % (idx - 1))
                if not isinstance(prev, int):
                    raise SaveError("%s 的 %s 层：迭代循环前缺少计数 @0x%X"
                                    % (label, lname, r.pos))
                r._loop_count = prev
            bucket[key] = read_write_call(r, meth, arg, label)
        pre[lname] = bucket
    return pre


def read_write_call(r, method, arg, label):
    """按写端方法名读取对应字节。"""
    if method == "__nested__":
        return parse_factory_queue(r, label)
    if method == "__loop__":
        _cnt, inner = arg
        return [OrderedDict(("%02d_%s" % (i, m), read_write_call(r, m, a, label))
                            for i, (m, a) in enumerate(inner))
                for _ in range(r._loop_count)]
    if method == "writeFloat":
        return r.float()
    if method == "writeInt":
        return r.int()
    if method == "writeByte":
        return r.byte()
    if method == "writeBoolean":
        return r.boolean()
    if method == "writeShort":
        return r.short()
    if method == "writeLong":
        return r.long()
    if method == "writeStringUTF":
        return r.utf()
    if method == "writeStringNullable":
        return r.nullable_utf()
    if method == "writeIntNullable":
        return r.nullable_int()
    if method == "writeEnumOrdinal":
        return r.int()
    if method in ("writeUnitIdOrNullBaseUnit", "writeOrderableUnit",
                  "writeObjectId", "writeUnitIdIfAlive"):
        return r.long()
    if method == "writeTeamIdByte":
        return r.byte()
    if method == "writeDebugMessage":
        return None                            # 仅调试流写出，不落盘
    raise SaveError("%s：暂不支持的写端方法 %s @0x%X" % (label, method, r.pos))


def parse_unit_price_body(r):
    """造价（实例体，没有「是否存在」标志位，直接是标志字节）。"""
    flags = r.byte()
    up = OrderedDict()
    up["flags"] = flags
    up["cost"] = r.int()
    if flags & 1:
        up["f_c"] = r.float(); up["f_d"] = r.float(); up["f_e"] = r.float()
        up["i_f"] = r.int(); up["i_g"] = r.int(); up["i_h"] = r.int()
        up["i_i"] = r.int(); up["i_j"] = r.int()
    if flags & 2:
        up["resources"] = parse_stored_resources(r)
    return up


def parse_unit_price(r):
    """造价（带「是否存在」标志位的包装形式）。"""
    if not r.boolean():
        return {"present": False}
    up = parse_unit_price_body(r)
    up["present"] = True
    return up


def parse_factory_queue(r, label):
    """生产队列：正在生产什么、进度、集结点。"""
    q = OrderedDict()
    q["e"] = r.float()
    n = r.int()
    q["queue"] = [parse_buildings_projectile(r, label) for _ in range(n)]
    q["queue_size"] = n
    q["has_rally_point"] = r.boolean()
    if q["has_rally_point"]:
        q["rally_x"] = r.float()
        q["rally_y"] = r.float()
    return q


def parse_buildings_projectile(r, label):
    """生产队列里的一项（工厂正在造的东西）。"""
    p = OrderedDict()
    p["const_minus1"] = r.int()
    p["launch_delay"] = r.int()
    p["progress"] = r.float()
    p["const_minus1_b"] = r.int()
    p["price_hash"] = r.int()
    p["flag_f"] = r.boolean()
    p["action_id"] = r.utf()
    p["action_id_dup"] = r.utf()
    p["target_unit_id"] = r.long()
    p["target_point"] = r.point()
    p["is_high_priority"] = r.boolean()
    p["m"] = r.float()
    p["unit_type"] = r.unit_type()
    p["price_c"] = parse_unit_price_body(r)
    p["price_d"] = parse_unit_price(r)
    p["animation_tag"] = r.nullable_utf()
    return p


def parse_unit_stats(r):
    """单位被自定义属性覆盖的情况。"""
    if r.boolean():
        return {"is_default": True}
    n = r.short()
    fields = []
    for _ in range(n):
        fields.append({"index": r.short(), "value": r.double(), "config": r.double()})
    return {"is_default": False, "changed": n, "fields": fields}


def parse_custom_unit(r, u, label):
    """自定义单位（模组单位）的自有字段 + 后续继承链。"""
    u["custom_unit_version"] = r.int()
    u["frame_animation_timer"] = r.float()
    u["transport_unload_timer"] = r.float()
    u["is_unloading"] = r.boolean()
    n = r.int()
    u["transported_units"] = [r.long() for _ in range(n)]
    u["field_r"] = r.boolean()
    u["field_o"] = r.float()
    u["frame_animation_delay"] = r.float()
    u["field_s"] = r.float()
    u["field_v"] = r.boolean()
    nleg = r.byte()
    legs = []
    for _ in range(nleg):
        legs.append(OrderedDict([("b", r.float()), ("c", r.float()), ("d", r.float()),
                                 ("i", r.float()), ("k", r.boolean()), ("j", r.boolean()),
                                 ("m", r.boolean()), ("n", r.boolean())]))
    u["leg_instances"] = legs
    u["factory_queue"] = parse_factory_queue(r, label)
    u["factory_unit_config"] = r.unit_type()
    u["frame_animation_reverse"] = r.boolean()
    u["frame_animation_playing"] = r.boolean()
    has_tag = r.boolean()
    u["has_action_tag"] = has_tag
    if has_tag:
        u["current_action_handler"] = r.nullable_utf()
    u["unit_stats"] = parse_unit_stats(r)
    u["field_q"] = r.float()
    parse_orderable_unit(r, u, label)


def parse_unit_command(r):
    """单位的一条指令（移动/攻击/采集…）。"""
    c = OrderedDict()
    c["type"] = r.int()
    c["unit_type"] = r.unit_type()
    c["x"] = r.float()
    c["y"] = r.float()
    c["target_id"] = r.long()
    c["arg"] = r.byte()
    c["f1"] = r.float()
    c["f2"] = r.float()
    c["b1"] = r.boolean()
    c["b2"] = r.boolean()
    c["b3"] = r.boolean()
    c["action_id"] = r.nullable_utf()
    return c


def parse_air_effects(r, label):
    """空中单位的状态效果（速度加成、禁止某些指令等）。"""
    n = r.short()
    eff = OrderedDict([("count", n), ("effects", [])])
    if n == 0:
        return eff
    _, blen, b = r.start_block("s")
    for _ in range(n):
        t = b.int()
        e = OrderedDict([("type_ordinal", t)])
        if t == 0:                                # MovementSpeedEffect
            e["type_name"] = "移动速度加成"
            e["speed_multiplier"] = b.float()
        elif t == 1:                              # SpecialActionBlockEffect
            e["type_name"] = "禁止指令"
            e["blocked_action"] = b.nullable_utf()
            e["start_time"] = b.int()
        else:
            raise SaveError("%s：未知的空中效果类型 %d @0x%X" % (label, t, b.pos))
        e["effect_id"] = b.int()
        eff["effects"].append(e)
    begin = b.pos - blen
    if b.pos - begin != blen:
        raise SaveError("空中效果块未读满，声明 %d 实读 %d" % (blen, b.pos - begin))
    return eff


def parse_orderable_unit(r, u, label):
    """可命令单位的通用字段（移动、攻击、路径、编队…）。"""
    u["blocked_recovery_time"] = r.float()
    u["move_throttle"] = r.float()
    u["movement_level0_rotation"] = r.float()
    u["waypoint_count"] = r.int()
    cnt = r.int()
    u["waypoints"] = [parse_unit_command(r) for _ in range(cnt)]
    u["attack_mode"] = r.int()
    u["attack_target_id"] = r.long()
    u["turret_rotation"] = r.float()
    u["turret_turn_speed"] = r.float()
    u["waypoint_timer"] = r.float()
    u["is_pathing_active"] = r.boolean()
    u["path_target_x"] = r.float()
    u["path_target_y"] = r.float()
    u["repath_cooldown"] = r.float()
    u["transported_by_id"] = r.long()
    u["is_transport_attached"] = r.boolean()
    u["should_maintain_formation"] = r.boolean()
    u["in_formation"] = r.boolean()
    u["transport_offset_x"] = r.float()
    u["transport_offset_y"] = r.float()
    u["formation_slot_angle"] = r.float()
    u["last_transport_path_update_tick"] = r.int()
    u["waypoint_sync_group_id"] = r.int()
    apc = r.int()
    u["active_path_count"] = apc
    u["path_positions"] = [{"x": r.float(), "y": r.float()} for _ in range(apc)]
    u["active_path_count_2"] = r.int()
    u["total_path_positions"] = r.int()
    u["ext_version"] = r.byte()
    u["last_path_target_x"] = r.float()
    u["last_path_target_y"] = r.float()
    u["transport_recovery_time"] = r.float()
    u["transport_recovery_delay"] = r.float()
    u["is_path_incomplete"] = r.boolean()
    u["transport_attack_assist_timer"] = r.float()
    u["path_target_radius"] = r.int()
    u["path_retry_timer"] = r.float()
    u["navigation_angle"] = r.float()
    u["is_secondary_recharging"] = r.boolean()
    u["is_target_search_pending"] = r.boolean()
    u["formation_slot_index"] = r.short()
    u["pending_credits"] = r.float()
    u["long_range_pathing"] = r.int()
    u["previous_path_retry_timer"] = r.float()
    u["body_movement_freeze_timer"] = r.float()
    u["legacy_movement_timer"] = r.float()
    u["air_effects"] = parse_air_effects(r, label)
    parse_base_unit(r, u)


def parse_base_unit(r, u):
    """所有单位共有的字段：队伍、坐标、血量、状态标记…"""
    u["is_active"] = r.boolean()
    u["target_unit_id"] = r.long()
    u["attack_target_unit_id"] = r.long()
    u["field_bS"] = r.float()
    u["is_alive"] = r.boolean()
    u["is_dead"] = r.boolean()
    u["unit_creation_time"] = r.long()
    u["team"] = r.byte()
    u["world_x"] = r.float()
    u["world_y"] = r.float()
    u["velocity_x"] = r.float()
    u["velocity_y"] = r.float()
    u["rotation"] = r.float()
    u["rotation_speed"] = r.float()
    u["radius"] = r.float()
    u["display_radius"] = r.float()
    u["spawn_exit_lock_timer"] = r.float()
    u["build_progress"] = r.float()
    u["is_unit_paralyzed"] = r.boolean()
    u["is_unit_disabled"] = r.boolean()
    u["current_health"] = r.float()
    u["max_health"] = r.float()
    u["is_moving"] = r.boolean()
    u["movement_level0_target_x"] = r.float()
    u["movement_level0_velocity_y"] = r.float()
    u["transport_container_id"] = r.long()
    u["base_ext_version"] = r.byte()
    u["kill_count"] = r.int()
    u["total_damage_dealt"] = r.float()
    u["direction"] = r.float()
    u["target_rotation"] = r.float()
    tech = r.int()
    u["tech_level"] = tech
    mv = []
    for _ in range(tech):
        mv.append(OrderedDict([
            ("target_x", r.float()), ("velocity_x", r.float()), ("velocity_y", r.float()),
            ("rotation", r.float()), ("speed", r.float()),
            ("shadow_offset_x", r.float()), ("shadow_offset_y", r.float()),
            ("target_unit_id", r.long()), ("is_unit_transporting", r.boolean())]))
    u["movement_levels"] = mv
    u["field_bs"] = r.int()
    u["shield"] = r.float()
    u["unit_shield_max"] = r.float()
    u["energy"] = r.float()
    u["unit_energy_max"] = r.float()
    u["is_unit_invulnerable"] = r.boolean()
    u["is_unit_untargetable"] = r.boolean()
    u["is_unit_capturable"] = r.boolean()
    u["is_ai_unit"] = r.boolean()
    u["current_energy"] = r.float()
    u["is_rotating"] = r.boolean()
    has_eff = r.boolean()
    u["has_unit_effects"] = has_eff
    if has_eff:
        n = r.int()
        u["unit_effects"] = [{"is_active": r.boolean(), "unit_level": r.int()} for _ in range(n)]
    u["unit_armor"] = r.float()
    u["unit_target1_id"] = r.long()
    u["ammo"] = r.int()
    u["unit_flags"] = r.int()
    u["time_alive_stamp"] = r.int()
    u["custom_timer_stamp"] = r.int()
    u["last_converted_stamp"] = r.int()
    u["unit_counter"] = r.int()
    u["is_builder"] = r.boolean()
    u["is_targetable"] = r.boolean()
    u["unit_custom_effects"] = parse_stored_resources(r)
    u["unit_custom_components"] = parse_animation_tracking(r)
    u["parent_entity_id"] = r.long()
    u["attachment_slot"] = r.short()
    u["attachment_start_time_millis"] = r.int()
    u["unit_target2"] = parse_unit_or_placeholder(r)
    u["unit_target3"] = parse_unit_or_placeholder(r)
    u["unit_variables"] = parse_variable_scope(r)
    u["price"] = parse_unit_price(r)
    u["additional_cost"] = parse_unit_price(r)
    u["paid_build_progress"] = r.float()
    # PositionedObject
    u["pos_x"] = r.float()
    u["pos_y"] = r.float()
    u["pos_z"] = r.float()
    u["ex"] = r.int()
    # GameObject
    u["is_destroyed"] = r.boolean()
    u["is_static"] = r.boolean()
    u["draw_layer"] = r.int()


def parse_animation_tracking(r):
    """自定义动画组件（腿、炮管等的挂点）。"""
    kind = r.i8()
    if kind == -1:
        return {"kind": -1, "empty": True, "components": []}
    n = r.short()
    comps = []
    for _ in range(n):
        cid = r.nullable_utf()
        m = r.short()
        slots = [{"unit_id": r.long(), "b1": r.boolean(), "b2": r.boolean()} for _ in range(m)]
        comps.append({"id": cid, "slots": slots})
    return {"kind": kind, "empty": False, "components": comps}


def parse_unit_or_placeholder(r):
    """单位引用或占位符。"""
    kind = r.byte()
    if kind == 0:
        return {"kind": 0, "object_id": r.long()}
    return {"kind": kind}


def parse_variable_scope(r):
    """单位自定义变量。"""
    kind = r.i8()
    if kind == -2:
        return {"kind": -2, "null": True, "values": []}
    if kind == -1:
        return {"kind": -1, "empty": True, "values": []}
    n = r.short()
    vals = []
    for _ in range(n):
        name = r.utf()
        has = r.boolean()
        t = r.int()
        v = r.double() if has else None
        vals.append({"name": name, "has_value": has, "type": t, "value": v})
    return {"kind": kind, "empty": False, "size": n, "values": vals}


def parse_unit_type_id(r):
    """UnitType 引用（`GameOutputStream.writeUnitTypeId` / `GameInputStream.q()`）。

    i32 编码：
        -1        → null
        -2        → 自定义单位，后跟一个 UTF 名
        >=0       → UnitTypeEnum 的 ordinal
    """
    code = r.int()
    if code == -1:
        return {"code": -1, "null": True}
    if code == -2:
        return {"code": -2, "custom_name": r.utf()}
    return {"code": code, "ordinal": code,
            "name": UNIT_TYPE_ENUM_NAMES.get(code, "UnitTypeEnum#%d" % code)}


def parse_projectile_template(r):
    """抛射物模板引用（`ProjectileTemplate.a()`）。

    b==0 → 默认模板；b==1 → 自定义模板，后跟 `CustomProjectileTemplate.a()`
    （UnitType 引用 + UTF 名）。
    """
    kind = r.byte()
    if kind == 0:
        return {"kind": 0, "default": True}
    if kind == 1:
        return {"kind": 1, "custom_unit": parse_unit_type_id(r), "template_name": r.utf()}
    raise SaveError("未知的 ProjectileTemplate 类型 %d" % kind)


def parse_projectile(r):
    """抛射物（`game/Projectile.java` 的 `a(GameOutputStream)`，:415-519）。

    字段顺序严格照抄写端，末尾调 `super.a()` 写 GameObject 公共字段
    （PositionedObject 的 posX/posY/posZ/ex + GameObject 的三个标志）。
    """
    p = OrderedDict()

    # ---- 运动与归属 ----
    p["life_timer"] = r.float()
    p["source_unit_id"] = r.long()          # writeUnitIdOrNullBaseUnit：null → -1
    p["target_unit_id"] = r.long()
    p["speed"] = r.float()
    p["const_99"] = r.int()                 # 写端硬编码 99
    p["is_instant_hit"] = r.boolean()
    p["persists_after_explosion"] = r.boolean()
    p["has_exploded"] = r.boolean()
    p["field_T"] = r.boolean()
    p["damage"] = r.float()
    p["splash_damage"] = r.float()
    p["explosion_radius"] = r.float()
    p["color"] = r.int()
    p["is_ballistic"] = r.boolean()
    p["min_height"] = r.float()
    p["max_height"] = r.float()
    p["reached_apex"] = r.boolean()
    p["vertical_velocity"] = r.float()
    p["has_trail"] = r.boolean()
    p["trail_timer"] = r.float()
    p["is_small_explosion"] = r.boolean()
    p["plays_hit_sound"] = r.boolean()
    p["has_hit"] = r.boolean()
    p["remove_requested"] = r.boolean()
    p["render_jitter"] = r.boolean()
    p["texture_frame"] = r.short()
    p["target_speed"] = r.float()
    p["acceleration"] = r.float()
    p["is_area_damage"] = r.boolean()
    p["collides_with_terrain"] = r.boolean()
    p["angle"] = r.float()
    p["explosion_search_radius"] = r.float()
    p["field_aB"] = r.boolean()
    p["flies_to_position"] = r.boolean()
    r.boolean()                            # 写端恒 false
    r.float()                              # 写端恒 0.0
    r.float()                              # 写端恒 0.0
    p["is_beam"] = r.boolean()

    # ---- 寿命与追踪 ----
    p["max_life_time"] = r.float()
    p["age"] = r.float()
    p["track_offset_x"] = r.float()
    p["track_offset_y"] = r.float()
    p["has_fixed_target"] = r.boolean()
    p["target_x"] = r.float()
    p["target_y"] = r.float()
    p["field_C"] = r.boolean()
    p["is_nuke"] = r.boolean()
    p["parent_projectile_id"] = r.long()

    # ---- 命中与爆炸 ----
    p["hit_x"] = r.float()
    p["hit_y"] = r.float()
    p["hit_z"] = r.float()
    p["removal_complete"] = r.boolean()
    p["explosion_anim_timer"] = r.float()
    p["explosion_anim_duration"] = r.float()
    p["angle_initialized"] = r.boolean()
    p["texture_type"] = r.short()
    p["track_hit_units"] = r.boolean()

    # ---- 命中过的目标列表（writeGameObjectList：i32 长度 + N×i64）----
    n_hit = r.int()
    p["hit_unit_ids"] = [r.long() for _ in range(n_hit)]

    p["frame_index"] = r.short()
    p["render_scale"] = r.float()
    p["damage_enemies_only"] = r.boolean()
    p["excludes_air"] = r.boolean()
    p["spawn_emitter_on_hit"] = r.boolean()
    p["damage_multiplier"] = r.float()
    p["matches_target_altitude"] = r.boolean()
    p["auto_retarget"] = r.boolean()
    p["render_shadow"] = r.boolean()
    p["glow_scale"] = r.float()
    p["explosion_particle_timer"] = r.float()
    p["initial_delay"] = r.float()
    p["reveals_fog"] = r.boolean()
    p["explodes_on_timer_end"] = r.boolean()
    p["push_force"] = r.float()
    p["push_base"] = r.float()
    p["damage_to_buildings"] = r.float()
    p["damage_to_air"] = r.float()
    r.int()                                # 写端恒 0
    r.float()                              # 写端恒 0.0
    r.float()                              # 写端恒 0.0
    p["spawn_unit_type"] = parse_unit_type_id(r)     # 写端传 null
    r.int()                                # 写端恒 0
    r.boolean()                            # 写端恒 false

    # ---- 动画与伤害系数 ----
    p["animation_set"] = r.nullable_utf()            # AnimationTag.a()
    p["target_damage_multiplier"] = r.float()
    p["splash_damage_multiplier"] = r.float()
    p["no_friendly_fire"] = r.boolean()
    p["can_hit_submerged"] = r.boolean()
    p["armour_ignore"] = r.float()
    r.boolean()                            # 写端恒 false

    p["template"] = parse_projectile_template(r)

    # ---- 跟随对象（条件块：bool 存在位 + 存在时 3 个 float）----
    has_follow = r.boolean()
    p["follow_object_id"] = None
    p["follow_last"] = None
    if has_follow:
        p["follow_object_id"] = r.long()
        p["follow_last"] = (r.float(), r.float(), r.float())

    p["tech_level"] = r.short()
    p["depth"] = r.int()
    p["global_damage_multiplier"] = r.float()
    p["target_z"] = r.float()
    p["follow_node_index"] = r.int()

    # ---- super.a()：PositionedObject + GameObject ----
    p["pos_x"] = r.float()
    p["pos_y"] = r.float()
    p["pos_z"] = r.float()
    p["ex"] = r.int()
    p["is_destroyed"] = r.boolean()
    p["is_static"] = r.boolean()
    p["draw_layer"] = r.int()
    return p


def parse_effect_emitter(r):
    """特效发射器（`gameFramework/effects/EffectEmitter.java:78-83`）。

    写端只有 3 个 float + `super.a()`。注意 `EffectEmitter extends GameObject`
    （**不是** PositionedObject / BaseUnit），所以 super 只写
    `GameObject.a()` 的 3 个字段：isDestroyed / isStatic / drawLayer。
    位置由 EffectEmitter 自己那 2 个 float（posX / posY）承载，没有 posZ。
    """
    e = OrderedDict()
    e["pos_x"] = r.float()
    e["pos_y"] = r.float()
    e["duration"] = r.float()
    # super.a() → GameObject.java:68-72
    e["is_destroyed"] = r.boolean()
    e["is_static"] = r.boolean()
    e["draw_layer"] = r.int()
    return e


def parse_one_unit(r, shell):
    """按外壳类型分派，读取单个单位的完整数据。"""
    label = "对象#%d(%s)" % (shell["index"], shell.get("type_name", "?"))
    if shell["kind"] == SHELL_BASIC:
        sub = shell.get("sub_type")
        if sub == 1:
            return parse_scorch_mark(r)
        if sub == 2:
            return parse_projectile(r)
        if sub == 3:
            return parse_effect_emitter(r)
        raise SaveError("%s：未知的特殊对象子类型 %s" % (label, sub))

    u = OrderedDict()
    if shell["kind"] == SHELL_ENUM:
        ordinal = shell["ordinal"]
        cls, layers, chain_entry = enum_layout(ordinal)
        u["enum_class"] = cls
        u["prefix"] = read_enum_prefix(r, layers, label) if layers else None
        if chain_entry == "BaseUnit":
            u["chain_entry"] = "BaseUnit"
            parse_base_unit(r, u)
        else:
            u["chain_entry"] = chain_entry or "OrderableUnit"
            parse_orderable_unit(r, u, label)
    else:
        parse_custom_unit(r, u, label)
    return u


def parse_scorch_mark(r):
    """焦痕（炮击留下的地面痕迹），固定 38 字节。"""
    o = OrderedDict()
    o["pos_x"] = r.float()
    o["pos_y"] = r.float()
    o["type"] = r.int()
    o["variant"] = r.int()
    o["width"] = r.int()
    o["height"] = r.int()
    o["mark_type"] = r.int()
    o["frame"] = r.int()
    o["is_destroyed"] = r.boolean()
    o["is_static"] = r.boolean()
    o["draw_layer"] = r.int()
    return o


def parse_unit_data(r, shells):
    """单位数据段：逐对象读取，每个对象后必须跟一个对齐标记。"""
    marker = r.magic("start of unit data")
    objs = []
    for sh in shells:
        start = r.pos
        u = parse_one_unit(r, sh)
        u["index"] = sh["index"]
        u["object_id"] = sh["object_id"]
        u["type_name"] = sh.get("type_name")
        u["kind"] = sh["kind"]
        u["kind_name"] = sh.get("kind_name")
        u["offset"] = start
        u["magic_offset"] = r.magic("对象#%d 结束" % sh["index"])
        u["length"] = u["magic_offset"] - start
        objs.append(u)
    return {"marker_offset": marker, "objects": objs, "end": r.pos}


# ======================================================================
# 顶层入口
# ======================================================================

def open_save(path):
    """读取文件头并解压出 ROOT_BIN。"""
    if not os.path.isfile(path):
        raise SaveError("文件不存在：%s" % path)
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 16:
        raise SaveError("文件太小（%d 字节），可能不是有效存档" % len(raw))

    r = Reader(raw, 0, "文件头")
    magic = r.utf()
    if magic == MAGIC_REPLAY:
        raise SaveError("这是**录像回放**文件（rustedWarfareReplay），不是存档")
    if magic != MAGIC_SAVE:
        raise SaveError("文件头标识不符：%r（期望 %r）" % (magic, MAGIC_SAVE))

    version_code = r.int()                 # 游戏版本号，读端不使用
    fv = r.int()
    if fv > FORMAT_VERSION_MAX:
        raise SaveError("存档格式版本 %d 高于本工具支持的 %d（游戏自身也拒绝加载）"
                        % (fv, FORMAT_VERSION_MAX))
    is_demo = r.boolean() if fv >= 5 else False

    root = None
    comp_len = 0
    comp_start = comp_end = None
    block_name = None
    if fv >= 23:
        block_name, comp_len, comp = r.start_block("saveCompression")
        comp_start, comp_end = comp.pos, comp.pos + comp_len
        gz = raw[comp_start:comp_end]
        try:
            root = gzip.decompress(gz)
        except Exception as e:                          # noqa: BLE001
            raise SaveError("gzip 解压失败（偏移 0x%X，长度 %d）：%s"
                            % (comp_start, comp_len, e))
    else:
        # 极老版本没有压缩块，数据直接写在文件里
        root = raw[r.pos:]

    # 文件尾：magic short + "<SAVE END>"
    trailer = OrderedDict()
    trailer["offset"] = comp_end if comp_end is not None else len(raw)
    tr = Reader(raw, trailer["offset"], "文件尾")
    try:
        trailer["magic"] = tr.ushort()
        trailer["marker"] = tr.utf()
        trailer["exact"] = (tr.pos == len(raw))
    except SaveError as e:
        trailer["error"] = str(e)
        trailer["exact"] = False

    return {
        "path": path,
        "file": os.path.basename(path),
        "raw": raw,
        "raw_size": len(raw),
        "magic": magic,
        "version_code": version_code,
        "format_version": fv,
        "is_demo": is_demo,
        "compression_block": block_name,
        "compressed_size": comp_len,
        "compressed_range": (comp_start, comp_end),
        "root": root,
        "root_size": len(root),
        "trailer": trailer,
    }


def parse_save(path):
    """解析一个存档，返回结构化结果。

    成功即意味着 ROOT_BIN 已被完整消费（剩余 0 字节）。
    """
    s = open_save(path)
    fv = s["format_version"]
    root = s["root"]
    r = Reader(root, 0, "ROOT")
    bounds = []

    def seg(name, start, extra=None):
        e = OrderedDict([("name", name), ("start", start), ("end", r.pos),
                         ("length", r.pos - start)])
        if extra:
            e.update(extra)
        bounds.append(e)

    st = r.pos
    cu = parse_custom_units(r); seg("customUnits", st, {"count": cu["count"]})

    st = r.pos
    gs = parse_game_setup(r, fv); seg("gameSetup", st)

    st = r.pos
    ms = parse_map_section(r, fv); seg("地图", st, {"map_bytes": len(ms["map_data"] or b"")})

    st = r.pos
    gl = parse_global_state(r, fv); seg("全局状态", st)

    st = r.pos
    tm = parse_team_section(r); seg("队伍表", st, {"max_team_id": tm["max_team_id"]})
    n_ext = sum(1 for t in tm["teams"] if t["exists"])

    st = r.pos
    sh = parse_unit_shells(r); seg("单位外壳表", st, {"objects": sh["count"]})

    st = r.pos
    mid = parse_mid_scalars(r); seg("中段", st)

    st = r.pos
    stats = parse_stats(r); seg("统计", st, {"teams": stats["team_count"]})

    st = r.pos
    ext = []
    for _ in range(n_ext):
        e = parse_extended_team_state(r)
        e["offset"] = r.pos
        ext.append(e)
    seg("队伍扩展段", st, {"segments": len(ext)})

    st = r.pos
    ud = parse_unit_data(r, sh["shells"]); seg("单位数据段", st, {"objects": len(ud["objects"])})

    remaining = len(root) - r.pos
    if remaining != 0:
        raise SaveError("解析结束后仍剩 %d 字节未消费，说明中途有字段对不上（游标 0x%X）"
                        % (remaining, r.pos))

    return {
        "meta": OrderedDict([
            ("文件", s["file"]),
            ("原始字节数", s["raw_size"]),
            ("解压后字节数", s["root_size"]),
            ("游戏版本码", s["version_code"]),
            ("格式版本", fv),
            ("是否demo", s["is_demo"]),
            ("压缩块", s["compression_block"]),
            ("压缩字节数", s["compressed_size"]),
            ("文件尾偏移", s["trailer"]["offset"]),
            ("文件尾标记", s["trailer"].get("marker")),
            ("文件尾精确对齐", s["trailer"].get("exact")),
            ("未消费剩余字节", remaining),
        ]),
        "customUnits": cu,
        "gameSetup": gs,
        "map": {"path": ms["map_path"], "has_remote": ms["has_remote_map"],
                "bytes": len(ms["map_data"] or b"")},
        "map_data": ms["map_data"],
        "global": gl,
        "teams": tm,
        "shells": sh,
        "mid": mid,
        "stats": stats,
        "extended": ext,
        "unit_data": ud,
        "segments": bounds,
    }


# ======================================================================
# 输出：把结果转成 JSON 安全的结构
# ======================================================================

def jsonable(obj):
    """bytes → 摘要描述；NaN/Inf → 字符串；浮点保留 6 位。"""
    if isinstance(obj, bytes):
        return {"__bytes__": len(obj), "head_hex": obj[:16].hex(" ")}
    if isinstance(obj, dict):
        return OrderedDict((k, jsonable(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, float):
        if obj != obj:
            return "NaN"
        if obj == float("inf"):
            return "Infinity"
        if obj == float("-inf"):
            return "-Infinity"
        return round(obj, 6)
    return obj


def team_display_name(t):
    if not t["exists"]:
        return None
    nm = t["state"].get("team_name")
    return nm if nm else "(无名队伍 %d)" % t["index"]


def disp_width(s):
    """估算字符串在等宽终端里的显示宽度（CJK 字符占 2 列）。"""
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad(s, width):
    """按显示宽度左对齐补空格，中英文混排也能对齐。"""
    s = str(s)
    over = disp_width(s) - width
    if over > 0:                      # 过长则截断，保证列不被撑坏
        out = ""
        for ch in s:
            if disp_width(out) + disp_width(ch) > width - 1:
                break
            out += ch
        s = out + "…" if out != s else s
    return s + " " * max(1, width - disp_width(s))


def fmt_ms(ms):
    """把毫秒格式化成「X时Y分Z秒」。"""
    if not isinstance(ms, int) or ms < 0:
        return "-"
    total = ms // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d小时%02d分%02d秒" % (h, m, s)
    if m:
        return "%d分%02d秒" % (m, s)
    return "%d秒" % s


# ======================================================================
# 控制台报告
# ======================================================================

def print_report(res, show_units=False, show_teams=False, unit_limit=40):
    meta = res["meta"]
    W = 76
    print("=" * W)
    print("《铁锈战争》存档解析 — %s" % meta["文件"])
    print("=" * W)
    print("  文件大小      : %s 字节（解压后 %s 字节）"
          % (format(meta["原始字节数"], ","), format(meta["解压后字节数"], ",")))
    print("  版本          : 游戏版本码 %d，存档格式版本 %d"
          % (meta["游戏版本码"], meta["格式版本"]))
    print("  完整性        : %s"
          % ("✔ 全部字节已解析，无剩余" if meta["未消费剩余字节"] == 0
             else "✘ 剩余 %d 字节" % meta["未消费剩余字节"]))

    # ---- 对局信息 ----
    g = res["global"]
    print("\n【对局信息】")
    print("  游戏时长      : %s（%d 毫秒）" % (fmt_ms(g["game_time_millis"]), g["game_time_millis"]))
    print("  镜头位置      : (%.1f, %.1f)   缩放 %.3f"
          % (g["view_center_x"], g["view_center_y"], g["target_zoom"]))
    if "is_game_started" in g:
        print("  雾战          : 启用=%s 周期更新=%s 渲染=%s"
              % (g.get("fog_enabled"), g.get("fog_periodic"), g.get("fog_render")))
    m = g.get("mission")
    if m:
        print("  任务引擎      : 版本 %d，触发器 %d 个%s"
              % (m["mission_state_version"], len(m["triggers"]),
                 "（生存模式经典规则）" if m.get("survival_waves_classic") else ""))

    gs = res["gameSetup"]
    if gs and gs.get("rules"):
        ru = gs["rules"]
        print("\n【对局规则】")
        print("  起始资金 %d，收入倍率 %.2f，初始单位 %d，单位上限 %d"
              % (ru["starting_credits"], ru["income_multiplier"],
                 ru["starting_units"], ru["max_unit_cap"]))
        print("  AI 难度 %d，迷雾模式 %d，全图可见=%s，禁止核弹=%s"
              % (ru["ai_difficulty"], ru["fog_mode"], ru["revealed_map"], ru["no_nukes"]))
    elif gs:
        print("\n【对局规则】（本局未写入规则）")

    # ---- 地图 ----
    mp = res["map"]
    print("\n【地图】")
    print("  路径          : %s" % (mp["path"] if mp["path"] else "（空，地图内嵌在存档中）"))
    if mp["bytes"]:
        head = res["map_data"][:40].decode("utf-8", "replace").replace("\n", " ").strip()
        print("  内嵌地图      : %s 字节（Tiled .tmx），开头：%s…" % (mp["bytes"], head))
        print("                 用 --map 可导出为 .tmx 文件后用 Tiled 打开")

    # ---- 队伍 ----
    tm = res["teams"]
    stats = {row["index"]: row for row in res["stats"]["rows"]}
    print("\n【队伍】共 %d 队（本机玩家在第 %d 队）"
          % (tm["max_team_id"], tm["player_team_id"]))
    print("  %s %s %s %s %s %s %s" %
          (pad("队号", 5), pad("名称", 24), pad("颜色", 6),
           pad("金钱", 9), pad("击杀", 7), pad("损失", 7), pad("胜负", 5)))
    for t in tm["teams"]:
        i = t["index"]
        if not t["exists"]:
            print("  %s %s %s" % (pad(i, 5), pad("（空队伍）", 24), "-"))
            continue
        st = t["state"]
        sd = stats.get(i, {})
        name = st.get("team_name") or "(无名)"
        victory = "已胜利" if st.get("is_team_victory") else ""
        print("  %s %s %s %s %s %s %s" %
              (pad(i, 5), pad(name, 24), pad(st["team_color_id"], 6),
               pad(st["credits"], 9), pad(sd.get("killed_units", "-"), 7),
               pad(sd.get("lost_units", "-"), 7), pad(victory, 5)))

    # ---- 单位统计 ----
    objs = res["unit_data"]["objects"]
    kindc = Counter(o["kind_name"] for o in objs)
    print("\n【单位】共 %d 个对象" % len(objs))
    print("  构成          : %s" % "，".join("%s %d" % kv for kv in kindc.most_common()))
    alive = sum(1 for o in objs if o.get("current_health"))
    if alive:
        print("  有血量的      : %d 个" % alive)

    mid = res["mid"]
    print("  当前对象 ID   : %d（下一个新对象将使用此 ID）" % mid["current_unit_id"])
    print("  编队组        : %d 组（Ctrl+数字），下一个编队 ID %d"
          % (mid["unit_groups"], mid["next_formation_id"]))
    if mid["groups"]:
        for g in mid["groups"]:
            if g["unit_ids"]:
                print("      组%d 半径 %.0f，含 %d 个单位"
                      % (g["index"], g["radius"], len(g["unit_ids"])))

    # ---- 自定义单位 ----
    cu = res["customUnits"]
    titled = sum(1 for e in cu["entries"] if e["title"])
    print("\n【模组单位定义】共 %d 条（其中 %d 条带显示名）" % (cu["count"], titled))
    for e in cu["entries"][:10]:
        print("      %s %s" % (pad(e["name"], 28), e["title"] or ""))
    if cu["count"] > 10:
        print("      … 其余 %d 条见 --export 导出的 CSV" % (cu["count"] - 10))

    # ---- 单位列表 ----
    if show_units:
        print("\n【单位列表】%s" % ("（前 %d 个）" % unit_limit if len(objs) > unit_limit else ""))
        print("  %s %s %s %s %s %s %s %s"
              % (pad("序号", 5), pad("对象ID", 9), pad("类型", 22), pad("队伍", 5),
                 pad("X", 9), pad("Y", 9), pad("血量", 13), pad("朝向", 7)))
        for o in objs[:unit_limit]:
            hp = o.get("current_health")
            hps = ("%.0f/%.0f" % (hp, o["max_health"])) if hp is not None else "-"
            print("  %s %s %s %s %s %s %s %s"
                  % (pad(o["index"], 5), pad(o["object_id"], 9),
                     pad(o["type_name"] or "?", 22), pad(o.get("team", "-"), 5),
                     pad("%.1f" % o.get("pos_x", 0), 9),
                     pad("%.1f" % o.get("pos_y", 0), 9), pad(hps, 13),
                     pad("%.0f" % o.get("rotation", 0), 7)))
        if len(objs) > unit_limit:
            print("  … 共 %d 个，用 --unit-limit 调整或 --export 导出 CSV" % len(objs))

    # ---- 分段边界 ----
    print("\n【数据分段】")
    print("  %s %s %s %s" % (pad("段", 16), pad("起始", 12), pad("结束", 12), "长度"))
    for b in res["segments"]:
        print("  %s 0x%-10X 0x%-10X %d"
              % (pad(b["name"], 16), b["start"], b["end"], b["length"]))
    print("  %s %s 0x%-10X %d" % (pad("(根流末端)", 16), pad("-", 12),
                                  res["meta"]["解压后字节数"],
                                  res["meta"]["解压后字节数"]))
    print("=" * W)


def print_teams_verbose(res):
    """逐队展开详细信息，包括迷雾图尺寸。"""
    tm = res["teams"]
    stats = {row["index"]: row for row in res["stats"]["rows"]}
    ext_i = 0
    print("=" * 76)
    print("队伍详细信息 — %s" % res["meta"]["文件"])
    print("=" * 76)
    for t in tm["teams"]:
        i = t["index"]
        if not t["exists"]:
            print("\n【队 %d】空队伍（该编号未被占用）" % i)
            continue
        st = t["state"]
        print("\n【队 %d】%s" % (i, st.get("team_name") or "(无名)"))
        print("  颜色ID %d   金钱 %d   内部 ID %d   网络 ID %d"
              % (st["team_color_id"], st["credits"], st["team_id"],
                 st.get("network_team_id", -1)))
        if st.get("team_last_ping_time"):
            import datetime
            try:
                ts = datetime.datetime.fromtimestamp(st["team_last_ping_time"] / 1000.0)
                print("  最后心跳     : %s" % ts.strftime("%Y-%m-%d %H:%M:%S"))
            except Exception:                       # noqa: BLE001
                pass
        sd = stats.get(i, {})
        if sd:
            print("  战绩         : 击杀 单位%d/建筑%d/实验单位%d，损失 单位%d/建筑%d/实验单位%d"
                  % (sd["killed_units"], sd["killed_buildings"], sd["killed_experimental"],
                     sd["lost_units"], sd["lost_buildings"], sd["lost_experimental"]))
        flags = []
        for k, lbl in (("is_team_victory", "已胜利"), ("is_team_spectator", "旁观者"),
                       ("is_team_observer", "观察者"), ("is_ai_unit", "AI"),
                       ("surrender_triggered", "已投降")):
            if st.get(k):
                flags.append(lbl)
        if flags:
            print("  标记         : %s" % "，".join(flags))
        if ext_i < len(res["extended"]):
            ex = res["extended"][ext_i]
            fog = ex.get("fog") or {}
            if fog.get("present"):
                print("  战争迷雾     : %d × %d 格" % (fog["width"], fog["height"]))
            cr = ex.get("custom_resources") or {}
            if cr and not cr.get("empty") and cr.get("entries"):
                print("  自定义资源   : %s"
                      % ", ".join("%s=%.2f" % (k or "?", v) for k, v in cr["entries"][:6]))
            ext_i += 1


def print_units_verbose(res, limit=40):
    """按类型分组统计单位，并列出详细列表。"""
    objs = res["unit_data"]["objects"]
    print("=" * 76)
    print("单位详细信息 — %s（共 %d 个对象）" % (res["meta"]["文件"], len(objs)))
    print("=" * 76)

    by_type = Counter(o["type_name"] or "?" for o in objs)
    print("\n【按类型统计】")
    for nm, c in by_type.most_common():
        print("  %s %d" % (pad(nm, 28), c))

    by_team = Counter(o.get("team") for o in objs if o.get("team") is not None)
    if by_team:
        names = {t["index"]: team_display_name(t) for t in res["teams"]["teams"]}
        print("\n【按队伍统计】")
        for tid, c in by_team.most_common():
            label = names.get(tid)
            if tid == 255:
                label = "中立"
            elif tid == 254:
                label = "联盟"
            print("  队 %s %s %d 个" % (pad(tid, 4), pad(label or "?", 22), c))

    print("\n【列表】%s" % ("（前 %d 个）" % limit if len(objs) > limit else ""))
    print("  %s %s %s %s %s %s %s %s %s"
          % (pad("序号", 5), pad("对象ID", 9), pad("类型", 22), pad("队", 4),
             pad("X", 10), pad("Y", 10), pad("血量", 13), pad("朝向", 7), pad("建造", 6)))
    for o in objs[:limit]:
        hp = o.get("current_health")
        hps = ("%.0f/%.0f" % (hp, o["max_health"])) if hp is not None else "-"
        bp = o.get("build_progress")
        bps = ("%.0f%%" % (bp * 100)) if bp is not None else "-"
        print("  %s %s %s %s %s %s %s %s %s"
              % (pad(o["index"], 5), pad(o["object_id"], 9),
                 pad(o["type_name"] or "?", 22), pad(o.get("team", "-"), 4),
                 pad("%.1f" % o.get("pos_x", 0), 10),
                 pad("%.1f" % o.get("pos_y", 0), 10), pad(hps, 13),
                 pad("%.0f" % o.get("rotation", 0), 7), pad(bps, 6)))
    if len(objs) > limit:
        print("  … 共 %d 个，用 --unit-limit 调整或 --export 导出 CSV" % len(objs))


# ======================================================================
# 导出
# ======================================================================

def safe_stem(filename, fallback="save"):
    stem = os.path.splitext(os.path.basename(filename))[0]
    for ch in '<>:"/\\|?*':
        stem = stem.replace(ch, "_")
    stem = stem.strip().rstrip(".")
    return stem[:120] or fallback


def export_one(res, outdir, stem, want_json=True, want_map=True):
    """把一个存档的结果写到目录里，返回写出的文件列表。"""
    os.makedirs(outdir, exist_ok=True)
    written = []

    # -- 可读报告 --
    rp = os.path.join(outdir, "%s_报告.txt" % stem)
    with open(rp, "w", encoding="utf-8") as f:
        old = sys.stdout
        sys.stdout = f
        try:
            print_report(res)
            print()
            print_teams_verbose(res)
            print()
            print_units_verbose(res, limit=10 ** 9)
        finally:
            sys.stdout = old
    written.append(rp)

    # -- 单位 CSV --
    up = os.path.join(outdir, "%s_单位.csv" % stem)
    cols = ["序号", "对象ID", "类别", "类型", "队伍", "队伍名", "X", "Y", "Z",
            "当前血量", "最大血量", "朝向", "半径", "科技等级", "击杀数", "累计伤害",
            "建造进度", "存活", "死亡", "可被攻击", "弹药", "数据长度", "数据偏移"]
    names = {t["index"]: team_display_name(t) for t in res["teams"]["teams"]}
    with open(up, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for o in res["unit_data"]["objects"]:
            hp = o.get("current_health")
            tid = o.get("team")
            w.writerow({
                "序号": o["index"], "对象ID": o["object_id"],
                "类别": o["kind_name"], "类型": o["type_name"],
                "队伍": tid,
                "队伍名": names.get(tid) if tid is not None else "",
                "X": o.get("pos_x"), "Y": o.get("pos_y"), "Z": o.get("pos_z"),
                "当前血量": hp, "最大血量": o.get("max_health"),
                "朝向": o.get("rotation"), "半径": o.get("radius"),
                "科技等级": o.get("tech_level"), "击杀数": o.get("kill_count"),
                "累计伤害": o.get("total_damage_dealt"),
                "建造进度": o.get("build_progress"),
                "存活": o.get("is_alive"), "死亡": o.get("is_dead"),
                "可被攻击": o.get("is_targetable"), "弹药": o.get("ammo"),
                "数据长度": o["length"], "数据偏移": o["offset"],
            })
    written.append(up)

    # -- 队伍 CSV --
    tp = os.path.join(outdir, "%s_队伍.csv" % stem)
    tcols = ["队号", "名称", "颜色", "金钱", "内部ID", "网络ID", "最后心跳",
             "击杀单位", "击杀建筑", "损失单位", "损失建筑", "已胜利", "旁观",
             "迷雾宽", "迷雾高"]
    stats = {row["index"]: row for row in res["stats"]["rows"]}
    ext_i = 0
    with open(tp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=tcols)
        w.writeheader()
        for t in res["teams"]["teams"]:
            i = t["index"]
            if not t["exists"]:
                w.writerow({"队号": i, "名称": "（空）"})
                continue
            st = t["state"]
            sd = stats.get(i, {})
            fog = {}
            if ext_i < len(res["extended"]):
                fog = res["extended"][ext_i].get("fog") or {}
                ext_i += 1
            w.writerow({
                "队号": i, "名称": st.get("team_name") or "(无名)",
                "颜色": st["team_color_id"], "金钱": st["credits"],
                "内部ID": st["team_id"], "网络ID": st.get("network_team_id"),
                "最后心跳": st.get("team_last_ping_time"),
                "击杀单位": sd.get("killed_units"), "击杀建筑": sd.get("killed_buildings"),
                "损失单位": sd.get("lost_units"), "损失建筑": sd.get("lost_buildings"),
                "已胜利": st.get("is_team_victory"), "旁观": st.get("is_team_spectator"),
                "迷雾宽": fog.get("width"), "迷雾高": fog.get("height"),
            })
    written.append(tp)

    # -- 模组单位 CSV --
    cp = os.path.join(outdir, "%s_模组单位.csv" % stem)
    with open(cp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["序号", "名称", "哈希", "显示名"])
        w.writeheader()
        for i, e in enumerate(res["customUnits"]["entries"]):
            w.writerow({"序号": i, "名称": e["name"], "哈希": e["config_hash"],
                        "显示名": e["title"] or ""})
    written.append(cp)

    # -- 迷雾图 PNG 由调用方通过 export_fog_maps() 单独导出（可加 --no-fog 跳过）--

    # -- 地图 --
    if want_map and res.get("map_data"):
        mp = os.path.join(outdir, "%s_地图.tmx" % stem)
        with open(mp, "wb") as f:
            f.write(res["map_data"])
        written.append(mp)

    # -- 完整 JSON --
    if want_json:
        jp = os.path.join(outdir, "%s_完整数据.json" % stem)
        payload = OrderedDict((k, jsonable(v)) for k, v in res.items() if k != "map_data")
        payload["map_data_bytes"] = len(res["map_data"] or b"")
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        written.append(jp)

    return written


# ---- 迷雾图导出（纯标准库实现的极简 PNG 编码器）--------------------

def _png_chunk(tag, data):
    import zlib
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png_gray(path, width, height, pixels, scale=1):
    """写 8 位灰度 PNG。pixels 为 bytes，长度须为 width*height。

    不依赖 PIL —— 手写 PNG 容器（IHDR/IDAT/IEND），只用标准库 zlib。
    """
    if width <= 0 or height <= 0 or len(pixels) != width * height:
        return False
    if scale > 1:
        rows = []
        for y in range(height):
            row = pixels[y * width:(y + 1) * width]
            wide = b"".join(bytes([b]) * scale for b in row)
            rows.extend([wide] * scale)
        pixels = b"".join(rows)
        width *= scale
        height *= scale

    raw = bytearray()
    for y in range(height):
        raw.append(0)                              # 每行的 filter 类型 0
        raw.extend(pixels[y * width:(y + 1) * width])

    import zlib
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += _png_chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    return True


# 迷雾字节值 → 灰度（0=未探索，1=已探索，2=已见；具体取值以游戏为准）
FOG_GRAY = {0: 0, 1: 90, 2: 200, 3: 255}


def export_fog_maps(res, outdir, scale=1):
    """把每队的战争迷雾图导出为 PNG。返回写出的文件路径列表。"""
    os.makedirs(outdir, exist_ok=True)
    names = {t["index"]: team_display_name(t) for t in res["teams"]["teams"]}
    out = []
    for i, ex in enumerate(res["extended"]):
        fog = ex.get("fog") or {}
        if not fog.get("present") or not fog.get("width"):
            continue
        w, h, data = fog["width"], fog["height"], fog["data"]
        lut = bytes(bytearray(FOG_GRAY.get(b, b) for b in range(256)))
        pixels = data.translate(lut)
        idx = i
        fname = os.path.join(outdir, "队伍%d_迷雾.png" % idx)
        if write_png_gray(fname, w, h, pixels, scale=scale):
            out.append(fname)
    return out


# ======================================================================
# 命令行
# ======================================================================

def build_parser():
    ap = argparse.ArgumentParser(
        prog="rwsave_inspector.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="《铁锈战争》(Rusted Warfare) .rwsave 存档查看器 —— "
                    "输入存档路径，输出全部可读信息。",
        epilog="""示例：
  %(prog)s 存档.rwsave                  查看全部信息
  %(prog)s 存档.rwsave --units          额外列出每个单位
  %(prog)s 存档.rwsave --teams          额外展开每队详情
  %(prog)s 存档.rwsave --export out/    导出报告 + CSV + JSON + 地图 + 迷雾图
  %(prog)s 存档目录/ --export out/      批量处理目录下所有 .rwsave
  %(prog)s 存档.rwsave --json > d.json  全量 JSON 打到标准输出

说明：本工具只读存档，绝不修改。""")
    ap.add_argument("path", help="存档文件路径，或包含 .rwsave 的目录")
    ap.add_argument("--export", metavar="目录", help="把结果导出到指定目录")
    ap.add_argument("--no-json", action="store_true", help="导出时不写完整 JSON（体积大）")
    ap.add_argument("--no-map", action="store_true", help="导出时不写地图 .tmx")
    ap.add_argument("--no-fog", action="store_true", help="导出时不写迷雾图 PNG")
    ap.add_argument("--fog-scale", type=int, default=1, metavar="N",
                    help="迷雾图放大倍数（默认 1，219×262 这类小图建议 3）")
    ap.add_argument("--units", action="store_true", help="额外列出单位")
    ap.add_argument("--teams", action="store_true", help="额外展开队伍详情")
    ap.add_argument("--unit-limit", type=int, default=40, metavar="N",
                    help="单位列表最多显示多少条（默认 40）")
    ap.add_argument("--json", action="store_true", help="把全量 JSON 打到标准输出")
    ap.add_argument("--quiet", "-q", action="store_true", help="不打印文字报告")
    ap.add_argument("--version", action="version", version="rwsave_inspector %s" % __version__)
    return ap


def collect_targets(path):
    """把输入路径展开成存档文件列表。"""
    if os.path.isdir(path):
        out = []
        for root, _dirs, files in os.walk(path):
            for fn in sorted(files):
                if fn.lower().endswith(".rwsave"):
                    out.append(os.path.join(root, fn))
        return out
    if os.path.isfile(path):
        return [path]
    raise SaveError("路径不存在：%s" % path)


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    try:
        targets = collect_targets(args.path)
    except SaveError as e:
        print("错误：%s" % e, file=sys.stderr)
        return 2
    if not targets:
        print("错误：%s 下没有找到任何 .rwsave 文件" % args.path, file=sys.stderr)
        return 2

    ok = 0
    for i, path in enumerate(targets):
        if len(targets) > 1:
            print("\n[%d/%d] %s" % (i + 1, len(targets), os.path.basename(path)))
        try:
            res = parse_save(path)
        except SaveError as e:
            print("\n✘ 解析失败：%s\n  %s" % (os.path.basename(path), e), file=sys.stderr)
            if len(targets) == 1:
                return 1
            continue
        except Exception as e:                              # noqa: BLE001
            print("\n✘ 解析时发生意外错误：%s\n  %s: %s"
                  % (os.path.basename(path), type(e).__name__, e), file=sys.stderr)
            if len(targets) == 1:
                return 1
            continue
        ok += 1

        if args.json:
            payload = OrderedDict((k, jsonable(v)) for k, v in res.items()
                                  if k != "map_data")
            payload["map_data_bytes"] = len(res["map_data"] or b"")
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        elif not args.quiet:
            print_report(res, show_units=args.units, unit_limit=args.unit_limit)
            if args.teams:
                print()
                print_teams_verbose(res)
            if args.units:
                print()
                print_units_verbose(res, limit=args.unit_limit)

        if args.export:
            stem = safe_stem(os.path.basename(path))
            outdir = os.path.join(args.export, stem) if len(targets) > 1 else args.export
            try:
                written = export_one(res, outdir, stem,
                                     want_json=not args.no_json,
                                     want_map=not args.no_map)
                if not args.no_fog:
                    fdir = os.path.join(outdir, "%s_迷雾图" % stem)
                    try:
                        written.extend(export_fog_maps(res, fdir, scale=max(1, args.fog_scale)))
                    except Exception as e:              # noqa: BLE001
                        print("  （迷雾图导出跳过：%s）" % e, file=sys.stderr)
                # 导出清单总是打印（--quiet 只压制解析报告，不压制「写出了什么」）
                print("已导出 %d 个文件到：%s" % (len(written), os.path.abspath(outdir)))
                for w in written:
                    print("    %s" % os.path.basename(w))
            except OSError as e:
                print("导出失败：%s" % e, file=sys.stderr)

    print("\n完成：%d / %d 个存档解析成功。" % (ok, len(targets)))
    return 0 if ok == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
