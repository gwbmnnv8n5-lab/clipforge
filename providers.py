"""视频/图片模型适配器。

每个平台实现 submit()/query()，统一返回：
    submit -> task_id (str)
    query  -> {"status": "running"|"succeeded"|"failed", "url": str|None, "error": str|None, "headers": {}}
新增平台：在 PROVIDERS 里加一项，并实现对应的 _xxx_submit/_xxx_query。
"""
import base64, hashlib, hmac, json, mimetypes, os, subprocess, tempfile, threading, time, uuid, urllib.error, urllib.parse, urllib.request


class ProviderError(Exception):
    pass


# ---------------------------------------------------------------- http helpers
def http_json(method, url, headers=None, body=None, timeout=90):
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        status = e.code
    except Exception as e:
        raise ProviderError("网络请求失败：%s" % e)
    try:
        js = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        js = {"_raw": raw[:500].decode("utf-8", "replace")}
    return status, js


def short(obj, n=400):
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return s[:n]


def image_data_uri(path):
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        return "data:%s;base64,%s" % (mime, base64.b64encode(f.read()).decode("ascii"))


def image_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def ratio_size(ratio, long_side):
    """把比例和长边换算成宽高，例如 9:16 + 1280 -> (720, 1280)"""
    a, b = [int(x) for x in ratio.split(":")]
    if a >= b:
        w, h = long_side, round(long_side * b / a)
    else:
        h, w = long_side, round(long_side * a / b)
    return w - w % 2, h - h % 2


# ---------------------------------------------------------------- registry
# 每个模型：id, label, modes(t2v/i2v), durations, resolutions, ratios(None=不支持设置比例)
PROVIDERS = {
    "minimax": {
        "name": "MiniMax 海螺",
        "home": "https://platform.minimaxi.com",
        "free": "Token Plan 订阅含视频额度；开放平台新用户可能有体验金",
        "fields": [{"k": "api_key", "label": "API Key（Token Plan 订阅 Key 或按量 Key）", "secret": True}],
        "regions": {"cn": "国内 api.minimaxi.com", "intl": "国际 api.minimax.io"},
        "hosts": {"cn": "https://api.minimaxi.com", "intl": "https://api.minimax.io"},
        "video_models": [
            {"id": "MiniMax-Hailuo-2.3", "label": "Hailuo 2.3", "modes": ["t2v", "i2v"], "durations": [6, 10], "resolutions": ["768P", "1080P"], "ratios": None,
             "note": "1080P 只支持 6 秒；文生视频固定横屏，竖屏请加竖版首帧图"},
            {"id": "MiniMax-Hailuo-2.3-Fast", "label": "Hailuo 2.3 Fast", "modes": ["i2v"], "durations": [6, 10], "resolutions": ["768P", "1080P"], "ratios": None,
             "note": "仅图生视频，速度快、更省"},
            {"id": "MiniMax-Hailuo-02", "label": "Hailuo 02", "modes": ["t2v", "i2v"], "durations": [6, 10], "resolutions": ["768P", "1080P"], "ratios": None,
             "note": "1080P 只支持 6 秒"},
        ],
        "image_models": [{"id": "image-01", "label": "image-01", "ratios": ["9:16", "16:9", "1:1", "3:4", "4:3", "2:3", "3:2", "21:9"]}],
    },
    "ark": {
        "name": "火山方舟 Seedance（即梦同源）",
        "home": "https://console.volcengine.com/ark",
        "free": "新用户通常每个模型有免费 tokens，以控制台为准；需在控制台「开通管理」开通模型",
        "fields": [{"k": "api_key", "label": "方舟 API Key", "secret": True}],
        "regions": {"cn": "国内 火山方舟", "intl": "海外 BytePlus ModelArk"},
        "hosts": {"cn": "https://ark.cn-beijing.volces.com/api/v3", "intl": "https://ark.ap-southeast.bytepluses.com/api/v3"},
        "video_models": [
            {"id": "doubao-seedance-1-0-pro-250528", "label": "Seedance 1.0 Pro", "modes": ["t2v", "i2v"], "durations": [5, 10], "resolutions": ["480p", "720p", "1080p"],
             "ratios": ["9:16", "16:9", "1:1", "3:4", "4:3", "21:9"]},
            {"id": "doubao-seedance-1-0-lite-t2v-250428", "label": "Seedance 1.0 Lite 文生", "modes": ["t2v"], "durations": [5, 10], "resolutions": ["480p", "720p", "1080p"],
             "ratios": ["9:16", "16:9", "1:1", "3:4", "4:3", "21:9"]},
            {"id": "doubao-seedance-1-0-lite-i2v-250428", "label": "Seedance 1.0 Lite 图生", "modes": ["i2v"], "durations": [5, 10], "resolutions": ["480p", "720p", "1080p"],
             "ratios": ["adaptive", "9:16", "16:9", "1:1"]},
        ],
        "image_models": [{"id": "doubao-seedream-3-0-t2i-250415", "label": "Seedream 3.0", "ratios": ["9:16", "16:9", "1:1", "3:4", "4:3"]}],
        "model_hint": "方舟模型 ID 带日期，新模型（如 Seedance 2.0）请从控制台复制 ID 填到「额外模型」",
    },
    "bailian": {
        "name": "阿里百炼 通义万相",
        "home": "https://bailian.console.aliyun.com",
        "free": "新人每个模型有免费额度（北京地域，90 天有效），以控制台为准",
        "fields": [{"k": "api_key", "label": "百炼 API Key（sk-开头）", "secret": True}],
        "regions": {"cn": "国内 北京", "intl": "国际 新加坡"},
        "hosts": {"cn": "https://dashscope.aliyuncs.com/api/v1", "intl": "https://dashscope-intl.aliyuncs.com/api/v1"},
        "video_models": [
            {"id": "wan2.5-t2v-preview", "label": "万相 2.5 文生", "modes": ["t2v"], "durations": [5, 10], "resolutions": ["480P", "720P", "1080P"], "ratios": ["9:16", "16:9", "1:1", "3:4", "4:3"]},
            {"id": "wan2.2-t2v-plus", "label": "万相 2.2 文生", "modes": ["t2v"], "durations": [5], "resolutions": ["480P", "1080P"], "ratios": ["9:16", "16:9", "1:1", "3:4", "4:3"]},
            {"id": "wan2.6-i2v-flash", "label": "万相 2.6 图生 Flash", "modes": ["i2v"], "durations": [5, 10], "resolutions": ["720P", "1080P"], "ratios": None},
            {"id": "wan2.5-i2v-preview", "label": "万相 2.5 图生", "modes": ["i2v"], "durations": [5, 10], "resolutions": ["480P", "720P", "1080P"], "ratios": None},
        ],
        "image_models": [],
        "model_hint": "图生视频的比例跟随首帧图",
    },
    "kling": {
        "name": "可灵 Kling",
        "home": "https://app.klingai.com/cn/dev",
        "free": "开放平台一般需购买资源包；新用户偶有体验额度",
        "fields": [{"k": "access_key", "label": "Access Key", "secret": False}, {"k": "secret_key", "label": "Secret Key", "secret": True}],
        "regions": {"cn": "国内 api-beijing", "intl": "海外 api-singapore"},
        "hosts": {"cn": "https://api-beijing.klingai.com", "intl": "https://api-singapore.klingai.com"},
        "video_models": [
            {"id": "kling-v2-5-turbo", "label": "可灵 2.5 Turbo", "modes": ["t2v", "i2v"], "durations": [5, 10], "resolutions": ["std", "pro"], "ratios": ["9:16", "16:9", "1:1"],
             "note": "清晰度 std=标准，pro=高品质（更贵）"},
            {"id": "kling-v2-1", "label": "可灵 2.1", "modes": ["i2v"], "durations": [5, 10], "resolutions": ["std", "pro"], "ratios": None},
            {"id": "kling-v1-6", "label": "可灵 1.6", "modes": ["t2v", "i2v"], "durations": [5, 10], "resolutions": ["std", "pro"], "ratios": ["9:16", "16:9", "1:1"]},
        ],
        "image_models": [],
    },
    "vidu": {
        "name": "Vidu 生数",
        "home": "https://platform.vidu.cn",
        "free": "开放平台新用户可能赠送积分，以控制台为准",
        "fields": [{"k": "api_key", "label": "API Key", "secret": True}],
        "regions": {"cn": "国内 api.vidu.cn", "intl": "海外 api.vidu.com"},
        "hosts": {"cn": "https://api.vidu.cn/ent/v2", "intl": "https://api.vidu.com/ent/v2"},
        "video_models": [
            {"id": "viduq2", "label": "Vidu Q2", "modes": ["t2v", "i2v"], "durations": [4, 5, 8], "resolutions": ["720p", "1080p"], "ratios": ["9:16", "16:9", "1:1"]},
            {"id": "viduq1", "label": "Vidu Q1", "modes": ["t2v", "i2v"], "durations": [5], "resolutions": ["1080p"], "ratios": ["9:16", "16:9", "1:1"]},
        ],
        "image_models": [],
    },
    "zhipu": {
        "name": "智谱 CogVideoX-Flash（免费）",
        "home": "https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
        "free": "官方免费模型，不限次数（高峰期排队较慢，画质一般）；在 bigmodel.cn 注册即可拿 Key",
        "fields": [{"k": "api_key", "label": "智谱 API Key", "secret": True}],
        "regions": {"cn": "国内 open.bigmodel.cn"},
        "hosts": {"cn": "https://open.bigmodel.cn/api/paas/v4"},
        "video_models": [
            {"id": "cogvideox-flash", "label": "CogVideoX-Flash（免费）", "modes": ["t2v", "i2v"], "durations": [5, 10], "resolutions": ["720p", "1080p"],
             "ratios": ["9:16", "16:9", "1:1"], "note": "免费模型，画质比付费模型差一些，适合空镜、过场"},
        ],
        "image_models": [{"id": "cogview-3-flash", "label": "CogView-3-Flash（免费）", "ratios": ["9:16", "16:9", "1:1", "3:4", "4:3"]}],
        "model_hint": "海外的 z.ai 版本不免费，请用国内 bigmodel.cn 的 Key",
    },
    # ------------------------------------------------ 海外
    "openrouter": {
        "name": "OpenRouter（含免费模型）",
        "group": "global",
        "home": "https://openrouter.ai/settings/keys",
        "free": "带 :free 后缀的模型免费（如 Wan 2.6，有频率限制）；同一个 Key 也能付费调用 Veo、Sora 等",
        "fields": [{"k": "api_key", "label": "OpenRouter API Key（sk-or- 开头）", "secret": True}],
        "regions": {"default": "默认"},
        "hosts": {"default": "https://openrouter.ai/api/v1"},
        "video_models": [
            {"id": "alibaba/wan-2.6:free", "label": "Wan 2.6（免费）", "modes": ["t2v", "i2v"], "durations": [5, 10], "resolutions": ["720p", "1080p"],
             "ratios": ["9:16", "16:9", "1:1"], "note": "免费模型有频率限制，超限会自动换别家"},
        ],
        "image_models": [],
        "model_hint": "openrouter.ai/collections/video-models 里的模型 ID 都可以填到「额外模型」（不带 :free 的要付费）",
    },
    "google": {
        "name": "Google Veo（Vertex AI）",
        "group": "global",
        "home": "https://console.cloud.google.com/vertex-ai",
        "free": "Google Cloud 新用户 $300 试用金（90 天，需绑卡验证，不会自动扣费），可用于 Vertex AI 上的 Veo 和 Imagen",
        "fields": [{"k": "service_account", "label": "服务账号 JSON 密钥（整个文件内容粘贴进来）", "secret": True, "multiline": True},
                   {"k": "project_id", "label": "项目 ID（留空则从 JSON 读取）", "secret": False, "optional": True}],
        "regions": {"us-central1": "us-central1（推荐）", "us-east4": "us-east4", "europe-west4": "europe-west4", "asia-northeast1": "asia-northeast1"},
        "hosts": {},
        "video_models": [
            {"id": "veo-3.1-fast-generate-001", "label": "Veo 3.1 Fast 无声（最省）", "modes": ["t2v", "i2v"], "durations": [4, 6, 8], "resolutions": ["720p", "1080p"], "ratios": ["9:16", "16:9"],
             "note": "按秒计费，用 $300 试用金；无声版更便宜，配乐在合成时加"},
            {"id": "veo-3.1-fast-generate-001+audio", "label": "Veo 3.1 Fast 带音效", "modes": ["t2v", "i2v"], "durations": [4, 6, 8], "resolutions": ["720p", "1080p"], "ratios": ["9:16", "16:9"],
             "note": "Veo 会同时生成音效/环境声"},
            {"id": "veo-3.1-generate-001+audio", "label": "Veo 3.1 标准 带音效（最贵最好）", "modes": ["t2v", "i2v"], "durations": [4, 6, 8], "resolutions": ["720p", "1080p"], "ratios": ["9:16", "16:9"]},
            {"id": "veo-3.0-fast-generate-001", "label": "Veo 3 Fast 无声", "modes": ["t2v", "i2v"], "durations": [8], "resolutions": ["720p", "1080p"], "ratios": ["9:16", "16:9"]},
        ],
        "image_models": [{"id": "imagen-4.0-fast-generate-001", "label": "Imagen 4 Fast（几分钱一张）", "ratios": ["9:16", "16:9", "1:1", "3:4", "4:3"]},
                         {"id": "imagen-4.0-generate-001", "label": "Imagen 4", "ratios": ["9:16", "16:9", "1:1", "3:4", "4:3"]}],
        "model_hint": "需在 Google Cloud 开通 Vertex AI API，并创建服务账号（角色：Vertex AI User）下载 JSON 密钥",
    },
    "hf": {
        "name": "Hugging Face 免费 GPU（ZeroGPU）",
        "group": "global",
        "home": "https://huggingface.co/settings/tokens",
        "free": "完全免费：免费账号每天有几分钟 ZeroGPU 时长（PRO 会员更多），大约够生成几条短视频；不需要绑卡",
        "fields": [{"k": "hf_token", "label": "Access Token（Read 权限即可）", "secret": True}],
        "regions": {"default": "默认"},
        "hosts": {},
        "video_models": [
            {"id": "zerogpu-aoti/wan2-2-fp8da-aoti-faster", "label": "Wan 2.2 加速版（图生视频）", "modes": ["i2v"], "durations": [3, 5], "resolutions": ["默认"], "ratios": None,
             "note": "社区 Space，免费但可能排队；每天免费 GPU 时长用完会报 quota 错误，第二天恢复"},
            {"id": "Lightricks/ltx-video-distilled", "label": "LTX-Video（快）", "modes": ["t2v", "i2v"], "durations": [3, 5], "resolutions": ["默认"], "ratios": None,
             "note": "社区 Space，免费但可能排队；参数会自动识别"},
        ],
        "image_models": [],
        "model_hint": "可以把任何视频类 Gradio Space 的 ID（如 owner/space-name）填到「额外模型」，平台会自动识别它的参数。Space 由社区维护，可能下线或改版",
    },
    "replicate": {
        "name": "Replicate（模型超市）",
        "group": "global",
        "home": "https://replicate.com/account/api-tokens",
        "free": "新账号可免费试跑部分模型（如 minimax/video-01，次数有限）；之后按次付费，一个 Key 用遍 Veo/可灵/海螺/Wan 等",
        "fields": [{"k": "api_token", "label": "API Token（r8_ 开头）", "secret": True}],
        "regions": {"default": "默认"},
        "hosts": {"default": "https://api.replicate.com/v1"},
        "video_models": [
            {"id": "minimax/video-01", "label": "MiniMax video-01（有免费次数）", "modes": ["t2v", "i2v"], "durations": [6], "resolutions": ["默认"], "ratios": None},
            {"id": "minimax/hailuo-02", "label": "海螺 02", "modes": ["t2v", "i2v"], "durations": [6, 10], "resolutions": ["768p", "1080p"], "ratios": None},
            {"id": "kwaivgi/kling-v2.1", "label": "可灵 2.1（图生）", "modes": ["i2v"], "durations": [5, 10], "resolutions": ["默认"], "ratios": None},
            {"id": "bytedance/seedance-1-lite", "label": "Seedance 1 Lite", "modes": ["t2v", "i2v"], "durations": [5, 10], "resolutions": ["480p", "720p", "1080p"], "ratios": ["9:16", "16:9", "1:1"]},
            {"id": "google/veo-3-fast", "label": "Veo 3 Fast", "modes": ["t2v", "i2v"], "durations": [8], "resolutions": ["720p", "1080p"], "ratios": ["9:16", "16:9"]},
        ],
        "image_models": [],
        "model_hint": "replicate.com 上任意视频模型都能用：把「owner/model」填到额外模型，参数自动识别",
    },
}


def provider_public(pid, cfg):
    """给前端用的平台信息（不含密钥）"""
    p = PROVIDERS[pid]
    gateway = pid in ("hf", "replicate", "openrouter")
    models = list(p["video_models"])
    known = {m["id"] for m in models}
    for mid in [x.strip() for x in (cfg.get("extra_models") or "").split(",") if x.strip()]:
        if mid not in known:
            base = dict(models[0]) if models else {"durations": [5], "resolutions": [""], "ratios": None}
            if gateway:
                base = {"durations": [3, 5, 6, 8, 10], "resolutions": ["默认", "720p", "1080p"], "ratios": ["9:16", "16:9", "1:1"]}
            base.update({"id": mid, "label": mid + "（自定义）", "modes": ["t2v", "i2v"],
                         "note": "自定义模型：参数自动识别，模型不支持的选项会被忽略" if gateway else "自定义模型：参数沿用该平台默认值，不支持时接口会报错"})
            models.append(base)
    ready = bool(cfg.get("enabled", True)) and all(cfg.get(f["k"]) for f in p["fields"] if not f.get("optional"))
    return {
        "id": pid, "name": p["name"], "home": p["home"], "free": p["free"], "regions": p["regions"], "group": p.get("group", "cn"),
        "fields": [{"k": f["k"], "label": f["label"], "secret": f["secret"], "multiline": f.get("multiline", False), "set": bool(cfg.get(f["k"])),
                    "value": "" if f["secret"] else cfg.get(f["k"], "")} for f in p["fields"]],
        "region": cfg.get("region") if cfg.get("region") in p["regions"] else list(p["regions"])[0], "enabled": bool(cfg.get("enabled", True)), "ready": ready,
        "extra_models": cfg.get("extra_models", ""), "daily_limit": cfg.get("daily_limit", 0), "quota_note": cfg.get("quota_note", ""),
        "video_models": models, "image_models": p.get("image_models", []), "model_hint": p.get("model_hint", ""),
    }


def host(pid, cfg):
    if os.environ.get("CF_MOCK"):  # 仅用于自动化测试
        return os.environ["CF_MOCK"].rstrip("/") + "/" + pid
    if pid == "google":
        loc = cfg.get("region") if cfg.get("region") in PROVIDERS["google"]["regions"] else "us-central1"
        return "https://%s-aiplatform.googleapis.com/v1" % loc
    return PROVIDERS[pid]["hosts"].get(cfg.get("region", "cn"), list(PROVIDERS[pid]["hosts"].values())[0])


# ---------------------------------------------------------------- MiniMax
def _mm_check(st, js):
    br = js.get("base_resp") or {}
    code = br.get("status_code", 0)
    if code:
        hints = {1004: "Key 无效或国内/国际选错", 1008: "余额或额度不足", 1026: "提示词触发内容审核", 1027: "生成内容未通过审核",
                 2013: "参数不支持（如 1080P 选了 10 秒）", 2038: "需先完成实名认证", 1002: "请求太频繁", 1039: "触发限流"}
        raise ProviderError("%s（%s %s）" % (hints.get(code, "MiniMax 接口错误"), code, br.get("status_msg", "")))
    if st >= 400:
        raise ProviderError("HTTP %s：%s" % (st, short(js)))


def _mm_headers(cfg):
    return {"Authorization": "Bearer " + cfg["api_key"]}


def minimax_submit(cfg, model, prompt, opt, image):
    body = {"model": model, "prompt": prompt, "duration": int(opt.get("duration") or 6),
            "resolution": opt.get("resolution") or "768P", "prompt_optimizer": bool(opt.get("optimizer"))}
    if image:
        body["first_frame_image"] = image_data_uri(image)
    st, js = http_json("POST", host("minimax", cfg) + "/v1/video_generation", _mm_headers(cfg), body)
    _mm_check(st, js)
    if not js.get("task_id"):
        raise ProviderError("没有返回 task_id：" + short(js))
    return str(js["task_id"])


def minimax_query(cfg, model, task_id):
    st, js = http_json("GET", host("minimax", cfg) + "/v1/query/video_generation?task_id=" + task_id, _mm_headers(cfg))
    _mm_check(st, js)
    s = js.get("status")
    if s == "Success" and js.get("file_id"):
        st2, js2 = http_json("GET", host("minimax", cfg) + "/v1/files/retrieve?file_id=%s" % js["file_id"], _mm_headers(cfg))
        _mm_check(st2, js2)
        url = (js2.get("file") or {}).get("download_url")
        if not url:
            raise ProviderError("没有拿到下载链接")
        return {"status": "succeeded", "url": url}
    if s == "Fail":
        return {"status": "failed", "error": "生成失败"}
    return {"status": "running"}


def minimax_image(cfg, model, prompt, ratio):
    body = {"model": model, "prompt": prompt, "aspect_ratio": ratio, "n": 1, "response_format": "url", "prompt_optimizer": False}
    st, js = http_json("POST", host("minimax", cfg) + "/v1/image_generation", _mm_headers(cfg), body, timeout=180)
    _mm_check(st, js)
    urls = (js.get("data") or {}).get("image_urls") or []
    if not urls:
        raise ProviderError("没有返回图片：" + short(js))
    return urls[0]


# ---------------------------------------------------------------- 火山方舟 Seedance
def _ark_err(st, js):
    if st >= 400 or js.get("error"):
        e = js.get("error") or {}
        raise ProviderError("方舟错误：%s %s" % (e.get("code", st), e.get("message", short(js))))


def ark_submit(cfg, model, prompt, opt, image):
    content = [{"type": "text", "text": prompt}]
    if image:
        content.append({"type": "image_url", "image_url": {"url": image_data_uri(image)}})
    body = {"model": model, "content": content, "duration": int(opt.get("duration") or 5), "watermark": False}
    if opt.get("resolution"):
        body["resolution"] = opt["resolution"]
    if opt.get("ratio"):
        body["ratio"] = opt["ratio"]
    st, js = http_json("POST", host("ark", cfg) + "/contents/generations/tasks", {"Authorization": "Bearer " + cfg["api_key"]}, body)
    _ark_err(st, js)
    if not js.get("id"):
        raise ProviderError("没有返回任务 ID：" + short(js))
    return js["id"]


def ark_query(cfg, model, task_id):
    st, js = http_json("GET", host("ark", cfg) + "/contents/generations/tasks/" + task_id, {"Authorization": "Bearer " + cfg["api_key"]})
    _ark_err(st, js)
    s = js.get("status")
    if s == "succeeded":
        url = (js.get("content") or {}).get("video_url")
        return {"status": "succeeded", "url": url} if url else {"status": "failed", "error": "成功但没有视频地址"}
    if s in ("failed", "cancelled", "expired"):
        return {"status": "failed", "error": short((js.get("error") or {}).get("message") or s)}
    return {"status": "running"}


def ark_image(cfg, model, prompt, ratio):
    w, h = ratio_size(ratio, 1280)
    body = {"model": model, "prompt": prompt, "size": "%dx%d" % (w, h), "response_format": "url", "watermark": False}
    st, js = http_json("POST", host("ark", cfg) + "/images/generations", {"Authorization": "Bearer " + cfg["api_key"]}, body, timeout=180)
    _ark_err(st, js)
    data = js.get("data") or []
    if not data or not data[0].get("url"):
        raise ProviderError("没有返回图片：" + short(js))
    return data[0]["url"]


# ---------------------------------------------------------------- 阿里百炼 万相
# 万相文生视频只接受固定的几种分辨率
WAN_SIZES = {
    "480P": {"16:9": (832, 480), "9:16": (480, 832), "1:1": (624, 624), "4:3": (640, 480), "3:4": (480, 640)},
    "720P": {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (960, 960), "4:3": (1088, 832), "3:4": (832, 1088)},
    "1080P": {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1440, 1440), "4:3": (1632, 1248), "3:4": (1248, 1632)},
}
def _bl_err(st, js):
    if st >= 400 or js.get("code"):
        raise ProviderError("百炼错误：%s %s" % (js.get("code", st), js.get("message", short(js))))


def bailian_submit(cfg, model, prompt, opt, image):
    res = (opt.get("resolution") or "720P").upper()
    params = {"duration": int(opt.get("duration") or 5), "prompt_extend": bool(opt.get("optimizer")), "watermark": False}
    inp = {"prompt": prompt}
    if image:
        inp["img_url"] = image_data_uri(image)
        params["resolution"] = res
    else:
        w, h = WAN_SIZES.get(res, WAN_SIZES["720P"]).get(opt.get("ratio") or "16:9", (1280, 720))
        params["size"] = "%d*%d" % (w, h)
    headers = {"Authorization": "Bearer " + cfg["api_key"], "X-DashScope-Async": "enable"}
    st, js = http_json("POST", host("bailian", cfg) + "/services/aigc/video-generation/video-synthesis", headers,
                       {"model": model, "input": inp, "parameters": params})
    _bl_err(st, js)
    tid = (js.get("output") or {}).get("task_id")
    if not tid:
        raise ProviderError("没有返回任务 ID：" + short(js))
    return tid


def bailian_query(cfg, model, task_id):
    st, js = http_json("GET", host("bailian", cfg) + "/tasks/" + task_id, {"Authorization": "Bearer " + cfg["api_key"]})
    _bl_err(st, js)
    out = js.get("output") or {}
    s = out.get("task_status")
    if s == "SUCCEEDED":
        return {"status": "succeeded", "url": out.get("video_url")}
    if s in ("FAILED", "CANCELED", "UNKNOWN"):
        return {"status": "failed", "error": short(out.get("message") or s)}
    return {"status": "running"}


# ---------------------------------------------------------------- 可灵 Kling
def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def kling_jwt(ak, sk):
    now = int(time.time())
    head = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    pay = _b64url(json.dumps({"iss": ak, "exp": now + 1800, "nbf": now - 5}, separators=(",", ":")).encode())
    sig = _b64url(hmac.new(sk.encode(), (head + "." + pay).encode(), hashlib.sha256).digest())
    return head + "." + pay + "." + sig


def _kl_headers(cfg):
    return {"Authorization": "Bearer " + kling_jwt(cfg["access_key"], cfg["secret_key"])}


def _kl_err(st, js):
    if st >= 400 or js.get("code", 0) != 0:
        raise ProviderError("可灵错误：%s %s" % (js.get("code", st), js.get("message", short(js))))


def kling_submit(cfg, model, prompt, opt, image):
    body = {"model_name": model, "prompt": prompt, "mode": opt.get("resolution") or "std", "duration": str(opt.get("duration") or 5)}
    if image:
        kind = "image2video"
        body["image"] = image_b64(image)
    else:
        kind = "text2video"
        body["aspect_ratio"] = opt.get("ratio") or "16:9"
    st, js = http_json("POST", host("kling", cfg) + "/v1/videos/" + kind, _kl_headers(cfg), body)
    _kl_err(st, js)
    tid = (js.get("data") or {}).get("task_id")
    if not tid:
        raise ProviderError("没有返回任务 ID：" + short(js))
    return kind + ":" + tid


def kling_query(cfg, model, task_id):
    kind, tid = task_id.split(":", 1)
    st, js = http_json("GET", host("kling", cfg) + "/v1/videos/%s/%s" % (kind, tid), _kl_headers(cfg))
    _kl_err(st, js)
    d = js.get("data") or {}
    s = d.get("task_status")
    if s == "succeed":
        vids = (d.get("task_result") or {}).get("videos") or []
        return {"status": "succeeded", "url": vids[0]["url"]} if vids else {"status": "failed", "error": "成功但没有视频"}
    if s == "failed":
        return {"status": "failed", "error": d.get("task_status_msg") or "生成失败"}
    return {"status": "running"}


# ---------------------------------------------------------------- Vidu
def _vd_headers(cfg):
    return {"Authorization": "Token " + cfg["api_key"]}


def _vd_err(st, js):
    if st >= 400 or js.get("err_code") or js.get("code"):
        raise ProviderError("Vidu 错误：%s %s" % (js.get("err_code") or js.get("code") or st, js.get("message") or short(js)))


def vidu_submit(cfg, model, prompt, opt, image):
    body = {"model": model, "prompt": prompt, "duration": int(opt.get("duration") or 5), "resolution": opt.get("resolution") or "720p",
            "movement_amplitude": "auto"}
    if image:
        path = "/img2video"
        body["images"] = [image_data_uri(image)]
    else:
        path = "/text2video"
        body["aspect_ratio"] = opt.get("ratio") or "16:9"
    st, js = http_json("POST", host("vidu", cfg) + path, _vd_headers(cfg), body)
    _vd_err(st, js)
    if not js.get("task_id"):
        raise ProviderError("没有返回任务 ID：" + short(js))
    return str(js["task_id"])


def vidu_query(cfg, model, task_id):
    st, js = http_json("GET", host("vidu", cfg) + "/tasks/%s/creations" % task_id, _vd_headers(cfg))
    _vd_err(st, js)
    s = js.get("state")
    if s == "success":
        cr = js.get("creations") or []
        return {"status": "succeeded", "url": cr[0].get("url")} if cr else {"status": "failed", "error": "成功但没有视频"}
    if s == "failed":
        return {"status": "failed", "error": js.get("err_code") or "生成失败"}
    return {"status": "running"}



# ---------------------------------------------------------------- Google Vertex AI（Veo / Imagen）
_GTOK = {}


def _http_form(url, form, timeout=60):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        raise ProviderError("网络请求失败：%s" % e)


def _rs256(private_key_pem, msg):
    """用系统自带的 openssl 做 RS256 签名（不依赖第三方 Python 库）"""
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(private_key_pem)
        os.chmod(path, 0o600)
        p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", path], input=msg, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            raise ProviderError("服务账号私钥签名失败（服务器需要 openssl）：%s" % p.stderr.decode()[:200])
        return p.stdout
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _gsa(cfg):
    try:
        sa = json.loads(cfg.get("service_account") or "")
        assert sa.get("client_email") and sa.get("private_key")
        return sa
    except Exception:
        raise ProviderError("服务账号 JSON 格式不对：请把下载的 .json 密钥文件完整内容粘贴进来")


def google_token(cfg):
    sa = _gsa(cfg)
    c = _GTOK.get(sa["client_email"])
    if c and c[1] > time.time() + 120:
        return c[0]
    now = int(time.time())
    uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(json.dumps({"iss": sa["client_email"], "scope": "https://www.googleapis.com/auth/cloud-platform", "aud": uri,
                                 "iat": now, "exp": now + 3600}, separators=(",", ":")).encode())
    sig = _b64url(_rs256(sa["private_key"], (head + "." + claims).encode()))
    st, js = _http_form(uri, {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": head + "." + claims + "." + sig})
    if st >= 400 or not js.get("access_token"):
        raise ProviderError("Google 登录失败：%s" % short(js))
    _GTOK[sa["client_email"]] = (js["access_token"], now + int(js.get("expires_in", 3600)))
    return js["access_token"]


def _gproj(cfg):
    pid = (cfg.get("project_id") or "").strip() or _gsa(cfg).get("project_id")
    if not pid:
        raise ProviderError("缺少 Google Cloud 项目 ID")
    loc = cfg.get("region") if cfg.get("region") in PROVIDERS["google"]["regions"] else "us-central1"
    return "%s/projects/%s/locations/%s/publishers/google/models" % (host("google", cfg), pid, loc)


def _gerr(st, js):
    if st >= 400 or js.get("error"):
        e = js.get("error") or {}
        msg = e.get("message") if isinstance(e, dict) else str(e)
        hint = ""
        if st == 403 or "PERMISSION" in str(e):
            hint = "（检查：Vertex AI API 是否已开通、服务账号是否有 Vertex AI User 角色）"
        elif st == 429:
            hint = "（触发限流，稍后再试）"
        raise ProviderError("Google 错误 %s：%s%s" % (st, (msg or short(js))[:300], hint))


def google_submit(cfg, model, prompt, opt, image):
    audio = model.endswith("+audio")
    mid = model.replace("+audio", "")
    inst = {"prompt": prompt}
    if image:
        inst["image"] = {"bytesBase64Encoded": image_b64(image), "mimeType": mimetypes.guess_type(image)[0] or "image/jpeg"}
    params = {"sampleCount": 1, "durationSeconds": int(opt.get("duration") or 8), "generateAudio": audio,
              "personGeneration": "allow_adult"}
    # Veo 3 系列的提示词增强无法关闭（传 false 会报错），只有旧版才允许显式设置
    if not mid.startswith("veo-3"):
        params["enhancePrompt"] = bool(opt.get("optimizer"))
    if opt.get("ratio"):
        params["aspectRatio"] = opt["ratio"]
    if opt.get("resolution"):
        params["resolution"] = opt["resolution"]
    st, js = http_json("POST", "%s/%s:predictLongRunning" % (_gproj(cfg), mid), {"Authorization": "Bearer " + google_token(cfg)},
                       {"instances": [inst], "parameters": params})
    _gerr(st, js)
    if not js.get("name"):
        raise ProviderError("没有返回任务：" + short(js))
    return mid + "|" + js["name"]


def google_query(cfg, model, task_id):
    mid, op = task_id.split("|", 1)
    st, js = http_json("POST", "%s/%s:fetchPredictOperation" % (_gproj(cfg), mid), {"Authorization": "Bearer " + google_token(cfg)},
                       {"operationName": op}, timeout=180)
    _gerr(st, js)
    if not js.get("done"):
        return {"status": "running"}
    if js.get("error"):
        return {"status": "failed", "error": short(js["error"].get("message") if isinstance(js["error"], dict) else js["error"])}
    resp = js.get("response") or {}
    vids = resp.get("videos") or []
    if vids and vids[0].get("bytesBase64Encoded"):
        return {"status": "succeeded", "data": base64.b64decode(vids[0]["bytesBase64Encoded"])}
    if vids and str(vids[0].get("gcsUri", "")).startswith("http"):
        return {"status": "succeeded", "url": vids[0]["gcsUri"]}
    if resp.get("raiMediaFilteredCount"):
        return {"status": "failed", "error": "被 Google 安全审核拦截：%s（改一下描述再试，通常不扣费）" % short(resp.get("raiMediaFilteredReasons") or "")}
    return {"status": "failed", "error": "没有返回视频：" + short(resp)}


def google_image(cfg, model, prompt, ratio):
    st, js = http_json("POST", "%s/%s:predict" % (_gproj(cfg), model), {"Authorization": "Bearer " + google_token(cfg)},
                       {"instances": [{"prompt": prompt}], "parameters": {"sampleCount": 1, "aspectRatio": ratio, "personGeneration": "allow_adult"}},
                       timeout=180)
    _gerr(st, js)
    preds = js.get("predictions") or []
    if not preds or not preds[0].get("bytesBase64Encoded"):
        raise ProviderError("没有返回图片（可能被安全审核拦截）：" + short(js))
    return base64.b64decode(preds[0]["bytesBase64Encoded"])


# ---------------------------------------------------------------- Hugging Face Spaces（自动识别 Gradio 接口）
HF_JOBS = {}  # task_id -> {"status","url","error"}


def _hf_base(space):
    space = space.strip().rstrip("/")
    if os.environ.get("CF_MOCK"):
        return os.environ["CF_MOCK"].rstrip("/") + "/hf/" + space
    if space.startswith("http"):
        return space
    sub = space.lower().replace("/", "-").replace("_", "-").replace(".", "-")
    return "https://%s.hf.space" % sub


def _hf_headers(cfg):
    return {"Authorization": "Bearer " + cfg["hf_token"]} if cfg.get("hf_token") else {}


def _hf_pick(info):
    """从 /gradio_api/info 里挑一个输出视频、输入里有提示词的接口"""
    best, best_score = None, -1
    for name, ep in (info.get("named_endpoints") or {}).items():
        params = ep.get("parameters") or []
        rets = ep.get("returns") or []
        score = 0
        if any("video" in (r.get("component") or "").lower() or "video" in json.dumps(r.get("type") or "").lower() for r in rets):
            score += 4
        if any(_is_prompt(p) for p in params):
            score += 2
        if any((p.get("component") or "").lower() == "image" for p in params):
            score += 1
        if score > best_score:
            best, best_score = (name, params), score
    if not best or best_score < 4:
        raise ProviderError("这个 Space 没有找到「生成视频」的接口，可能不是视频 Space 或已改版")
    return best


def _plabel(p):
    return ("%s %s" % (p.get("parameter_name") or "", p.get("label") or "")).lower()


def _is_prompt(p):
    l = _plabel(p)
    return (p.get("component") or "").lower() in ("textbox", "text") and ("prompt" in l or "提示" in l) and "neg" not in l


def _hf_upload(base, cfg, path):
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    name = os.path.basename(path)
    body = (("--%s\r\nContent-Disposition: form-data; name=\"files\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
             % (boundary, name, mimetypes.guess_type(path)[0] or "image/jpeg")).encode() + content + ("\r\n--%s--\r\n" % boundary).encode())
    h = dict(_hf_headers(cfg), **{"Content-Type": "multipart/form-data; boundary=" + boundary})
    req = urllib.request.Request(base + "/gradio_api/upload", data=body, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            paths = json.loads(r.read().decode())
    except Exception as e:
        raise ProviderError("上传首帧到 Space 失败：%s" % e)
    return {"path": paths[0], "orig_name": name, "meta": {"_type": "gradio.FileData"}}


def _hf_find_video(obj, base):
    if isinstance(obj, dict):
        for k in ("video", "value"):
            if k in obj:
                r = _hf_find_video(obj[k], base)
                if r:
                    return r
        if obj.get("url"):
            return obj["url"]
        if obj.get("path"):
            return base + "/gradio_api/file=" + obj["path"]
    if isinstance(obj, list):
        for x in obj:
            r = _hf_find_video(x, base)
            if r:
                return r
    if isinstance(obj, str) and (obj.endswith(".mp4") or obj.endswith(".webm")):
        return obj if obj.startswith("http") else base + "/gradio_api/file=" + obj
    return None


def _hf_wait(task_id, base, api, event_id, cfg):
    url = "%s/gradio_api/call/%s/%s" % (base, api, event_id)
    try:
        req = urllib.request.Request(url, headers=_hf_headers(cfg))
        with urllib.request.urlopen(req, timeout=1800) as r:
            event = None
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event == "complete":
                        out = json.loads(data)
                        vurl = _hf_find_video(out, base)
                        HF_JOBS[task_id] = {"status": "succeeded", "url": vurl} if vurl else {"status": "failed", "error": "没找到视频输出：" + short(out)}
                        return
                    if event == "error":
                        msg = data if data and data != "null" else "Space 报错（常见原因：今天的免费 GPU 时长用完了，或 Space 正忙）"
                        HF_JOBS[task_id] = {"status": "failed", "error": "Hugging Face：" + short(msg, 300)}
                        return
        HF_JOBS[task_id] = {"status": "failed", "error": "连接中断，没有拿到结果"}
    except Exception as e:
        HF_JOBS[task_id] = {"status": "failed", "error": "Hugging Face 连接出错：%s" % e}


def hf_submit(cfg, model, prompt, opt, image):
    base = _hf_base(model)
    st, info = http_json("GET", base + "/gradio_api/info", _hf_headers(cfg), timeout=60)
    if st >= 400:
        raise ProviderError("打不开这个 Space（%s）：可能已下线、在休眠或需要重启，换一个试试" % st)
    api, params = _hf_pick(info)
    data, used_prompt = [], False
    for p in params:
        comp, l = (p.get("component") or "").lower(), _plabel(p)
        default = p.get("parameter_default") if p.get("parameter_has_default") else None
        if not used_prompt and _is_prompt(p):
            data.append(prompt); used_prompt = True
        elif comp == "image":
            if image:
                data.append(_hf_upload(base, cfg, image))
            elif p.get("parameter_has_default"):
                data.append(default)
            else:
                raise ProviderError("这个 Space 只能图生视频：请先给镜头加首帧图")
        elif comp in ("slider", "number") and any(k in l for k in ("duration", "second", "时长", "秒")):
            data.append(float(opt.get("duration") or default or 3))
        else:
            data.append(default)
    if not used_prompt:
        raise ProviderError("没在这个 Space 里找到提示词输入框")
    st, js = http_json("POST", "%s/gradio_api/call/%s" % (base, api.lstrip("/")), _hf_headers(cfg), {"data": data})
    if st >= 400 or not js.get("event_id"):
        raise ProviderError("提交到 Space 失败：%s" % short(js))
    task_id = "%s|%s|%s" % (model, api.lstrip("/"), js["event_id"])
    HF_JOBS[task_id] = {"status": "running"}
    threading.Thread(target=_hf_wait, args=(task_id, base, api.lstrip("/"), js["event_id"], cfg), daemon=True).start()
    return task_id


def hf_query(cfg, model, task_id):
    job = HF_JOBS.get(task_id)
    if not job:
        return {"status": "failed", "error": "服务重启导致这个免费任务丢失，请重新生成"}
    return job


# ---------------------------------------------------------------- Replicate（自动识别模型参数）
_RSCHEMA = {}


def _rp_headers(cfg):
    return {"Authorization": "Bearer " + cfg["api_token"]}


def _rp_err(st, js):
    if st >= 400:
        d = js.get("detail") or js.get("title") or short(js)
        hint = "（余额不足：免费次数用完后需在 Replicate 充值）" if st == 402 else ""
        raise ProviderError("Replicate 错误 %s：%s%s" % (st, str(d)[:300], hint))


def _rp_schema(cfg, model):
    if model in _RSCHEMA:
        return _RSCHEMA[model]
    st, js = http_json("GET", "%s/models/%s" % (host("replicate", cfg), model), _rp_headers(cfg))
    _rp_err(st, js)
    sch = ((js.get("latest_version") or {}).get("openapi_schema") or {}).get("components", {}).get("schemas", {})
    _RSCHEMA[model] = sch
    return sch


def _rp_prop(sch, name):
    props = (sch.get("Input") or {}).get("properties") or {}
    p = props.get(name)
    if p is None:
        return None
    for ref in [x.get("$ref") for x in p.get("allOf", []) if isinstance(x, dict)]:
        if ref:
            p = dict(p, **sch.get(ref.split("/")[-1], {}))
    return p


def _rp_fit(prop, want):
    """把想要的值适配到模型允许的取值"""
    enum = prop.get("enum")
    if enum:
        norm = lambda x: str(x).strip().lower()
        for e in enum:
            if norm(e) == norm(want):
                return e
        try:
            num = lambda x: float(norm(x).rstrip("ps"))
            return min(enum, key=lambda e: abs(num(e) - num(want)))
        except Exception:
            return enum[0]
    if prop.get("type") == "integer":
        v = int(float(want))
        if "maximum" in prop:
            v = min(v, prop["maximum"])
        if "minimum" in prop:
            v = max(v, prop["minimum"])
        return v
    if prop.get("type") == "number":
        return float(want)
    return want


def replicate_submit(cfg, model, prompt, opt, image):
    sch = _rp_schema(cfg, model)
    inp = {"prompt": prompt}
    if image:
        key = next((k for k in ("first_frame_image", "start_image", "image", "input_image", "first_frame", "image_url") if _rp_prop(sch, k)), None)
        if not key:
            raise ProviderError("这个模型不支持首帧图，请取消「用首帧图」")
        inp[key] = image_data_uri(image)
    for k, v in (("duration", opt.get("duration")), ("aspect_ratio", opt.get("ratio")), ("resolution", opt.get("resolution"))):
        p = _rp_prop(sch, k)
        if p is not None and v not in (None, "", "默认"):
            inp[k] = _rp_fit(p, v)
    if _rp_prop(sch, "prompt_optimizer") is not None:
        inp["prompt_optimizer"] = bool(opt.get("optimizer"))
    st, js = http_json("POST", "%s/models/%s/predictions" % (host("replicate", cfg), model), _rp_headers(cfg), {"input": inp})
    _rp_err(st, js)
    if not js.get("id"):
        raise ProviderError("没有返回任务 ID：" + short(js))
    return js["id"]


def replicate_query(cfg, model, task_id):
    st, js = http_json("GET", "%s/predictions/%s" % (host("replicate", cfg), task_id), _rp_headers(cfg))
    _rp_err(st, js)
    s = js.get("status")
    if s == "succeeded":
        out = js.get("output")
        if isinstance(out, list):
            out = out[0] if out else None
        if isinstance(out, dict):
            out = out.get("video") or out.get("url")
        return {"status": "succeeded", "url": out} if out else {"status": "failed", "error": "成功但没有视频"}
    if s in ("failed", "canceled", "aborted"):
        return {"status": "failed", "error": short(js.get("error") or s)}
    return {"status": "running"}


# ---------------------------------------------------------------- 智谱 CogVideoX
def _zp_headers(cfg):
    return {"Authorization": "Bearer " + cfg["api_key"]}


def _zp_err(st, js):
    if st >= 400 or js.get("error"):
        e = js.get("error") or {}
        raise ProviderError("智谱错误：%s %s" % (e.get("code", st), e.get("message", short(js))))


ZP_SIZES = {"720p": {"16:9": "1280x720", "9:16": "720x1280", "1:1": "1024x1024"},
            "1080p": {"16:9": "1920x1080", "9:16": "1080x1920", "1:1": "1024x1024"}}


def zhipu_submit(cfg, model, prompt, opt, image):
    body = {"model": model, "prompt": prompt[:1500], "with_audio": False, "duration": int(opt.get("duration") or 5)}
    if image:
        body["image_url"] = image_data_uri(image)
    else:
        body["size"] = ZP_SIZES.get((opt.get("resolution") or "720p").lower(), ZP_SIZES["720p"]).get(opt.get("ratio") or "16:9", "1280x720")
    st, js = http_json("POST", host("zhipu", cfg) + "/videos/generations", _zp_headers(cfg), body)
    _zp_err(st, js)
    if not js.get("id"):
        raise ProviderError("没有返回任务 ID：" + short(js))
    return js["id"]


def zhipu_query(cfg, model, task_id):
    st, js = http_json("GET", host("zhipu", cfg) + "/async-result/" + task_id, _zp_headers(cfg))
    _zp_err(st, js)
    s = js.get("task_status")
    if s == "SUCCESS":
        vr = js.get("video_result") or []
        return {"status": "succeeded", "url": vr[0]["url"]} if vr else {"status": "failed", "error": "成功但没有视频"}
    if s == "FAIL":
        return {"status": "failed", "error": "智谱生成失败"}
    return {"status": "running"}


def zhipu_image(cfg, model, prompt, ratio):
    sizes = {"9:16": "720x1440", "16:9": "1440x720", "1:1": "1024x1024", "3:4": "864x1152", "4:3": "1152x864"}
    st, js = http_json("POST", host("zhipu", cfg) + "/images/generations", _zp_headers(cfg),
                       {"model": model, "prompt": prompt[:1000], "size": sizes.get(ratio, "1024x1024")}, timeout=180)
    _zp_err(st, js)
    data = js.get("data") or []
    if not data or not data[0].get("url"):
        raise ProviderError("没有返回图片：" + short(js))
    return data[0]["url"]


# ---------------------------------------------------------------- OpenRouter
def _or_headers(cfg):
    return {"Authorization": "Bearer " + cfg["api_key"], "HTTP-Referer": "https://clipforge.local", "X-Title": "ClipForge"}


def _or_err(st, js):
    if st >= 400 or js.get("error"):
        e = js.get("error") or {}
        msg = e.get("message") if isinstance(e, dict) else str(e)
        hint = "（触发免费模型频率限制或额度不足）" if st in (402, 429) else ""
        raise ProviderError("OpenRouter 错误 %s：%s%s" % (st, (msg or short(js))[:300], hint))


def openrouter_submit(cfg, model, prompt, opt, image):
    body = {"model": model, "prompt": prompt, "duration": int(opt.get("duration") or 5)}
    if opt.get("resolution") and opt["resolution"] != "默认":
        body["resolution"] = opt["resolution"]
    if opt.get("ratio") and opt["ratio"] != "adaptive":
        body["aspect_ratio"] = opt["ratio"]
    if image:
        body["frame_images"] = [{"type": "image_url", "image_url": {"url": image_data_uri(image)}, "frame_type": "first_frame"}]
    st, js = http_json("POST", host("openrouter", cfg) + "/videos", _or_headers(cfg), body)
    _or_err(st, js)
    if not js.get("id"):
        raise ProviderError("没有返回任务 ID：" + short(js))
    return js["id"]


def openrouter_query(cfg, model, task_id):
    st, js = http_json("GET", host("openrouter", cfg) + "/videos/" + task_id, _or_headers(cfg))
    _or_err(st, js)
    s = js.get("status")
    if s == "completed":
        if js.get("urls"):
            return {"status": "succeeded", "url": js["urls"][0]}
        return {"status": "succeeded", "url": host("openrouter", cfg) + "/videos/%s/content?index=0" % task_id, "headers": _or_headers(cfg)}
    if s in ("failed", "cancelled", "canceled"):
        return {"status": "failed", "error": short(js.get("error") or s)}
    return {"status": "running"}

# ---------------------------------------------------------------- dispatch
SUBMIT = {"minimax": minimax_submit, "ark": ark_submit, "bailian": bailian_submit, "kling": kling_submit, "vidu": vidu_submit,
          "google": google_submit, "hf": hf_submit, "replicate": replicate_submit, "zhipu": zhipu_submit, "openrouter": openrouter_submit}
QUERY = {"minimax": minimax_query, "ark": ark_query, "bailian": bailian_query, "kling": kling_query, "vidu": vidu_query,
         "google": google_query, "hf": hf_query, "replicate": replicate_query, "zhipu": zhipu_query, "openrouter": openrouter_query}
IMAGE = {"minimax": minimax_image, "ark": ark_image, "google": google_image, "zhipu": zhipu_image}


def submit(pid, cfg, model, prompt, opt, image=None):
    return SUBMIT[pid](cfg, model, prompt, opt, image)


def query(pid, cfg, model, task_id):
    return QUERY[pid](cfg, model, task_id)


def gen_image(pid, cfg, model, prompt, ratio):
    """返回图片 URL（str）或图片内容（bytes）"""
    if pid not in IMAGE:
        raise ProviderError("这个平台暂不支持生图")
    return IMAGE[pid](cfg, model, prompt, ratio)
