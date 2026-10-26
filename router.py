"""智能路由：把多家平台的免费额度当成一个"钱包"来用。

规则（按顺序花钱）：
  1. 每日免费（今天不用就浪费）：MiniMax Token Plan、Hugging Face……
  2. 试用额度（会过期的新人额度/试用金）：Google $300、阿里百炼、火山方舟、Replicate……
  3. 付费（只有用户允许"付费兜底"时才用）
同一档里再按「画质 / 画面比例是否匹配 / 花费」排序。提交失败（额度用完、Key 失效、参数不支持）自动换下一家。
"""
import hashlib, json, re, time
from datetime import datetime

import providers as P

# ---------------------------------------------------------------- 模型画质与成本估算
QUALITY = {
    "MiniMax-Hailuo-2.3": 4.5, "MiniMax-Hailuo-2.3-Fast": 4.0, "MiniMax-Hailuo-02": 4.0,
    "doubao-seedance-1-0-pro-250528": 4.5, "doubao-seedance-1-0-lite-t2v-250428": 3.5, "doubao-seedance-1-0-lite-i2v-250428": 3.5,
    "wan2.5-t2v-preview": 4.0, "wan2.2-t2v-plus": 3.5, "wan2.6-i2v-flash": 4.0, "wan2.5-i2v-preview": 4.0,
    "kling-v2-5-turbo": 4.5, "kling-v2-1": 4.0, "kling-v1-6": 3.5, "viduq2": 4.0, "viduq1": 3.5,
    "veo-3.1-fast-generate-001": 4.5, "veo-3.1-fast-generate-001+audio": 4.6, "veo-3.1-generate-001+audio": 5.0, "veo-3.0-fast-generate-001": 4.0,
    "zerogpu-aoti/wan2-2-fp8da-aoti-faster": 3.2, "Lightricks/ltx-video-distilled": 2.8,
    "cogvideox-flash": 3.0, "alibaba/wan-2.6:free": 4.0,
    "minimax/video-01": 3.5, "minimax/hailuo-02": 4.0, "kwaivgi/kling-v2.1": 4.0, "bytedance/seedance-1-lite": 3.5, "google/veo-3-fast": 4.5,
}
# 按秒计费的模型（美元/秒，估算值，以平台账单为准）
USD_PER_SEC = {"veo-3.1-fast-generate-001": 0.10, "veo-3.1-fast-generate-001+audio": 0.15, "veo-3.1-generate-001+audio": 0.40,
               "veo-3.0-fast-generate-001": 0.10}
USD_PER_IMAGE = {"imagen-4.0-fast-generate-001": 0.02, "imagen-4.0-generate-001": 0.04}

# 各平台默认额度类型：daily 每日免费 / trial 试用额度 / paid 付费
DEFAULT_BUDGET = {
    "minimax": {"free_kind": "daily", "unit": "count", "amount": 3},
    "hf": {"free_kind": "daily", "unit": "count", "amount": 3},
    "google": {"free_kind": "trial", "unit": "usd", "amount": 300},
    "bailian": {"free_kind": "trial", "unit": "count", "amount": 0},
    "ark": {"free_kind": "trial", "unit": "count", "amount": 0},
    "replicate": {"free_kind": "trial", "unit": "count", "amount": 0},
    "vidu": {"free_kind": "trial", "unit": "count", "amount": 0},
    "kling": {"free_kind": "paid", "unit": "count", "amount": 0},
    "zhipu": {"free_kind": "free", "unit": "count", "amount": 0},
    "openrouter": {"free_kind": "free", "unit": "count", "amount": 0},
}
# 顺序：每日免费（今天不用就浪费）→ 永久免费（不限量但画质一般）→ 试用额度（会过期）→ 付费
KIND_RANK = {"daily": 0, "free": 1, "trial": 2, "paid": 3}
KIND_LABEL = {"daily": "每日免费", "free": "永久免费", "trial": "试用额度", "paid": "付费"}
# 海外模型用英文提示词效果更好
EN_PREFER = {"google", "hf", "replicate", "openrouter"}


def today():
    return datetime.now().strftime("%Y-%m-%d")


def budget(pid, cfg):
    b = dict(DEFAULT_BUDGET.get(pid, {"free_kind": "trial", "unit": "count", "amount": 0}))
    for k in ("free_kind", "unit"):
        if cfg.get(k):
            b[k] = cfg[k]
    if cfg.get("amount") not in (None, ""):
        b["amount"] = float(cfg["amount"])
    b["since"] = float(cfg.get("since") or 0)
    b["auto"] = cfg.get("auto", True) is not False
    return b


def cost_of(pid, model, seconds, kind="video"):
    """返回 (数量, 单位)；按条计的平台 1 条=1"""
    if kind == "image":
        return (USD_PER_IMAGE.get(model, 0.03), "usd") if pid == "google" else (1, "count")
    if model in USD_PER_SEC:
        return round(USD_PER_SEC[model] * float(seconds or 8), 3), "usd"
    return 1, "count"


class Wallet:
    """额度计算（依赖 app 里的 db）"""

    def __init__(self, db, pcfg):
        self.db, self.pcfg = db, pcfg

    def used(self, pid, b):
        if b["unit"] == "usd":
            col, kinds = "COALESCE(cost, 0)", ("video", "image")
        else:
            col, kinds = "1", ("video",)
        sql = "SELECT COALESCE(SUM(%s),0) s FROM usage WHERE provider=? AND ok=1 AND kind IN (%s)" % (col, ",".join("?" * len(kinds)))
        args = [pid] + list(kinds)
        if b["free_kind"] == "daily":
            sql += " AND day=?"; args.append(today())
        elif b["since"]:
            sql += " AND created>=?"; args.append(b["since"])
        return float(self.db.one(sql, args)["s"])

    def exhausted(self, pid):
        e = self.db.kv_get("exhausted:" + pid)
        if not e:
            return None
        if e.get("day") and e["day"] != today():  # 每日额度，第二天自动恢复
            self.db.kv_set("exhausted:" + pid, None)
            return None
        return e

    def mark_exhausted(self, pid, msg, daily):
        """daily=True：明天自动恢复（每日额度、免费模型的频率限制）"""
        self.db.kv_set("exhausted:" + pid, {"day": today() if daily else None, "msg": msg[:200], "at": time.time()})

    def clear_exhausted(self, pid):
        self.db.kv_set("exhausted:" + pid, None)

    def status(self, pid):
        cfg = self.pcfg(pid)
        pub = P.provider_public(pid, cfg)
        b = budget(pid, cfg)
        used = self.used(pid, b)
        remaining = (b["amount"] - used) if b["amount"] else None
        ex = self.exhausted(pid)
        est = None
        if remaining is not None:
            if b["unit"] == "count":
                est = max(0, int(remaining))
            else:
                rates = [USD_PER_SEC[m["id"]] for m in pub["video_models"] if m["id"] in USD_PER_SEC]
                est = max(0, int(remaining // ((min(rates) if rates else 0.1) * 6)))
        if ex:
            est = 0
        return {"id": pid, "name": pub["name"], "group": pub["group"], "ready": pub["ready"], "home": pub["home"], "free": pub["free"],
                "free_kind": b["free_kind"], "kind_label": KIND_LABEL.get(b["free_kind"], ""), "unit": b["unit"], "amount": b["amount"],
                "used": round(used, 2), "remaining": None if remaining is None else round(max(0, remaining), 2),
                "est_videos": est, "exhausted": ex, "auto": b["auto"]}

    def available(self, pid, b, cost):
        if self.exhausted(pid):
            return False
        if b["amount"]:
            return self.used(pid, b) + cost <= b["amount"] + 1e-9
        return True


# ---------------------------------------------------------------- 路由
def _pick_duration(durs, target):
    ge = [d for d in durs if d >= target]
    return min(ge) if ge else max(durs)


def _pick_res(res, pref):
    if pref == "quality":
        for want in ("1080p", "1080P"):
            if want in res:
                return want
    return next((r for r in res if re.match(r"^(720p|768p)$", r, re.I)), res[0])


def candidates(wallet, mode, aspect, target, pref="save", allow_paid=False, exclude=()):
    out = []
    for pid in P.PROVIDERS:
        if pid in exclude:
            continue
        cfg = wallet.pcfg(pid)
        pub = P.provider_public(pid, cfg)
        if not pub["ready"]:
            continue
        b = budget(pid, cfg)
        if not b["auto"] or (b["free_kind"] == "paid" and not allow_paid):
            continue
        if wallet.exhausted(pid):
            continue
        for m in pub["video_models"]:
            if mode not in m["modes"]:
                continue
            dur = _pick_duration(m["durations"], target)
            if pid == "minimax" and b["unit"] == "count":
                dur = max(m["durations"])  # 按条计费，选最长的更划算（多出来的合成时自动裁掉）
            res = _pick_res(m["resolutions"], pref)
            if pid == "minimax" and res.upper() == "1080P" and dur == 10:
                res = "768P"
            ratio, aspect_pen = None, 0
            if m["ratios"]:
                if mode == "i2v" and "adaptive" in m["ratios"]:
                    ratio = "adaptive"
                elif aspect in m["ratios"]:
                    ratio = aspect
                else:
                    ratio, aspect_pen = m["ratios"][0], 1
            elif mode == "t2v" and aspect != "16:9":
                aspect_pen = 1  # 文生固定横屏，竖屏项目只能模糊填充
            cost, unit = cost_of(pid, m["id"], dur)
            if not wallet.available(pid, b, cost):
                continue
            q = QUALITY.get(m["id"], 3.0)
            tier = KIND_RANK.get(b["free_kind"], 1)
            # 同一家里：省额度选最便宜的，画质优先选最好的
            inner = (aspect_pen, cost, -q) if pref == "save" else (aspect_pen, -q, cost)
            outer = (tier, aspect_pen, -q) if pref == "save" else (aspect_pen, 1 if tier == 3 else 0, -q, tier)
            out.append({"provider": pid, "provider_name": pub["name"], "model": m["id"], "model_label": m["label"], "duration": dur,
                        "resolution": res, "ratio": ratio, "cost": cost, "unit": unit, "kind": b["free_kind"], "quality": q,
                        "_inner": inner, "_key": outer})
    # 每家只保留一个模型，失败时换的是"另一家"
    best = {}
    for c in out:
        if c["provider"] not in best or c["_inner"] < best[c["provider"]]["_inner"]:
            best[c["provider"]] = c
    res_list = sorted(best.values(), key=lambda c: c["_key"])
    for c in res_list:
        c.pop("_inner", None)
    return res_list


QUOTA_WORDS = ("额度", "余额", "quota", "insufficient", "balance", "exceed", "limit", "1008", "1039", "402", "429", "欠费", "arrearage", "上限")
AUTH_WORDS = ("Key 无效", "1004", "401", "403", "unauth", "invalid api", "InvalidApiKey", "登录失败", "PERMISSION", "权限")


def classify_error(msg):
    m = msg.lower()
    if any(w.lower() in m for w in AUTH_WORDS):
        return "auth"
    if any(w.lower() in m for w in QUOTA_WORDS):
        return "quota"
    return "other"


# ---------------------------------------------------------------- 提示词适配
CAM_ZH = {"push in": "镜头缓慢推进", "pull out": "镜头缓慢拉远", "truck left": "镜头向左平移", "truck right": "镜头向右平移",
          "pan left": "镜头向左摇", "pan right": "镜头向右摇", "pedestal up": "镜头升高", "pedestal down": "镜头降低",
          "tilt up": "镜头上仰", "tilt down": "镜头下俯", "zoom in": "镜头放大", "zoom out": "镜头缩小", "tracking shot": "跟随镜头",
          "shake": "镜头震动", "static shot": "固定镜头"}


def adapt_camera(prompt, pid):
    if pid == "minimax":
        return prompt

    def rep(m):
        parts = [CAM_ZH.get(x.strip().lower()) for x in m.group(1).split(",")]
        parts = [x for x in parts if x]
        return ("，".join(parts) + "，") if parts else ""
    return re.sub(r"\[([A-Za-z ,]+)\]\s*", rep, prompt)


def compose_prompt(project, shot, pid):
    pre = [x.strip().rstrip("。.") for x in ((project.get("style") or ""), (project.get("character") or "")) if x and x.strip()]
    body = shot.get("prompt") or ""
    pre = [x for x in pre if x[:20] not in body]  # 已经写进去了就不重复
    text = "。".join(pre + [body]) if pre else body
    return adapt_camera(text, pid)


def _strip_think(s):
    return re.sub(r"<think>.*?</think>", "", s or "", flags=re.S).strip()


def translate(db, pcfg, text):
    """借用已配置的 MiniMax（Token Plan 含文本模型）把提示词翻成英文；失败就返回 None"""
    if not re.search(r"[一-鿿]", text):
        return text
    key = "tr:" + hashlib.sha1(text.encode()).hexdigest()
    hit = db.kv_get(key)
    if hit:
        return hit
    cfg = pcfg("minimax")
    if not cfg.get("api_key"):
        return None
    model = cfg.get("text_model") or "MiniMax-M2.7"
    msgs = [{"role": "system", "content": "You translate video-generation prompts from Chinese to English. Keep every visual detail, "
                                          "camera movement and style word. Output only the English prompt, no explanations."},
            {"role": "user", "content": text}]
    try:
        st, js = P.http_json("POST", P.host("minimax", cfg) + "/v1/chat/completions", {"Authorization": "Bearer " + cfg["api_key"]},
                             {"model": model, "messages": msgs, "temperature": 0.2, "max_tokens": 2000}, timeout=90)
        out = _strip_think(((js.get("choices") or [{}])[0].get("message") or {}).get("content"))
        if st < 400 and out and not re.search(r"[一-鿿]{6,}", out):
            db.kv_set(key, out)
            return out
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 测试连接（不消耗额度）
def test_provider(pid, cfg):
    h = P.host
    try:
        if pid == "minimax":
            st, js = P.http_json("GET", h(pid, cfg) + "/v1/query/video_generation?task_id=0", {"Authorization": "Bearer " + cfg.get("api_key", "")})
            code = (js.get("base_resp") or {}).get("status_code")
            ok = st < 401 and code != 1004
        elif pid == "ark":
            st, js = P.http_json("GET", h(pid, cfg) + "/contents/generations/tasks?page_num=1&page_size=1", {"Authorization": "Bearer " + cfg.get("api_key", "")})
            ok = st not in (401, 403)
        elif pid == "bailian":
            st, js = P.http_json("GET", h(pid, cfg) + "/tasks/00000000-0000-0000-0000-000000000000", {"Authorization": "Bearer " + cfg.get("api_key", "")})
            ok = st not in (401, 403) and "InvalidApiKey" not in json.dumps(js)
        elif pid == "kling":
            st, js = P.http_json("GET", h(pid, cfg) + "/v1/videos/text2video?pageNum=1&pageSize=1", P._kl_headers(cfg))
            ok = st not in (401, 403) and js.get("code", 0) not in (1000, 1001, 1002, 1003, 1004)
        elif pid == "vidu":
            st, js = P.http_json("GET", h(pid, cfg) + "/tasks/0/creations", P._vd_headers(cfg))
            ok = st not in (401, 403)
        elif pid == "google":
            P.google_token(cfg); P._gproj(cfg)
            st, js, ok = 200, {}, True
        elif pid == "hf":
            import os
            url = (os.environ["CF_MOCK"] + "/hfapi/whoami-v2") if os.environ.get("CF_MOCK") else "https://huggingface.co/api/whoami-v2"
            st, js = P.http_json("GET", url, P._hf_headers(cfg))
            ok = st == 200
        elif pid == "replicate":
            st, js = P.http_json("GET", h(pid, cfg) + "/account", P._rp_headers(cfg))
            ok = st == 200
        else:
            return {"ok": False, "msg": "未知平台"}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:300]}
    return {"ok": ok, "msg": "连接正常，Key 有效（未消耗额度）" if ok else "Key 无效或没有权限（HTTP %s）：%s" % (st, P.short(js, 200))}
