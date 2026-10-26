#!/usr/bin/env python3
"""拼影工坊 ClipForge —— 多模型 AI 视频拼接平台（仅依赖 Python 3.8+ 标准库 + ffmpeg）

启动：python3 app.py
环境变量：
  CF_BIND      监听地址，默认 127.0.0.1（对外开放用 0.0.0.0，此时必须设置 CF_PASSWORD）
  CF_PORT      端口，默认 8800
  CF_PASSWORD  登录密码
  CF_DATA      数据目录，默认 ./data
"""
import hashlib, hmac, http.cookies, http.server, json, mimetypes, os, queue, re, secrets, shutil, sqlite3, sys, threading, time, traceback
import urllib.parse, urllib.request
from datetime import datetime

import providers as P
import render as R
import router as RT

HERE = os.path.dirname(os.path.abspath(__file__))
BIND = os.environ.get("CF_BIND", "127.0.0.1")
PORT = int(os.environ.get("CF_PORT", "8800"))
PASSWORD = os.environ.get("CF_PASSWORD", "")
DATA = os.path.abspath(os.environ.get("CF_DATA", os.path.join(HERE, "data")))
MEDIA = os.path.join(DATA, "media")
SUBDIRS = ("videos", "images", "music", "renders", "thumbs", "tmp")
IS_LOCAL = BIND in ("127.0.0.1", "localhost", "::1")
MAX_UPLOAD = 1024 * 1024 * 1024  # 1GB
POLL_SECONDS = 10

for d in SUBDIRS:
    os.makedirs(os.path.join(MEDIA, d), exist_ok=True)


def log(*a):
    print(datetime.now().strftime("%m-%d %H:%M:%S"), *a, flush=True)


# ====================================================================== DB
class DB:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, name TEXT, aspect TEXT DEFAULT '9:16', fit TEXT DEFAULT 'blur',
            transition TEXT DEFAULT 'fadewhite', tdur REAL DEFAULT 0.4, music TEXT, music_name TEXT, music_vol REAL DEFAULT 0.6,
            keep_audio INTEGER DEFAULT 1, orig_vol REAL DEFAULT 1.0, created REAL, updated REAL);
        CREATE TABLE IF NOT EXISTS shots(id INTEGER PRIMARY KEY, project_id INTEGER, idx INTEGER, title TEXT, prompt TEXT DEFAULT '',
            image TEXT, selected_take INTEGER, created REAL);
        CREATE TABLE IF NOT EXISTS takes(id INTEGER PRIMARY KEY, shot_id INTEGER, source TEXT, model TEXT, params TEXT, prompt TEXT,
            status TEXT, task_id TEXT, file TEXT, thumb TEXT, duration REAL, start REAL DEFAULT 0, end_t REAL DEFAULT 0, speed REAL DEFAULT 1,
            error TEXT, errors INTEGER DEFAULT 0, created REAL, finished REAL);
        CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY, provider TEXT, kind TEXT, model TEXT, day TEXT, created REAL, ok INTEGER, ref TEXT);
        CREATE TABLE IF NOT EXISTS renders(id INTEGER PRIMARY KEY, project_id INTEGER, status TEXT, progress REAL DEFAULT 0, msg TEXT,
            file TEXT, duration REAL, error TEXT, created REAL);
        """)
        for sql in ("ALTER TABLE usage ADD COLUMN cost REAL", "ALTER TABLE projects ADD COLUMN style TEXT",
                    "ALTER TABLE projects ADD COLUMN character TEXT", "ALTER TABLE projects ADD COLUMN auto_translate INTEGER DEFAULT 1"):
            try:
                self.c.execute(sql)
            except sqlite3.OperationalError:
                pass  # 已存在
        self.c.commit()

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.q(sql, args)
        return r[0] if r else None

    def x(self, sql, args=()):
        with self.lock:
            cur = self.c.execute(sql, args)
            self.c.commit()
            return cur.lastrowid

    def kv_get(self, k, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def kv_set(self, k, v):
        self.x("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))


db = DB(os.path.join(DATA, "clipforge.db"))
_secret_path = os.path.join(DATA, "secret.key")
if not os.path.exists(_secret_path):
    _fd = os.open(_secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(_fd, "w") as f:
        f.write(secrets.token_hex(32))
for _p in (DATA, _secret_path, os.path.join(DATA, "clipforge.db")):
    try:
        os.chmod(_p, 0o700 if os.path.isdir(_p) else 0o600)
    except OSError:
        pass
SECRET = open(_secret_path).read().strip().encode()
SESSION = hmac.new(SECRET, ("session:" + hashlib.sha256(PASSWORD.encode()).hexdigest()).encode(), hashlib.sha256).hexdigest()


def pcfg(pid):
    return db.kv_get("provider:" + pid, {}) or {}


wallet = RT.Wallet(db, pcfg)


def prefs():
    return dict({"allow_paid": False, "pref": "save"}, **(db.kv_get("prefs", {}) or {}))


def today():
    return datetime.now().strftime("%Y-%m-%d")


def mpath(rel):
    """media 相对路径 -> 绝对路径（防目录穿越）"""
    if not rel:
        return None
    sub, _, name = rel.partition("/")
    if sub not in SUBDIRS or not name or "/" in name or name.startswith("."):
        return None
    return os.path.join(MEDIA, sub, name)


def safe_unlink(rel):
    p = mpath(rel)
    if p and os.path.exists(p):
        try:
            os.remove(p)
        except OSError:
            pass


def download(url, rel, headers=None):
    dst = mpath(rel)
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "ClipForge/1.0"})
    tmp = dst + ".part"
    cap, got = 1024 * 1024 * 1024, 0  # 单个文件最多 1GB，防止异常响应写满磁盘
    try:
        with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                got += len(chunk)
                if got > cap:
                    raise IOError("下载文件超过 1GB，已中止")
                f.write(chunk)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, dst)
    return dst


# ====================================================================== sample
SAMPLE = [
    ("① 出场・凝弓", "国风玄幻CG电影画面，超高细节，体积光，冷蓝色调。夜空中悬着一轮巨大的明月，下方云海翻涌，一座破碎的浮空断崖漂浮在云海之上。[Push in] 镜头从远处缓慢推向断崖，崖顶站着一位东方女弓手：黑色长发高高束起，带一缕银白挑染，身穿深蓝与月白相间的轻甲长裙，腰间两条长长的青玉色飘带随风飞扬。她向身后的明月伸出一只手，无数银金色月光碎片如流星般从四面八方飞来，汇聚在她手中，凝结成一把巨大的银金色新月长弓，弓身镶着青玉宝石。长弓成形的瞬间爆发出一圈金色光波，飘带和长发猛然扬起。[Truck right,Push in] 镜头环绕到她侧面，她握弓的身影被月光勾出明亮的轮廓光。"),
    ("② 蹬弓・蓄力・松弦", "国风玄幻CG电影画面，超高细节，强烈轮廓光，慢动作。背景是占据整个画面的巨大明月。一位东方女弓手，黑色长发带一缕银白挑染，身穿深蓝与月白相间的轻甲长裙，腰系青玉色长飘带，身体后仰悬浮在空中，一条腿伸直，用脚掌稳稳蹬住巨大银金色新月长弓的弓把，另一条腿弯曲，双手在胸前把金色光弦缓缓拉成满月。[Truck left] 镜头缓慢环绕她，长发、裙摆和飘带在风中剧烈飞舞，周围碎石悬浮旋转。一支金色能量箭搭在弦上，箭尖前方层层展开旋转的金色符文法阵，月光像丝线一样被吸进箭尖，金色电弧缠绕箭身，光芒越来越亮。她松开手指，弓弦剧烈震荡，以弓为中心爆发出环形冲击波，[Shake] 金色光箭拖着耀眼的光尾高速射出画面。"),
    ("③ 跟箭・命中", "国风玄幻CG电影画面，超高细节，动态模糊，史诗感。[Tracking shot] 镜头紧跟一支燃烧着金色光焰、拖着长长光尾的箭高速向前飞行，穿越翻涌的云海和漂浮的陨石群，云层与陨石从两侧飞速掠过。远方夜空中一道巨大的暗紫色空间裂隙迅速逼近。箭贯穿裂隙中心，[Shake] 耀眼的金光猛烈爆发，放射状光柱和冲击波环向四周扩散，裂隙崩碎成无数金色光点，像光雨一样缓缓飘落，照亮下方的异星荒原。[Pull out] 镜头缓慢拉远，前景浮空断崖上，一位黑色长发、深蓝月白长裙、腰系青玉飘带的女弓手背影放下长弓，飘带在风中扬起。"),
]


# ====================================================================== services
def project_full(pid):
    p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        return None
    shots = db.q("SELECT * FROM shots WHERE project_id=? ORDER BY idx, id", (pid,))
    for s in shots:
        s["takes"] = db.q("SELECT * FROM takes WHERE shot_id=? ORDER BY id DESC", (s["id"],))
        for t in s["takes"]:
            t["params"] = json.loads(t["params"] or "{}")
    p["shots"] = shots
    p["renders"] = db.q("SELECT * FROM renders WHERE project_id=? ORDER BY id DESC LIMIT 10", (pid,))
    return p


def touch(pid):
    db.x("UPDATE projects SET updated=? WHERE id=?", (time.time(), pid))


def reindex(pid):
    for i, s in enumerate(db.q("SELECT id FROM shots WHERE project_id=? ORDER BY idx, id", (pid,))):
        db.x("UPDATE shots SET idx=? WHERE id=?", (i, s["id"]))


def usage_today(provider, kind="video"):
    return db.one("SELECT COUNT(*) n FROM usage WHERE provider=? AND kind=? AND day=? AND ok=1", (provider, kind, today()))["n"]


def finish_take_file(tid, rel):
    path = mpath(rel)
    dur = 0
    try:
        dur = R.probe(path)[0]
    except Exception:
        pass
    thumb = "thumbs/take_%d.jpg" % tid
    has_thumb = R.thumbnail(path, mpath(thumb))
    db.x("UPDATE takes SET status='succeeded', file=?, thumb=?, duration=?, error=NULL, finished=? WHERE id=?",
         (rel, thumb if has_thumb else None, dur, time.time(), tid))
    t = db.one("SELECT shot_id FROM takes WHERE id=?", (tid,))
    s = db.one("SELECT * FROM shots WHERE id=?", (t["shot_id"],))
    if s and not s["selected_take"]:
        db.x("UPDATE shots SET selected_take=? WHERE id=?", (tid, s["id"]))


def poll_take(t):
    cfg = pcfg(t["source"])
    try:
        res = P.query(t["source"], cfg, t["model"], t["task_id"])
    except Exception as e:
        n = (t["errors"] or 0) + 1
        st = "failed" if n >= 6 else t["status"]
        db.x("UPDATE takes SET errors=?, error=?, status=? WHERE id=?", (n, "查询出错：%s" % e, st, t["id"]))
        return
    if res["status"] == "succeeded":
        rel = "videos/take_%d.mp4" % t["id"]
        try:
            if res.get("data"):
                with open(mpath(rel), "wb") as f:
                    f.write(res["data"])
            elif res.get("url"):
                download(res["url"], rel, res.get("headers"))
            else:
                raise RuntimeError("平台没有返回视频地址")
            finish_take_file(t["id"], rel)
            log("take %d 完成" % t["id"])
        except Exception as e:
            db.x("UPDATE takes SET errors=errors+1, error=? WHERE id=?", ("下载视频失败（会自动重试）：%s" % e, t["id"]))
    elif res["status"] == "failed":
        err = res.get("error") or "生成失败"
        params = json.loads(t["params"] or "{}")
        if params.get("auto") and int(params.get("retry") or 0) < 1:
            # 智能模式：失败自动换一家重试一次（失败的任务通常不扣额度）
            if RT.classify_error(err) == "quota":
                wallet.mark_exhausted(t["source"], err, RT.budget(t["source"], pcfg(t["source"]))["free_kind"] in ("daily", "free"))
            shot = db.one("SELECT * FROM shots WHERE id=?", (t["shot_id"],))
            project = db.one("SELECT * FROM projects WHERE id=?", (shot["project_id"],)) if shot else None
            if shot and project:
                try:
                    r = auto_generate(shot, project, float(params.get("target") or 5), params.get("pref") or "save",
                                      bool(params.get("allow_paid")), params.get("mode") == "i2v", exclude=(t["source"],), retry=1)
                    err += "（已自动换 %s 重试）" % r["provider"]
                except Exception as e:
                    err += "（自动重试也失败：%s）" % str(e)[:100]
        db.x("UPDATE takes SET status='failed', error=?, finished=? WHERE id=?", (err, time.time(), t["id"]))
    else:
        db.x("UPDATE takes SET status='running', errors=0, error=NULL WHERE id=?", (t["id"],))
        if time.time() - (t["created"] or 0) > 3 * 3600:
            db.x("UPDATE takes SET status='failed', error='超过 3 小时未完成' WHERE id=?", (t["id"],))


def poller():
    while True:
        try:
            for t in db.q("SELECT * FROM takes WHERE status IN ('queued','running') AND task_id IS NOT NULL"):
                poll_take(t)
        except Exception:
            traceback.print_exc()
        time.sleep(POLL_SECONDS)


render_q = queue.Queue()


def render_worker():
    while True:
        rid = render_q.get()
        r = db.one("SELECT * FROM renders WHERE id=?", (rid,))
        if not r:
            continue
        p = project_full(r["project_id"])
        try:
            items = []
            for s in p["shots"]:
                take = next((t for t in s["takes"] if t["id"] == s["selected_take"] and t["status"] == "succeeded"), None)
                if not take:
                    continue
                items.append({"path": mpath(take["file"]), "start": take["start"], "end": take["end_t"], "speed": take["speed"], "label": s["title"]})
            opts = {k: p[k] for k in ("aspect", "fit", "transition", "tdur", "music_vol", "keep_audio", "orig_vol")}
            opts["music"] = mpath(p["music"]) if p["music"] else None
            rel = "renders/project%d_%s.mp4" % (p["id"], datetime.now().strftime("%Y%m%d_%H%M%S"))

            def prog(x, msg):
                db.x("UPDATE renders SET status='running', progress=?, msg=? WHERE id=?", (x, msg, rid))
            dur = R.render_project(items, opts, os.path.join(MEDIA, "tmp"), mpath(rel), prog)
            db.x("UPDATE renders SET status='succeeded', progress=1, file=?, duration=?, msg='完成' WHERE id=?", (rel, dur, rid))
            log("render %d 完成" % rid)
        except Exception as e:
            db.x("UPDATE renders SET status='failed', error=? WHERE id=?", (str(e), rid))


# ====================================================================== HTTP
class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


class Req:
    def __init__(self, h, body_reader, query):
        self.h, self.read_body, self.query = h, body_reader, query

    def json(self):
        raw = self.read_body()
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            raise ApiError("请求格式错误")


# ---------------- auth / meta
@route("GET", "/api/ping")
def r_ping(req):
    return {"auth_required": bool(PASSWORD), "logged_in": req.h.authed()}


_LOGIN_FAILS = {}  # ip -> [失败时间戳]
_LOGIN_LOCK = threading.Lock()


def _client_ip(req):
    return req.h.headers.get("X-Forwarded-For", "").split(",")[0].strip() or req.h.client_address[0]


@route("POST", "/api/login")
def r_login(req):
    ip = _client_ip(req)
    now = time.time()
    with _LOGIN_LOCK:
        fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < 600]
        _LOGIN_FAILS[ip] = fails
        if len(fails) >= 8:
            raise ApiError("尝试次数过多，请 10 分钟后再试", 429)
    pw = req.json().get("password", "")
    if not isinstance(pw, str):
        pw = ""
    if not PASSWORD or hmac.compare_digest(pw.encode(), PASSWORD.encode()):
        secure = "; Secure" if req.h.headers.get("X-Forwarded-Proto", "") == "https" else ""
        req.h.set_cookie = "cf_session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000%s" % (SESSION, secure)
        return {"ok": True}
    with _LOGIN_LOCK:
        _LOGIN_FAILS.setdefault(ip, []).append(now)
    time.sleep(1)
    raise ApiError("密码错误", 401)


@route("POST", "/api/logout")
def r_logout(req):
    req.h.set_cookie = "cf_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
    return {"ok": True}


@route("GET", "/api/meta")
def r_meta(req):
    provs = []
    for pid in P.PROVIDERS:
        d = P.provider_public(pid, pcfg(pid))
        d["budget"] = {k: v for k, v in RT.budget(pid, pcfg(pid)).items() if k != "since"}
        d["text_model"] = pcfg(pid).get("text_model", "")
        provs.append(d)
    return {"providers": provs,
            "transitions": R.TRANSITIONS, "aspects": list(R.SIZES.keys()),
            "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))}


@route("PUT", "/api/providers/(\\w+)")
def r_provider_save(req, pid):
    if pid not in P.PROVIDERS:
        raise ApiError("未知平台", 404)
    body = req.json()
    cfg = pcfg(pid)
    for f in P.PROVIDERS[pid]["fields"]:
        v = body.get(f["k"])
        if v is not None and v != "":
            cfg[f["k"]] = v.strip()
        if body.get("clear_" + f["k"]):
            cfg.pop(f["k"], None)
    for k in ("region", "extra_models", "quota_note", "text_model"):
        if k in body:
            cfg[k] = str(body[k]).strip()
    changed = False
    for k in ("free_kind", "unit"):
        if body.get(k) and body[k] != cfg.get(k):
            cfg[k] = body[k]; changed = True
    if "amount" in body:
        v = float(body["amount"] or 0)
        if v != float(cfg.get("amount", -1) if cfg.get("amount") not in (None, "") else -1):
            cfg["amount"] = v; changed = True
    if "auto" in body:
        cfg["auto"] = bool(body["auto"])
    if changed:
        cfg["since"] = time.time()  # 额度设置变了，从现在开始重新计算用量
        wallet.clear_exhausted(pid)
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if "daily_limit" in body:
        cfg["daily_limit"] = max(0, int(body["daily_limit"] or 0))
    if any(body.get(f["k"]) for f in P.PROVIDERS[pid]["fields"]):
        wallet.clear_exhausted(pid)  # 换了 Key
    db.kv_set("provider:" + pid, cfg)
    return P.provider_public(pid, cfg)


@route("GET", "/api/usage")
def r_usage(req):
    out = []
    for pid in P.PROVIDERS:
        row = {"provider": pid, "name": P.PROVIDERS[pid]["name"]}
        for kind in ("video", "image"):
            row[kind + "_today"] = usage_today(pid, kind)
            row[kind + "_total"] = db.one("SELECT COUNT(*) n FROM usage WHERE provider=? AND kind=? AND ok=1", (pid, kind))["n"]
        cfg = pcfg(pid)
        row["daily_limit"] = cfg.get("daily_limit", 0)
        row["quota_note"] = cfg.get("quota_note", "")
        out.append(row)
    recent = db.q("SELECT * FROM usage ORDER BY id DESC LIMIT 50")
    return {"providers": out, "recent": recent}


# ---------------- projects
@route("GET", "/api/projects")
def r_projects(req):
    rows = db.q("""SELECT p.*, (SELECT COUNT(*) FROM shots s WHERE s.project_id=p.id) shots,
        (SELECT t.thumb FROM takes t JOIN shots s ON s.id=t.shot_id WHERE s.project_id=p.id AND t.thumb IS NOT NULL ORDER BY t.id DESC LIMIT 1) cover
        FROM projects p ORDER BY updated DESC""")
    return {"projects": rows}


@route("POST", "/api/projects")
def r_project_new(req):
    b = req.json()
    now = time.time()
    aspect = b.get("aspect") if b.get("aspect") in R.SIZES else "9:16"
    pid = db.x("INSERT INTO projects(name, aspect, created, updated) VALUES(?,?,?,?)", ((b.get("name") or "未命名项目").strip()[:60], aspect, now, now))
    if b.get("sample"):
        for i, (t, pr) in enumerate(SAMPLE):
            db.x("INSERT INTO shots(project_id, idx, title, prompt, created) VALUES(?,?,?,?,?)", (pid, i, t, pr, now))
    else:
        db.x("INSERT INTO shots(project_id, idx, title, prompt, created) VALUES(?,?,?,?,?)", (pid, 0, "镜头 1", "", now))
    return {"id": pid}


@route("GET", "/api/projects/(\\d+)")
def r_project(req, pid):
    p = project_full(int(pid))
    if not p:
        raise ApiError("项目不存在", 404)
    return p


@route("PUT", "/api/projects/(\\d+)")
def r_project_save(req, pid):
    b = req.json()
    sets, args = [], []
    for k, conv in (("name", lambda v: str(v)[:60]), ("aspect", lambda v: v if v in R.SIZES else "9:16"),
                    ("fit", lambda v: v if v in ("blur", "crop", "pad") else "blur"),
                    ("transition", lambda v: v if v in R.TRANSITIONS else "fade"), ("tdur", lambda v: max(0.1, min(2.0, float(v)))),
                    ("music_vol", lambda v: max(0.0, min(2.0, float(v)))), ("orig_vol", lambda v: max(0.0, min(2.0, float(v)))),
                    ("keep_audio", lambda v: 1 if v else 0), ("style", lambda v: str(v)[:1000]), ("character", lambda v: str(v)[:1000]),
                    ("auto_translate", lambda v: 1 if v else 0)):
        if k in b:
            sets.append(k + "=?"); args.append(conv(b[k]))
    if sets:
        db.x("UPDATE projects SET %s, updated=? WHERE id=?" % ",".join(sets), args + [time.time(), int(pid)])
    return {"ok": True}


@route("DELETE", "/api/projects/(\\d+)")
def r_project_del(req, pid):
    pid = int(pid)
    p = db.one("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        return {"ok": True}
    for s in db.q("SELECT id FROM shots WHERE project_id=?", (pid,)):
        delete_shot(s["id"])
    for r in db.q("SELECT file FROM renders WHERE project_id=?", (pid,)):
        safe_unlink(r["file"])
    safe_unlink(p["music"])
    db.x("DELETE FROM renders WHERE project_id=?", (pid,))
    db.x("DELETE FROM projects WHERE id=?", (pid,))
    return {"ok": True}


# ---------------- shots
@route("POST", "/api/projects/(\\d+)/shots")
def r_shot_new(req, pid):
    b = req.json()
    pid = int(pid)
    n = db.one("SELECT COUNT(*) n FROM shots WHERE project_id=?", (pid,))["n"]
    sid = db.x("INSERT INTO shots(project_id, idx, title, prompt, created) VALUES(?,?,?,?,?)",
               (pid, n, (b.get("title") or "镜头 %d" % (n + 1))[:60], b.get("prompt", ""), time.time()))
    touch(pid)
    return {"id": sid}


@route("POST", "/api/projects/(\\d+)/shots/bulk")
def r_shot_bulk(req, pid):
    pid = int(pid)
    text = req.json().get("text", "")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    n = db.one("SELECT COUNT(*) n FROM shots WHERE project_id=?", (pid,))["n"]
    for i, b in enumerate(blocks):
        lines = b.splitlines()
        title, prompt = "镜头 %d" % (n + i + 1), b
        if len(lines) > 1 and len(lines[0]) <= 30:
            title, prompt = lines[0].strip("#*：: "), "\n".join(lines[1:]).strip()
        db.x("INSERT INTO shots(project_id, idx, title, prompt, created) VALUES(?,?,?,?,?)", (pid, n + i, title[:60], prompt, time.time()))
    touch(pid)
    return {"added": len(blocks)}


@route("PUT", "/api/shots/(\\d+)")
def r_shot_save(req, sid):
    b = req.json()
    s = db.one("SELECT * FROM shots WHERE id=?", (int(sid),))
    if not s:
        raise ApiError("镜头不存在", 404)
    if "title" in b:
        db.x("UPDATE shots SET title=? WHERE id=?", (str(b["title"])[:60], s["id"]))
    if "prompt" in b:
        db.x("UPDATE shots SET prompt=? WHERE id=?", (str(b["prompt"])[:4000], s["id"]))
    touch(s["project_id"])
    return {"ok": True}


@route("POST", "/api/shots/(\\d+)/move")
def r_shot_move(req, sid):
    d = int(req.json().get("dir", 0))
    s = db.one("SELECT * FROM shots WHERE id=?", (int(sid),))
    if not s:
        raise ApiError("不存在或已被删除", 404)
    reindex(s["project_id"])
    s = db.one("SELECT * FROM shots WHERE id=?", (int(sid),))
    if not s:
        raise ApiError("不存在或已被删除", 404)
    other = db.one("SELECT * FROM shots WHERE project_id=? AND idx=?", (s["project_id"], s["idx"] + d))
    if other:
        db.x("UPDATE shots SET idx=? WHERE id=?", (other["idx"], s["id"]))
        db.x("UPDATE shots SET idx=? WHERE id=?", (s["idx"], other["id"]))
    return {"ok": True}


def delete_shot(sid):
    s = db.one("SELECT * FROM shots WHERE id=?", (sid,))
    if not s:
        return
    for t in db.q("SELECT * FROM takes WHERE shot_id=?", (sid,)):
        safe_unlink(t["file"]); safe_unlink(t["thumb"])
    safe_unlink(s["image"])
    db.x("DELETE FROM takes WHERE shot_id=?", (sid,))
    db.x("DELETE FROM shots WHERE id=?", (sid,))
    reindex(s["project_id"])


@route("DELETE", "/api/shots/(\\d+)")
def r_shot_del(req, sid):
    delete_shot(int(sid))
    return {"ok": True}


def save_upload(req, rel, max_bytes=MAX_UPLOAD):
    n = req.h.clen()
    if n <= 0:
        raise ApiError("文件是空的")
    if n > max_bytes:
        raise ApiError("文件太大（上限 %dMB）" % (max_bytes // 1024 // 1024))
    dst = mpath(rel)
    left = n
    with open(dst + ".part", "wb") as f:
        while left > 0:
            chunk = req.h.rfile.read(min(left, 1024 * 256))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left:
        os.remove(dst + ".part")
        raise ApiError("上传中断")
    os.replace(dst + ".part", dst)
    req.consumed = True
    return dst


def ext_of(name, allowed, default):
    e = os.path.splitext(name or "")[1].lower().lstrip(".")
    return e if e in allowed else default


@route("POST", "/api/shots/(\\d+)/image")
def r_shot_image(req, sid):
    s = db.one("SELECT * FROM shots WHERE id=?", (int(sid),))
    if not s:
        raise ApiError("镜头不存在", 404)
    ext = ext_of(req.query.get("name", ""), ("jpg", "jpeg", "png", "webp"), "jpg")
    rel = "images/shot_%d_%d.%s" % (s["id"], int(time.time()), ext)
    save_upload(req, rel, 20 * 1024 * 1024)
    safe_unlink(s["image"])
    db.x("UPDATE shots SET image=? WHERE id=?", (rel, s["id"]))
    return {"image": rel}


@route("DELETE", "/api/shots/(\\d+)/image")
def r_shot_image_del(req, sid):
    s = db.one("SELECT * FROM shots WHERE id=?", (int(sid),))
    if not s:
        raise ApiError("镜头不存在", 404)
    safe_unlink(s["image"])
    db.x("UPDATE shots SET image=NULL WHERE id=?", (s["id"],))
    return {"ok": True}


@route("POST", "/api/shots/(\\d+)/genimage")
def r_shot_genimage(req, sid):
    b = req.json()
    s = db.one("SELECT * FROM shots WHERE id=?", (int(sid),))
    if not s:
        raise ApiError("镜头不存在", 404)
    pid = b.get("provider")
    cfg = pcfg(pid)
    if pid not in P.PROVIDERS or not P.provider_public(pid, cfg)["ready"]:
        raise ApiError("请先在「模型设置」里配置这个平台")
    prompt = (b.get("prompt") or s["prompt"] or "").strip()
    if not prompt:
        raise ApiError("提示词为空")
    try:
        img = P.gen_image(pid, cfg, b.get("model"), prompt[:1500], b.get("ratio") or "9:16")
        rel = "images/shot_%d_%d.%s" % (s["id"], int(time.time()), "png" if isinstance(img, bytes) and img[:4] == b"\x89PNG" else "jpg")
        if isinstance(img, bytes):
            with open(mpath(rel), "wb") as f:
                f.write(img)
        else:
            download(img, rel)
    except Exception as e:
        db.x("INSERT INTO usage(provider, kind, model, day, created, ok, ref) VALUES(?,?,?,?,?,0,?)", (pid, "image", b.get("model"), today(), time.time(), "shot %s" % sid))
        raise ApiError(str(e))
    safe_unlink(s["image"])
    db.x("UPDATE shots SET image=? WHERE id=?", (rel, s["id"]))
    cost = RT.cost_of(pid, b.get("model"), 0, "image")[0]
    db.x("INSERT INTO usage(provider, kind, model, day, created, ok, ref, cost) VALUES(?,?,?,?,?,1,?,?)", (pid, "image", b.get("model"), today(), time.time(), "shot %s" % sid, cost))
    return {"image": rel}


def submit_take(shot, project, pid, model, opt, use_image, extra=None):
    """提交到指定平台；自动拼角色/风格、适配运镜、海外模型翻译成英文。返回 take id，失败抛 ProviderError"""
    cfg = pcfg(pid)
    image = mpath(shot["image"]) if (use_image and shot["image"]) else None
    prompt = RT.compose_prompt(project, shot, pid)
    translated = False
    if pid in RT.EN_PREFER and project.get("auto_translate", 1):
        en = RT.translate(db, pcfg, prompt)
        if en:
            prompt, translated = en, en != RT.compose_prompt(project, shot, pid)
    task_id = P.submit(pid, cfg, model, prompt, opt, image)
    params = dict(opt, mode="i2v" if image else "t2v", translated=translated, **(extra or {}))
    end_t = 0
    tgt = (extra or {}).get("target")
    if tgt and opt.get("duration") and float(opt["duration"]) > float(tgt):
        end_t = float(tgt)  # 平台只能生成更长的，合成时自动裁到目标时长
    tid = db.x("INSERT INTO takes(shot_id, source, model, params, prompt, status, task_id, created, end_t) VALUES(?,?,?,?,?,?,?,?,?)",
               (shot["id"], pid, model, json.dumps(params, ensure_ascii=False), prompt, "running", task_id, time.time(), end_t))
    cost = RT.cost_of(pid, model, opt.get("duration"))[0]
    db.x("INSERT INTO usage(provider, kind, model, day, created, ok, ref, cost) VALUES(?,?,?,?,?,1,?,?)",
         (pid, "video", model, today(), time.time(), "take %d" % tid, cost))
    touch(shot["project_id"])
    log("提交 %s/%s take=%d task=%s" % (pid, model, tid, task_id))
    return tid


def auto_generate(shot, project, target, pref, allow_paid, use_image, exclude=(), retry=0):
    """智能模式：按「每日免费 → 试用额度 → 付费」依次尝试，失败自动换下一家"""
    mode = "i2v" if (use_image and shot["image"]) else "t2v"
    cands = RT.candidates(wallet, mode, project["aspect"], target, pref, allow_paid, exclude)
    if not cands and mode == "i2v":  # 没有能图生的免费平台，就退回文生
        mode, use_image = "t2v", False
        cands = RT.candidates(wallet, mode, project["aspect"], target, pref, allow_paid, exclude)
    tried = []
    for c in cands[:5]:
        opt = {"duration": c["duration"], "resolution": c["resolution"], "ratio": c["ratio"], "optimizer": False}
        try:
            tid = submit_take(shot, project, c["provider"], c["model"], opt, use_image,
                              {"auto": True, "target": target, "pref": pref, "allow_paid": allow_paid, "retry": retry, "kind": c["kind"]})
            return {"take": tid, "provider": c["provider_name"], "model": c["model_label"], "tried": tried}
        except Exception as e:
            msg = str(e)
            kind = RT.classify_error(msg)
            if kind in ("quota", "auth"):
                b = RT.budget(c["provider"], pcfg(c["provider"]))
                wallet.mark_exhausted(c["provider"], ("Key 有问题：" if kind == "auth" else "") + msg, b["free_kind"] in ("daily", "free"))
            tried.append("%s：%s" % (c["provider_name"], msg[:120]))
            log("自动模式 %s 失败，换下一家：%s" % (c["provider"], msg[:200]))
    if not cands:
        raise ApiError("没有可用的免费额度了：今天的每日额度已用完、试用额度也用完了。可以到「额度」页添加新平台，或开启「允许付费兜底」，也可以上传 App 里生成的片段")
    raise ApiError("所有平台都提交失败：\n" + "\n".join(tried))


@route("POST", "/api/shots/(\\d+)/generate")
def r_generate(req, sid):
    b = req.json()
    s = db.one("SELECT * FROM shots WHERE id=?", (int(sid),))
    if not s:
        raise ApiError("镜头不存在", 404)
    project = db.one("SELECT * FROM projects WHERE id=?", (s["project_id"],))
    if not (s["prompt"] or "").strip():
        raise ApiError("这个镜头还没有写提示词")
    pid, model = b.get("provider"), b.get("model")
    if pid == "auto":
        pf = prefs()
        return auto_generate(s, project, float(b.get("target") or 5), b.get("pref") or pf["pref"],
                             bool(b.get("allow_paid", pf["allow_paid"])), bool(b.get("use_image", True)))
    cfg = pcfg(pid)
    if pid not in P.PROVIDERS or not P.provider_public(pid, cfg)["ready"]:
        raise ApiError("请先在「模型设置」里配置这个平台的 Key")
    limit = int(cfg.get("daily_limit") or 0)
    if limit and usage_today(pid) >= limit and not b.get("force"):
        raise ApiError("LIMIT:今天已经用了 %d 条，达到你设置的每日上限 %d 条" % (usage_today(pid), limit), 409)
    opt = {k: b.get(k) for k in ("duration", "resolution", "ratio", "optimizer")}
    try:
        tid = submit_take(s, project, pid, model, opt, bool(b.get("use_image")))
    except Exception as e:
        raise ApiError(str(e))
    return {"take": tid}


@route("GET", "/api/shots/(\\d+)/route")
def r_route_preview(req, sid):
    s = db.one("SELECT * FROM shots WHERE id=?", (int(sid),))
    if not s:
        raise ApiError("不存在或已被删除", 404)
    project = db.one("SELECT * FROM projects WHERE id=?", (s["project_id"],))
    pf = prefs()
    use_image = req.query.get("use_image", "1") == "1" and bool(s["image"])
    target = float(req.query.get("target") or 5)
    pref = req.query.get("pref") or pf["pref"]
    c = RT.candidates(wallet, "i2v" if use_image else "t2v", project["aspect"], target, pref, pf["allow_paid"])
    if not c and use_image:
        c = RT.candidates(wallet, "t2v", project["aspect"], target, pref, pf["allow_paid"])
    return {"plan": [{k: v for k, v in x.items() if not k.startswith("_")} for x in c[:4]]}


@route("POST", "/api/projects/(\\d+)/generate_all")
def r_generate_all(req, pid):
    b = req.json()
    project = db.one("SELECT * FROM projects WHERE id=?", (int(pid),))
    if not project:
        raise ApiError("不存在或已被删除", 404)
    pf = prefs()
    done, skipped, errors = [], 0, []
    for s in db.q("SELECT * FROM shots WHERE project_id=? ORDER BY idx, id", (project["id"],)):
        has = db.one("SELECT COUNT(*) n FROM takes WHERE shot_id=? AND status IN ('running','queued','succeeded')", (s["id"],))["n"]
        if has or not (s["prompt"] or "").strip():
            skipped += 1
            continue
        try:
            r = auto_generate(s, project, float(b.get("target") or 5), b.get("pref") or pf["pref"], pf["allow_paid"], True)
            done.append("%s → %s" % (s["title"], r["provider"]))
        except ApiError as e:
            errors.append("%s：%s" % (s["title"], str(e)[:150]))
            if "没有可用的免费额度" in str(e):
                break
    return {"started": done, "skipped": skipped, "errors": errors}


# ---------------- 额度中心
@route("GET", "/api/quota")
def r_quota(req):
    rows = [wallet.status(pid) for pid in P.PROVIDERS]
    ready = [r for r in rows if r["ready"] and r["auto"]]
    today_est = sum(r["est_videos"] or 0 for r in ready if r["free_kind"] == "daily")
    unlimited = [r["name"] for r in ready if r["free_kind"] == "free" and not r["exhausted"]]
    trial_est = sum(r["est_videos"] or 0 for r in ready if r["free_kind"] == "trial")
    unknown = [r["name"] for r in ready if r["free_kind"] == "trial" and r["est_videos"] is None]
    return {"providers": rows, "today_free": today_est, "trial_free": trial_est, "unknown": unknown, "unlimited": unlimited, "prefs": prefs()}


@route("PUT", "/api/prefs")
def r_prefs(req):
    b = req.json()
    p = prefs()
    if "allow_paid" in b:
        p["allow_paid"] = bool(b["allow_paid"])
    if b.get("pref") in ("save", "quality"):
        p["pref"] = b["pref"]
    db.kv_set("prefs", p)
    return p


@route("POST", "/api/providers/(\\w+)/test")
def r_provider_test(req, pid):
    if pid not in P.PROVIDERS:
        raise ApiError("未知平台", 404)
    r = RT.test_provider(pid, pcfg(pid))
    if r["ok"] and (wallet.exhausted(pid) or {}).get("msg", "").startswith("Key 有问题"):
        wallet.clear_exhausted(pid)
    return r


@route("POST", "/api/providers/(\\w+)/reset")
def r_provider_reset(req, pid):
    wallet.clear_exhausted(pid)
    return {"ok": True}


@route("POST", "/api/shots/(\\d+)/upload")
def r_upload_clip(req, sid):
    s = db.one("SELECT * FROM shots WHERE id=?", (int(sid),))
    if not s:
        raise ApiError("镜头不存在", 404)
    name = req.query.get("name", "clip.mp4")
    ext = ext_of(name, ("mp4", "mov", "webm", "mkv", "m4v"), "mp4")
    tid = db.x("INSERT INTO takes(shot_id, source, model, params, prompt, status, created) VALUES(?,?,?,?,?,?,?)",
               (s["id"], "upload", os.path.basename(name)[:80], "{}", "", "uploading", time.time()))
    rel = "videos/take_%d.%s" % (tid, ext)
    try:
        save_upload(req, rel)
        R.probe(mpath(rel))
    except Exception as e:
        safe_unlink(rel)
        db.x("DELETE FROM takes WHERE id=?", (tid,))
        raise ApiError("上传失败或不是有效视频：%s" % e)
    finish_take_file(tid, rel)
    touch(s["project_id"])
    return {"take": tid}


# ---------------- takes
@route("PUT", "/api/takes/(\\d+)")
def r_take_save(req, tid):
    b = req.json()
    t = db.one("SELECT * FROM takes WHERE id=?", (int(tid),))
    if not t:
        raise ApiError("片段不存在", 404)
    start = max(0.0, float(b.get("start", t["start"]) or 0))
    end = max(0.0, float(b.get("end", t["end_t"]) or 0))
    speed = max(0.25, min(4.0, float(b.get("speed", t["speed"]) or 1)))
    db.x("UPDATE takes SET start=?, end_t=?, speed=? WHERE id=?", (start, end, speed, t["id"]))
    return {"ok": True}


@route("POST", "/api/takes/(\\d+)/select")
def r_take_select(req, tid):
    t = db.one("SELECT * FROM takes WHERE id=?", (int(tid),))
    if not t:
        raise ApiError("不存在或已被删除", 404)
    db.x("UPDATE shots SET selected_take=? WHERE id=?", (t["id"], t["shot_id"]))
    return {"ok": True}


@route("POST", "/api/takes/(\\d+)/refresh")
def r_take_refresh(req, tid):
    t = db.one("SELECT * FROM takes WHERE id=?", (int(tid),))
    if t and t["task_id"] and t["status"] in ("running", "queued", "failed"):
        if t["status"] == "failed":
            db.x("UPDATE takes SET status='running', errors=0 WHERE id=?", (t["id"],))
            t = db.one("SELECT * FROM takes WHERE id=?", (int(tid),))
            if not t:
                raise ApiError("不存在或已被删除", 404)
        poll_take(t)
    return db.one("SELECT * FROM takes WHERE id=?", (int(tid),))


@route("DELETE", "/api/takes/(\\d+)")
def r_take_del(req, tid):
    t = db.one("SELECT * FROM takes WHERE id=?", (int(tid),))
    if t:
        safe_unlink(t["file"]); safe_unlink(t["thumb"])
        db.x("DELETE FROM takes WHERE id=?", (t["id"],))
        db.x("UPDATE shots SET selected_take=NULL WHERE selected_take=?", (t["id"],))
    return {"ok": True}


# ---------------- music & render
@route("POST", "/api/projects/(\\d+)/music")
def r_music(req, pid):
    p = db.one("SELECT * FROM projects WHERE id=?", (int(pid),))
    if not p:
        raise ApiError("不存在或已被删除", 404)
    name = req.query.get("name", "music.mp3")
    ext = ext_of(name, ("mp3", "m4a", "aac", "wav", "ogg", "flac"), "mp3")
    rel = "music/project%d_%d.%s" % (p["id"], int(time.time()), ext)
    save_upload(req, rel, 100 * 1024 * 1024)
    try:
        R.probe(mpath(rel))
    except Exception:
        safe_unlink(rel)
        raise ApiError("不是有效的音频文件")
    safe_unlink(p["music"])
    db.x("UPDATE projects SET music=?, music_name=? WHERE id=?", (rel, os.path.basename(name)[:80], p["id"]))
    return {"ok": True}


@route("DELETE", "/api/projects/(\\d+)/music")
def r_music_del(req, pid):
    p = db.one("SELECT * FROM projects WHERE id=?", (int(pid),))
    if not p:
        raise ApiError("不存在或已被删除", 404)
    safe_unlink(p["music"])
    db.x("UPDATE projects SET music=NULL, music_name=NULL WHERE id=?", (p["id"],))
    return {"ok": True}


@route("POST", "/api/projects/(\\d+)/render")
def r_render(req, pid):
    if not shutil.which("ffmpeg"):
        raise ApiError("服务器没有安装 ffmpeg，无法合成（安装方法见部署说明）")
    p = project_full(int(pid))
    if not p:
        raise ApiError("项目不存在", 404)
    ok = [s for s in p["shots"] if any(t["id"] == s["selected_take"] and t["status"] == "succeeded" for t in s["takes"])]
    if not ok:
        raise ApiError("还没有可用片段：给镜头生成或上传视频，并点「用这条」选中")
    rid = db.x("INSERT INTO renders(project_id, status, msg, created) VALUES(?,?,?,?)", (p["id"], "queued", "排队中", time.time()))
    render_q.put(rid)
    return {"render": rid, "shots_used": len(ok), "shots_total": len(p["shots"])}


@route("DELETE", "/api/renders/(\\d+)")
def r_render_del(req, rid):
    r = db.one("SELECT * FROM renders WHERE id=?", (int(rid),))
    if r and r["status"] not in ("queued", "running"):
        safe_unlink(r["file"])
        db.x("DELETE FROM renders WHERE id=?", (r["id"],))
    return {"ok": True}


# ---------------- handler
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "application/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ClipForge"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass

    def clen(self):
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def authed(self):
        if not PASSWORD:
            return True
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        v = c.get("cf_session")
        return bool(v) and hmac.compare_digest(v.value, SESSION)

    def send_bytes(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, code, obj, extra=None):
        self.send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                        dict({"Cache-Control": "no-store"}, **(extra or {})))

    def serve_media(self, rel):
        path = mpath(urllib.parse.unquote(rel))
        if not path or not os.path.isfile(path):
            return self.send_bytes(404, b"not found", "text/plain")
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1)); end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416); self.send_header("Content-Range", "bytes */%d" % size); self.send_header("Content-Length", "0"); self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=3600")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if q.get("dl"):
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % urllib.parse.quote(os.path.basename(path)))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(left, 256 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def handle_any(self, method):
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        self.set_cookie = None
        if method in ("GET", "HEAD") and path in STATIC:
            fn, ct = STATIC[path]
            with open(os.path.join(HERE, "static", fn), "rb") as f:
                return self.send_bytes(200, f.read(), ct, {"Cache-Control": "no-cache"})
        if path.startswith("/media/"):
            if not self.authed():
                return self.send_bytes(401, b"login required", "text/plain")
            return self.serve_media(path[len("/media/"):])
        if not path.startswith("/api/"):
            return self.send_bytes(404, b"not found", "text/plain")
        holder = {"body": None}

        def reader():
            if holder["body"] is None:
                n = self.clen()
                if n > 5 * 1024 * 1024:
                    raise ApiError("请求太大")
                holder["body"] = self.rfile.read(n) if n else b""
            return holder["body"]
        req = Req(self, reader, query)
        req.consumed = False
        try:
            for m, rx, fn in ROUTES:
                mm = rx.match(path)
                if mm and m == method:
                    if fn not in (r_ping, r_login) and not self.authed():
                        raise ApiError("请先登录", 401)
                    out = fn(req, *mm.groups())
                    break
            else:
                raise ApiError("接口不存在", 404)
            code = 200
        except ApiError as e:
            out, code = {"error": str(e)}, e.code
        except Exception as e:
            traceback.print_exc()
            out, code = {"error": "服务器内部错误：%s" % e}, 500
        # 未读完的请求体要读掉，避免 keep-alive 连接错乱
        n = self.clen()
        if n and holder["body"] is None and not req.consumed:
            try:
                left = n
                while left > 0:
                    c = self.rfile.read(min(left, 65536))
                    if not c:
                        break
                    left -= len(c)
            except Exception:
                pass
        self.send_json(code, out, {"Set-Cookie": self.set_cookie} if self.set_cookie else None)

    def do_GET(self):
        self.handle_any("GET")

    def do_HEAD(self):
        self.handle_any("HEAD")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_DELETE(self):
        self.handle_any("DELETE")


def main():
    if not IS_LOCAL and not PASSWORD:
        sys.exit("错误：监听 %s 时必须设置登录密码，例如：CF_PASSWORD=你的密码 python3 app.py" % BIND)
    if not shutil.which("ffmpeg"):
        log("⚠️ 没找到 ffmpeg：可以生成视频，但「合成成片」不可用。Ubuntu/Debian: apt install ffmpeg")
    # 重启后把未完成的合成任务标记为失败
    db.x("UPDATE renders SET status='failed', error='服务重启，任务中断，请重新合成' WHERE status IN ('queued','running')")
    db.x("UPDATE takes SET status='failed', error='上传中断' WHERE status='uploading'")
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=render_worker, daemon=True).start()
    srv = http.server.ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    log("拼影工坊已启动：http://%s:%d   数据目录：%s   登录密码：%s" % ("localhost" if IS_LOCAL else BIND, PORT, DATA, "已设置" if PASSWORD else "未设置（仅本机）"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
