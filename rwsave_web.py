#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rwsave_web.py — .rwsave 修改器网页版
=====================================

浏览器里查看 / 筛选 / 修改 `.rwsave`，改完直接存成新文件。

设计原则
--------
1. **复用而非重写**：解析与字节补丁全部委托给 `rwsave_edit_core`
   （它又复用同目录的 `rwsave_inspector.py`）。本文件只做 HTTP 层。
2. **绝不就地覆盖原存档**：保存时默认写到「输出/」目录并加时间戳后缀，
   原文件只读打开。误操作不会毁掉存档。
3. **保存前强制自检**：写盘后立刻重新解析 + 逐字节比对补丁点，
   不通过则删除产物并报错，绝不留下坏档。

用法：
    python rwsave_web.py                          # 扫描默认目录
    python rwsave_web.py --dir "D:/你的存档目录"   # 指定目录
    python rwsave_web.py --dir A --dir B --port 5001
"""

import argparse
import os
import sys
import threading
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import rwsave_edit_core as E                                # noqa: E402

try:
    from flask import Flask, jsonify, request, send_file, abort
except ImportError:                                         # pragma: no cover
    print("缺少 Flask。请先安装：pip install flask")
    raise

WEB_DIR = os.path.join(_HERE, "web")

# 没有 --dir 时的默认目录。**不预置任何样例**：整个文件夹拷到哪都能用，
# 用户把 .rwsave 放进 saves/ ，或直接 --dir / 拖拽 启动.bat 指定自己的目录。
DEFAULT_DIRS = [
    os.path.join(_HERE, "saves"),
]

app = Flask(__name__, static_folder=None)


# ---------------------------------------------------------------- 存档库

class Library:
    """管理已加载的存档：解析 → 编辑视图，带缓存与待提交编辑。"""

    def __init__(self, dirs):
        self.dirs = [d for d in dirs if d and os.path.isdir(d)]
        self._docs = {}                 # key -> doc
        self._edits = {}                # key -> {(unit, field): value}
        self._lock = threading.Lock()

    # -- 扫描 --
    def scan(self):
        out = []
        for d in self.dirs:
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            for fn in names:
                if fn.lower().endswith(".rwsave"):
                    p = os.path.join(d, fn)
                    try:
                        st = os.stat(p)
                        out.append({"path": p, "name": fn, "dir": d,
                                    "size": st.st_size,
                                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                                          time.localtime(st.st_mtime))})
                    except OSError:
                        pass
        return out

    @staticmethod
    def _key(path):
        try:
            st = os.stat(path)
            return (os.path.abspath(path), st.st_mtime_ns, st.st_size)
        except OSError:
            return (os.path.abspath(path), None, None)

    def get(self, path):
        """取存档的编辑视图（带缓存）。已提交的编辑会被重放。"""
        k = self._key(path)
        with self._lock:
            if k in self._docs:
                return self._docs[k]
        doc = E.load_editable(path)          # 较慢，放锁外
        pend = self._edits.setdefault(k, {})
        with self._lock:
            self._docs[k] = doc
        self._replay(doc, pend, path)
        return doc

    @staticmethod
    def _replay(doc, edits, path):
        """把待提交的编辑应用到内存视图（仅用于列表/详情预览，不落盘）。"""
        if not edits:
            return
        items = [{"unit": u, "field": f, "value": v} for (u, f), v in edits.items()]
        try:
            E.apply_edits(doc, items)
        except E.EditError:
            # 待提交编辑里混入非法项（不该发生）：整体丢弃并重载，保证视图可信
            edits.clear()
            fresh = E.load_editable(path)
            doc["root"] = fresh["root"]
            doc["units"] = fresh["units"]

    # -- 待提交编辑 --
    def pending(self, path):
        return self._edits.get(self._key(path), {})

    def stage(self, path, unit, field, value):
        d = self.get(path)
        try:
            ui = int(unit)
        except (TypeError, ValueError):
            raise E.EditError("unit 下标必须是整数，收到 %r" % (unit,)) from None
        if not (0 <= ui < len(d["units"])):
            raise E.EditError("对象下标 %d 越界（共 %d 个对象）" % (ui, len(d["units"])))
        u = d["units"][ui]
        f = u["fields"].get(field)
        if f is None:
            locked = {lf["name"]: lf for lf in (u.get("locked_fields") or [])}
            if field in locked:
                raise E.EditError("字段 %r 是只读的：%s" % (field, locked[field]["reason"]))
            raise E.EditError("对象 #%d 没有可编辑字段 %r" % (ui, field))
        b = E.encode_value(f["type"], value, field)          # 先编码校验
        if len(b) != f["size"]:
            raise E.EditError("字段 %r 编码长度不符" % field)
        with self._lock:
            self._edits.setdefault(self._key(path), {})[(ui, field)] = value
        return {"unit": ui, "field": field, "value": value,
                "label": f["label"], "type": f["type"]}

    def unstage_all(self, path):
        k = self._key(path)
        with self._lock:
            self._edits[k] = {}

    def drop(self, path):
        """从缓存移除（保存后调用，保证下次读到新文件）。"""
        k = self._key(path)
        with self._lock:
            self._docs.pop(k, None)
            self._edits.pop(k, None)


LIB = None


def lib():
    return LIB


# ---------------------------------------------------------------- 工具

def _f(v):
    """浮点 → JSON 安全（NaN/Inf → None）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _json_unit(u):
    """单位详情：字段按分组组织，标注是否可写 / 是否危险。

    「只读字段」来自内核的 FORBIDDEN 名单（改了会破坏字节布局），
    这里一并返回，让网页端能展示它们并说明原因，而不是直接消失。
    """
    groups = {}
    for name, f in u["fields"].items():
        groups.setdefault(f["group"], []).append({
            "name": name, "label": f["label"], "ui": f["ui"],
            "type": f["type"], "value": _f(f["value"]),
            "offset": f["offset"], "size": f["size"],
            "danger": f["danger"],
            "locked": False,
        })
    for lf in (u.get("locked_fields") or []):
        g = E.FIELD_META.get(lf["name"], ("其它",))[0]
        groups.setdefault(g, []).append({
            "name": lf["name"], "label": lf["label"], "ui": "x",
            "type": lf["type"], "value": _f(lf["value"]),
            "offset": None, "size": None, "danger": True,
            "locked": True, "lock_reason": lf["reason"],
        })
    for g in groups.values():
        g.sort(key=lambda x: x["name"])
    ordered = []
    for g in E.GROUP_ORDER:
        if g in groups:
            ordered.append({"group": g, "fields": groups.pop(g)})
    for g in sorted(groups):
        ordered.append({"group": g, "fields": groups[g]})
    return {
        "index": u["index"], "object_id": u["object_id"],
        "type_name": u["type_name"], "kind": u["kind"],
        "kind_name": u["kind_name"], "offset": u["offset"],
        "length": u["length"], "groups": ordered,
        "duplicate_offsets": u.get("duplicate_offsets"),
        "rejected": u.get("rejected"),
    }


# ---------------------------------------------------------------- 页面

@app.route("/")
def index():
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.route("/favicon.ico")
def favicon():
    """浏览器总会请求它；返回空 204，避免日志里堆 404/500 噪音。"""
    return "", 204


@app.route("/web/<path:fn>")
def webfile(fn):
    full = os.path.join(WEB_DIR, fn)
    if not os.path.isfile(full):
        abort(404)
    with open(full, encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": _mime(fn)}


def _mime(fn):
    return {"html": "text/html; charset=utf-8",
            "css": "text/css; charset=utf-8",
            "js": "application/javascript; charset=utf-8"}.get(
                fn.rsplit(".", 1)[-1], "text/plain; charset=utf-8")


# ---------------------------------------------------------------- API

@app.route("/api/saves")
def api_saves():
    return jsonify({"saves": LIB.scan(), "dirs": LIB.dirs})


@app.route("/api/save")
def api_save_info():
    """载入一个存档，返回概览 + 单位扁平行。"""
    path = request.args.get("path", "")
    if not os.path.isfile(path):
        return jsonify({"error": "存档不存在：%s" % path}), 404
    try:
        doc = LIB.get(path)
    except E.EditError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:                                    # noqa: BLE001
        return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 400

    rows = E.unit_rows(doc)
    pend = LIB.pending(path)
    # 每个单位被改动的字段数（前端直接用，省得自己再遍历一遍）
    cnt = {}
    for (u, _f) in pend:
        cnt[u] = cnt.get(u, 0) + 1
    for r in rows:
        r["pending"] = cnt.get(r["i"], 0)

    return jsonify({
        "path": path,
        "name": doc["name"],
        "summary": doc["summary"],
        "teams": doc["teams"],
        "stats": doc["stats"],
        "units": rows,
        "pending": [{"unit": u, "field": f, "value": _f(v)}
                    for (u, f), v in pend.items()],
    })


@app.route("/api/unit")
def api_unit():
    """单个单位的完整可编辑字段。"""
    path = request.args.get("path", "")
    idx = request.args.get("index", "")
    if not os.path.isfile(path):
        return jsonify({"error": "存档不存在"}), 404
    try:
        doc = LIB.get(path)
        i = int(idx)
        u = doc["units"][i]
    except (ValueError, IndexError):
        return jsonify({"error": "对象下标无效：%r" % idx}), 400
    except E.EditError as e:
        return jsonify({"error": str(e)}), 400

    out = _json_unit(u)
    pend = LIB.pending(path)
    out["edits"] = [{"unit": uu, "field": ff, "value": _f(vv)}
                    for (uu, ff), vv in pend.items() if uu == i]
    return jsonify(out)


@app.route("/api/edit", methods=["POST"])
def api_edit():
    """暂存一个字段修改（不落盘）。"""
    data = request.get_json(force=True) or {}
    try:
        r = LIB.stage(data.get("path"), data.get("unit"),
                      data.get("field"), data.get("value"))
    except E.EditError as e:
        return jsonify({"error": str(e)}), 400
    except (TypeError, ValueError) as e:
        return jsonify({"error": "参数无效：%s" % e}), 400
    return jsonify({"ok": True, "edit": r})


@app.route("/api/batch", methods=["POST"])
def api_batch():
    """批量修改：把某个字段的所有值设为同一个值。

    body: {path, field, value, units: [i, ...] | null(全部), filter: {...}}
    """
    data = request.get_json(force=True) or {}
    path = data.get("path")
    field = data.get("field")
    value = data.get("value")
    try:
        doc = LIB.get(path)
    except E.EditError as e:
        return jsonify({"error": str(e)}), 400

    targets = data.get("units")
    if targets is None:
        targets = [u["index"] for u in doc["units"]
                   if field in u["fields"] and _match_filter(u, data.get("filter"))]
    ok, errs = 0, []
    for i in targets:
        try:
            LIB.stage(path, i, field, value)
            ok += 1
        except E.EditError as e:
            errs.append("对象 #%s：%s" % (i, e))
    return jsonify({"ok": True, "applied": ok, "errors": errs[:20],
                    "error_count": len(errs)})


def _match_filter(u, flt):
    if not flt:
        return True
    for k, v in flt.items():
        if v is None or v == "":
            continue
        if k in ("team",):
            if str(u["fields"].get("team", {}).get("value")) != str(v):
                return False
        elif k == "type":
            if (u["type_name"] or "") != v:
                return False
        elif k == "hp_below":
            hp = u["fields"].get("current_health", {}).get("value")
            if hp is None or float(hp) > float(v):
                return False
        elif k == "hp_above":
            hp = u["fields"].get("current_health", {}).get("value")
            if hp is None or float(hp) < float(v):
                return False
    return True


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """丢弃全部待提交修改。"""
    data = request.get_json(force=True) or {}
    LIB.unstage_all(data.get("path"))
    return jsonify({"ok": True})


@app.route("/api/pending")
def api_pending():
    path = request.args.get("path", "")
    pend = LIB.pending(path)
    return jsonify({"pending": [{"unit": u, "field": f, "value": _f(v)}
                               for (u, f), v in pend.items()],
                    "count": len(pend)})


@app.route("/api/save", methods=["POST"])
def api_save_write():
    """写盘：应用待提交编辑 → 生成新文件 → 回读自检。

    绝不覆盖原文件。默认写到 `--out`（缺省 `<存档目录>/../输出` 或存档同级）。
    """
    data = request.get_json(force=True) or {}
    path = data.get("path")
    out_dir = data.get("out_dir") or os.path.join(
        os.path.dirname(os.path.abspath(path)), "modified")
    fname = data.get("name") or _default_name(path)

    try:
        doc = LIB.get(path)
    except E.EditError as e:
        return jsonify({"error": str(e)}), 400

    pend = LIB.pending(path)
    if not pend:
        return jsonify({"error": "没有待保存的修改"}), 400

    try:
        items = [{"unit": u, "field": f, "value": v} for (u, f), v in pend.items()]
        applied = E.apply_edits(doc, items)
    except E.EditError as e:
        return jsonify({"error": str(e)}), 400

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, fname)
    n = 1
    while os.path.exists(out_path):
        stem, ext = os.path.splitext(fname)
        out_path = os.path.join(out_dir, "%s_%d%s" % (stem, n, ext))
        n += 1

    info = E.save(doc, out_path)

    # ---- 保存后自检：重新解析 + 校验补丁点 ----
    verify = _verify_out(path, out_path, applied)
    if not verify["ok"]:
        try:
            os.remove(out_path)
        except OSError:
            pass
        return jsonify({"error": "保存后自检失败，已删除产物：%s" % verify["error"]}), 500

    LIB.unstage_all(path)
    LIB.drop(path)
    return jsonify({
        "ok": True, "path": out_path, "name": os.path.basename(out_path),
        "size": info["size"], "root_size": info["root_size"],
        "gzip_size": info["gzip_size"], "edits": len(applied),
        "verify": verify,
    })


def _default_name(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return "%s_已修改_%s.rwsave" % (stem, time.strftime("%Y%m%d-%H%M%S"))


def _verify_out(src, out, applied):
    """回读产物，确认可解析且补丁生效。"""
    try:
        with open(out, "rb") as f:
            raw = f.read()
        doc = E.load_editable_from_bytes(raw, os.path.basename(out))
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": "产物无法解析：%s: %s" % (type(e).__name__, e)}

    with open(src, "rb") as f:
        orig = E.load_editable_from_bytes(f.read(), os.path.basename(src))
    if len(doc["root"]) != len(orig["root"]):
        return {"ok": False, "error": "解压后长度变化：%d → %d"
                % (len(orig["root"]), len(doc["root"]))}

    # 补丁点生效 + 非补丁字节不变
    spans = [(a["offset"], a["size"]) for a in applied]
    changed = 0
    for i in range(len(doc["root"])):
        inside = any(off <= i < off + n for off, n in spans)
        if inside:
            if doc["root"][i] != orig["root"][i]:
                changed += 1
        elif doc["root"][i] != orig["root"][i]:
            return {"ok": False, "error": "非补丁字节 0x%X 被改动" % i}

    # 值回读：以**字节**为准，而不是 Python 值的相等性。
    # 网页输入框提交的是字符串（'771'），解析器回读出的是浮点（771.0），
    # 直接比 `got != a["new"]` 会把正确产物误判为失败并删除。
    # 这里先把期望值按字段类型编码，再与产物中该 offset 的字节逐位比对。
    for a in applied:
        want = E.encode_value(a["type"], a["new"], a["field"])
        off, n = a["offset"], a["size"]
        if len(want) != n:
            return {"ok": False, "error": "字段 %s 期望编码长度 %d ≠ 字段宽度 %d"
                    % (a["field"], len(want), n)}
        got_bytes = bytes(doc["root"][off:off + n])
        if got_bytes != want:
            u = doc["units"][a["unit"]]
            got = u["fields"].get(a["field"], {}).get("value")
            return {"ok": False, "error": "字段 %s 期望 %r 实得 %r（字节 %s ≠ %s）"
                    % (a["field"], a["new"], got, got_bytes.hex(), want.hex())}

    return {"ok": True, "objects": len(doc["units"]),
            "patched_bytes": sum(n for _o, n in spans),
            "changed_bytes": changed}


@app.route("/api/download")
def api_download():
    """把某个 .rwsave 作为附件下载（用于取回原档）。"""
    path = request.args.get("path", "")
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=os.path.basename(path))


@app.errorhandler(404)
def on_404(e):
    """未命中路由。API 请求回 JSON，页面请求回一段提示。

    注意：这里必须显式处理，否则会被下面的 Exception 兜底吞成 500。
    """
    if request.path.startswith("/api/"):
        return jsonify({"error": "接口不存在：%s" % request.path}), 404
    from html import escape
    return ("<h3>404</h3><p>%s 不存在。<a href='/'>回到首页</a></p>"
            % escape(request.path)), 404


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, EditError):
        return jsonify({"error": str(e)}), 400
    traceback.print_exc()
    return jsonify({"error": "%s: %s" % (type(e).__name__, e)}), 500


# ---------------------------------------------------------------- 启动

def _auto_stop(seconds):
    """仅供自动化测试（battest.py）用：设了正数的等待秒数后，到点自动关服务。

    ⚠️ `seconds <= 0` 时**什么都不做**。绝不能无脑启动一个 `sleep(0)` 线程 ——
    那会让进程在 `app.run()` 之前就 `os._exit(0)` 自杀，
    表现为「打印完启动横幅就退出、浏览器拒绝连接」。
    """
    if seconds <= 0:
        return
    import threading

    def _bye():
        time.sleep(seconds)
        os._exit(0)
    threading.Thread(target=_bye, daemon=True).start()


def main():
    global LIB
    ap = argparse.ArgumentParser(description="rwsave 修改器 · 网页版")
    ap.add_argument("--dir", action="append", default=[],
                    help="存档目录，可重复指定")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    dirs = args.dir or DEFAULT_DIRS
    LIB = Library(dirs)

    n = len(LIB.scan())
    print("=" * 60)
    print("rwsave 修改器 · 网页版")
    print("  扫描目录：%s" % ("、".join(dirs) or "（无）"))
    print("  发现存档：%d 个" % n)
    print("  打开浏览器： http://%s:%d" % (args.host, args.port))
    print("=" * 60)

    # 仅测试用：设了 RWX_TEST_MAX_SECONDS 才限时自退；未设置则常驻
    _auto_stop(int(os.environ.get("RWX_TEST_MAX_SECONDS", "0")))

    url = "http://%s:%d" % (args.host, args.port)
    if not args.no_browser:
        threading.Timer(1.0, lambda: __import__("webbrowser").open(url)).start()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()