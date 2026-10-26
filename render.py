"""成片合成：用 ffmpeg 把各镜头片段统一规格、加转场、配乐。"""
import json, os, subprocess, uuid

SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "3:4": (1080, 1440), "4:3": (1440, 1080)}
TRANSITIONS = {  # xfade 名称（ffmpeg 4.3+ 都支持）
    "none": "无转场（硬切）", "fade": "淡入淡出", "fadewhite": "闪白", "fadeblack": "闪黑", "dissolve": "溶解",
    "slideleft": "左滑", "slideup": "上滑", "circleopen": "圆形展开", "wipeleft": "擦除", "radial": "径向",
}
FPS = 30


class RenderError(Exception):
    pass


def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        tail = p.stderr.decode("utf-8", "replace").strip().splitlines()[-6:]
        raise RenderError("ffmpeg 出错：\n" + "\n".join(tail))
    return p.stdout


def probe(path):
    """返回 (时长秒, 是否有音轨, 宽, 高)"""
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", path])
    js = json.loads(out.decode("utf-8") or "{}")
    dur = float((js.get("format") or {}).get("duration") or 0)
    streams = js.get("streams") or []
    has_a = any(s.get("codec_type") == "audio" for s in streams)
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    return dur, has_a, v.get("width") or 0, v.get("height") or 0


def thumbnail(video, out_jpg):
    try:
        dur = probe(video)[0]
        run(["ffmpeg", "-y", "-v", "error", "-ss", "%.2f" % min(1.0, dur / 2), "-i", video, "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", out_jpg])
        return True
    except Exception:
        return False


def _fit(mode, W, H):
    if mode == "crop":
        return "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d" % (W, H, W, H)
    if mode == "pad":
        return "scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black" % (W, H, W, H)
    # blur：模糊背景填充（竖屏放横屏素材时最好看）
    return ("split[a][b];[a]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,boxblur=24:2[bg];"
            "[b]scale=%d:%d:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2" % (W, H, W, H, W, H))


def _atempo(speed):
    parts, s = [], speed
    while s > 2.0:
        parts.append("atempo=2.0"); s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5"); s /= 0.5
    parts.append("atempo=%.4f" % s)
    return ",".join(parts)


def normalize(src, dst, W, H, fit, start, end, speed):
    dur, has_a, _, _ = probe(src)
    start = max(0.0, float(start or 0))
    end = float(end) if end else dur
    end = min(end, dur) if dur else end
    if end - start < 0.3:
        raise RenderError("片段裁剪后太短：%s" % os.path.basename(src))
    speed = float(speed or 1) or 1.0
    out_d = (end - start) / speed
    vf = "[0:v]setpts=(PTS-STARTPTS)/%.4f,fps=%d,%s,format=yuv420p,setsar=1[v]" % (speed, FPS, _fit(fit, W, H))
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", "%.3f" % start, "-t", "%.3f" % (end - start), "-i", src]
    if has_a:
        af = "[0:a]asetpts=PTS-STARTPTS,%s,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a]" % _atempo(speed)
    else:
        cmd += ["-f", "lavfi", "-t", "%.3f" % out_d, "-i", "anullsrc=r=48000:cl=stereo"]
        af = "[1:a]aformat=sample_fmts=fltp:channel_layouts=stereo[a]"
    cmd += ["-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]", "-t", "%.3f" % out_d,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", dst]
    run(cmd)
    return probe(dst)[0]


def join(clips, durs, transition, tdur, dst):
    if len(clips) == 1:
        run(["ffmpeg", "-y", "-v", "error", "-i", clips[0], "-c", "copy", dst])
        return durs[0]
    if transition == "none":
        lst = dst + ".txt"
        with open(lst, "w", encoding="utf-8") as f:
            for c in clips:
                f.write("file '%s'\n" % os.path.abspath(c).replace("'", "'\\''"))
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", dst])
        os.remove(lst)
        return sum(durs)
    T = max(0.1, min(float(tdur or 0.5), min(durs) / 2.0 - 0.05))
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for c in clips:
        cmd += ["-i", c]
    parts, acc, vlast, alast = [], 0.0, "[0:v]", "[0:a]"
    for i in range(1, len(clips)):
        acc += durs[i - 1]
        off = acc - i * T
        vo, ao = "[v%d]" % i, "[a%d]" % i
        parts.append("%s[%d:v]xfade=transition=%s:duration=%.3f:offset=%.3f%s" % (vlast, i, transition, T, off, vo))
        parts.append("%s[%d:a]acrossfade=d=%.3f%s" % (alast, i, T, ao))
        vlast, alast = vo, ao
    cmd += ["-filter_complex", ";".join(parts), "-map", vlast, "-map", alast,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-r", str(FPS), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", dst]
    run(cmd)
    return sum(durs) - (len(clips) - 1) * T


def add_music(src, music, total, music_vol, keep_audio, orig_vol, dst):
    fo = max(0.0, total - 2.0)
    m = "[1:a]volume=%.3f,atrim=0:%.3f,afade=t=out:st=%.3f:d=2,aformat=sample_fmts=fltp:channel_layouts=stereo[m]" % (music_vol, total, fo)
    if keep_audio:
        # amix 默认会除以输入数，这里乘 2 补回来
        fc = m.replace("volume=%.3f" % music_vol, "volume=%.3f" % (music_vol * 2)) + \
             ";[0:a]volume=%.3f[o];[o][m]amix=inputs=2:duration=first:dropout_transition=0[a]" % (orig_vol * 2)
        amap = "[a]"
    else:
        fc, amap = m, "[m]"
    run(["ffmpeg", "-y", "-v", "error", "-i", src, "-stream_loop", "-1", "-i", music, "-filter_complex", fc,
         "-map", "0:v", "-map", amap, "-t", "%.3f" % total, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", dst])


def render_project(items, opts, workdir, out_path, progress=lambda p, msg: None):
    """items: [{path,start,end,speed,label}]；opts: aspect, fit, transition, tdur, music, music_vol, keep_audio, orig_vol"""
    if not items:
        raise RenderError("没有可用的片段：每个镜头至少要有一条生成成功或上传的视频，并选中它")
    W, H = SIZES.get(opts.get("aspect") or "9:16", SIZES["9:16"])
    os.makedirs(workdir, exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    steps = len(items) + 1 + (1 if opts.get("music") else 0)
    clips, durs = [], []
    for i, it in enumerate(items):
        progress(i / steps, "处理第 %d/%d 个片段：%s" % (i + 1, len(items), it.get("label", "")))
        dst = os.path.join(workdir, "%s_%02d.mp4" % (tag, i))
        durs.append(normalize(it["path"], dst, W, H, opts.get("fit", "blur"), it.get("start"), it.get("end"), it.get("speed")))
        clips.append(dst)
    progress(len(items) / steps, "拼接转场")
    joined = os.path.join(workdir, "%s_joined.mp4" % tag)
    total = join(clips, durs, opts.get("transition", "fade"), opts.get("tdur", 0.5), joined)
    if opts.get("music"):
        progress((len(items) + 1) / steps, "混入背景音乐")
        add_music(joined, opts["music"], total, float(opts.get("music_vol", 0.6)), bool(opts.get("keep_audio", True)),
                  float(opts.get("orig_vol", 1.0)), out_path)
    else:
        run(["ffmpeg", "-y", "-v", "error", "-i", joined, "-c", "copy", "-movflags", "+faststart", out_path])
    for f in clips + [joined]:
        try:
            os.remove(f)
        except OSError:
            pass
    progress(1.0, "完成")
    return total
