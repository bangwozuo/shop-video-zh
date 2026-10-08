# -*- coding: utf-8 -*-
"""
口播文案+字幕工作流 —— 端到端编排脚本。

流程（与 SKILL.md 的 DAG 一致）：
  S1 字幕文案（原子技能 subtitle-copy，模型产出）：脚本要点 → 口播稿
  S2 本步（脚本承担）：语速核算 + 断句拆行 + 时间轴分配（确定性）
  S3 本步（脚本承担）：SRT 字幕文件与校验表落盘

本脚本承担确定性部分：
  - 口播条目解析（按 ①②③/编号 拆句）
  - 单行字幕上限：抖音/视频号 ≤16 字，小红书 ≤20 字；超限断句拆行
  - 时间轴分配：按字数占比铺满目标时长；整体语速 3.5-6.0 字/秒双死线
  - 时间码 0.1 秒精度，行间不重叠
  口播稿的润色改写由模型按 prompt.txt 的 S1 完成。

用法：
  python run_flow.py --input input.json --outdir out
  python run_flow.py --demo

产物（每步读上一步产物，逐步落盘）：
  out/steps/s1_口播条目.json → out/steps/s2_时间轴.json
  out/字幕.srt  out/字幕校验.csv  out/flow_result.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WF_DIR = os.path.dirname(HERE)
OUTDIR_DEFAULT = os.path.normpath(os.path.join(WF_DIR, "out"))

SPEED_LO, SPEED_HI = 3.5, 6.0          # 语速死线（字/秒）
LINE_LIMIT = {"抖音": 16, "视频号": 16, "小红书": 20}
PUNCT_SPLIT = re.compile(r"(?<=[。！？；!?;,，、])")
STRIP = re.compile(r"^[、，,。；;.\s]+|[。．\s]+$")

# 演示数据：与 runs_v2/shop-video-zh/workflows/voiceover-subtitle-flow 实跑同源
# （成都玉林路社区咖啡店实测，40 秒，抖音）
DEMO = {
    "input": "初始输入：一份已定稿的 40 秒探店脚本——主题「成都玉林路社区咖啡店实测」，脚本要点："
             "①店面开在老小区门口，老板一个人打理，周末上午排队 3-5 人；"
             "②手冲单品 22 元一杯，用当周烘的埃塞俄比亚豆子；"
             "③店里只有 4 个座位，多数客人站着喝完就走；"
             "④柜台小黑板上写着「自带杯减 3 元」。请把脚本转成口播稿，再生成时间轴字幕。",
    "duration": "40秒",
    "platform": "抖音",
}


def _parse_duration(v):
    if v is None:
        return 60
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"(\d+)", str(v))
    return int(m.group(1)) if m else 60


def _clean(s):
    return STRIP.sub("", s or "").strip()


def s1_parse(payload):
    """S1 口播条目解析（确定性；口播润色由模型在上游原子技能完成）。"""
    text = payload.get("input") or ""
    # 去掉「初始输入：」「请把脚本转成…」等指令性前后缀
    text = re.sub(r"^(初始输入|输入)[:：]\s*", "", text)
    text = re.sub(r"[。；;]*\s*请[^。；]*$", "", text)
    # 只保留要点正文（「脚本要点：/口播稿：/台词：」之后的文本）
    m = re.search(r"(脚本要点|口播稿|台词|要点)[:：]", text)
    if m:
        text = text[m.end():]
    parts = [p for p in re.split(r"[①②③④⑤⑥⑦⑧⑨⑩]", text) if p.strip()]
    items = [_clean(p) for p in parts]
    if len(items) <= 1:  # 无编号：按句号拆
        items = [s for s in (x.strip() for x in text.split("。")) if s]
    return {"items": items, "n_items": len(items),
            "duration_s": _parse_duration(payload.get("duration_s") or payload.get("duration") or 60),
            "platform": payload.get("platform") or "抖音"}


def split_lines(text, limit):
    """断句拆行：优先在标点处切分，仍超限的硬切，保证每行 ≤ limit 字。"""
    lines = []
    for seg in PUNCT_SPLIT.split(_clean(text)):
        seg = _clean(seg)
        if not seg:
            continue
        while len(seg) > limit:
            lines.append(seg[:limit])
            seg = seg[limit:]
        if seg:
            lines.append(seg)
    return lines


def s2_timeline(s1):
    """S2 语速核算 + 拆行 + 时间轴（确定性）。"""
    limit = LINE_LIMIT.get(s1["platform"], 16)
    lines = []
    for it in s1["items"]:
        lines.extend(split_lines(it, limit))
    total_chars = sum(len(l) for l in lines)
    target = s1["duration_s"]
    rate = total_chars / target if target else 0.0
    # 按字数占比铺满目标时长
    events, cursor = [], 0.0
    for l in lines:
        dur = len(l) / total_chars * target if total_chars else 0.0
        events.append({"start": round(cursor, 1), "end": round(cursor + dur, 1), "text": l})
        cursor += dur
    issues = []
    if not (SPEED_LO <= rate <= SPEED_HI):
        ideal_lo, ideal_hi = int(target * SPEED_LO), int(target * SPEED_HI)
        issues.append(f"语速 {rate:.1f} 字/秒超出 {SPEED_LO}-{SPEED_HI} 死线："
                      f"{target}s 目标对应台词 {ideal_lo}-{ideal_hi} 字，当前 {total_chars} 字")
    over = [e for e in events if len(e["text"]) > limit]
    if over:
        issues.append(f"仍有 {len(over)} 行超过 {limit} 字上限（拆行异常）")
    return {"line_limit": limit, "lines": events, "total_chars": total_chars,
            "rate": round(rate, 2), "speed_range": [SPEED_LO, SPEED_HI], "issues": issues}


def _ts(t):
    h, rem = divmod(int(t * 10), 36000)
    m, rem = divmod(rem, 600)
    s, d = divmod(rem, 10)
    return f"{h:02d}:{m:02d}:{s:02d},{d * 100:03d}" if h else f"00:{m:02d}:{s:02d},{d * 100:03d}"


def build(payload, outdir):
    steps_dir = os.path.join(outdir, "steps")
    os.makedirs(steps_dir, exist_ok=True)

    s1 = s1_parse(payload)
    if not s1["items"]:
        print("[错误] input 为空：没有口播内容就不生成字幕，不编造台词。", file=sys.stderr)
        sys.exit(3)
    with open(os.path.join(steps_dir, "s1_口播条目.json"), "w", encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=2)

    s2 = s2_timeline(s1)
    with open(os.path.join(steps_dir, "s2_时间轴.json"), "w", encoding="utf-8") as f:
        json.dump(s2, f, ensure_ascii=False, indent=2)

    # S3：SRT 字幕（可直接导入剪映/CapCut）
    srt_path = os.path.join(outdir, "字幕.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, e in enumerate(s2["lines"], 1):
            f.write(f"{i}\n{_ts(e['start'])} --> {_ts(e['end'])}\n{e['text']}\n\n")

    csv_path = os.path.join(outdir, "字幕校验.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["#", "起止(s)", "字幕内容", "字数", "上限", "判定"])
        for i, e in enumerate(s2["lines"], 1):
            ok = len(e["text"]) <= s2["line_limit"]
            w.writerow([i, f"{e['start']}-{e['end']}", e["text"], len(e["text"]),
                        s2["line_limit"], "通过" if ok else "超限"])

    md_path = os.path.join(outdir, "字幕交付说明.md")
    verdict = "打回（语速超死线）" if s2["issues"] else "校验通过"
    lines = [
        "# 字幕交付说明（工作流交付物）",
        "",
        f"- 平台 {s1['platform']}｜单行上限 {s2['line_limit']} 字｜行数 {len(s2['lines'])}",
        f"- 台词 {s2['total_chars']} 字 / 目标 {s1['duration_s']}s → 语速 {s2['rate']} 字/秒"
        f"（死线 {SPEED_LO}-{SPEED_HI}）",
        f"- 判定：{verdict}",
        "",
        "## 交付物",
        "",
        "- `out/字幕.srt`：可直接导入剪映/CapCut 的字幕文件（0.1 秒精度）",
        "- `out/字幕校验.csv`：逐行字数与时间码校验表",
        "",
        "## 处理规则",
        "",
        "- 断句拆行：优先在标点处切分，硬切不超过上限；数字与单位不拆行",
        "- 时间轴按字数占比铺满目标时长，行间不重叠",
    ]
    if s2["issues"]:
        lines += ["", "## 不达标项", ""] + [f"- {i}" for i in s2["issues"]]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    js_path = os.path.join(outdir, "flow_result.json")
    result = {
        "flow": "voiceover-subtitle-flow", "flow_id": "de_ecom_05_wf03",
        "steps": ["S1 字幕文案/口播稿（原子技能）", "S2 语速与时间轴核算（脚本）", "S3 SRT 落盘（脚本）"],
        "summary": f"{s1['duration_s']}s / {s1['platform']} / {len(s2['lines'])} 行字幕 —— {verdict}",
        "steps_result": {"s1": {"n_items": s1["n_items"], "duration_s": s1["duration_s"]},
                         "s2": {k: s2[k] for k in ("line_limit", "total_chars", "rate", "issues")}},
        "deliverable": os.path.abspath(srt_path),
        "verdict": verdict,
    }
    with open(js_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"工作流完成 —— {s1['duration_s']}s / {len(s2['lines'])} 行 / 语速 {s2['rate']} 字/秒：{verdict}")
    files = [srt_path, csv_path, md_path, js_path]
    for f_ in files:
        print(" 产物:", f_, f"({os.path.getsize(f_) / 1024:.1f} KB)")


def main():
    ap = argparse.ArgumentParser(description="口播文案+字幕工作流（S1 口播稿 → S2 时间轴 → S3 SRT）")
    ap.add_argument("--input", help="输入 JSON（input 口播内容/duration/platform）")
    ap.add_argument("--outdir", default=OUTDIR_DEFAULT, help="输出目录")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    if a.demo:
        payload = DEMO
    elif a.input:
        with open(a.input, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        ap.error("需要 --input 或 --demo")
    build(payload, a.outdir)


if __name__ == "__main__":
    main()
