#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按 Google nam-files 的简体中文 slices 重切本地字体（非可变，Regular/400），输出 WOFF2 + 对应 CSS。
默认读取：
  https://raw.githubusercontent.com/googlefonts/nam-files/refs/heads/main/slices/simplified-chinese_default.txt

用法示例（当前目录输出）：
  python reslice_sc_400.py \
    --font ./HarmonyOS_SansSC_Regular.ttf \
    --outdir . \
    --family "HarmonyOSSans-Regular" \
    --jobs 8
"""
import argparse, os, pathlib, re, sys, subprocess, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Set, Tuple

# ---------------- Utilities ----------------

DEFAULT_SLICES_URL = (
    "https://raw.githubusercontent.com/googlefonts/nam-files/refs/heads/main/"
    "slices/simplified-chinese_default.txt"
)

def fetch_text(path_or_url: str) -> str:
    """读取本地或远程文本；支持 GitHub blob -> raw 的容错。"""
    p = path_or_url.strip()
    if p.startswith("http"):
        import requests  # 仅网络场景才需要
        url = p
        if "github.com" in url and "/blob/" in url:
            url = url.replace("github.com/", "raw.githubusercontent.com/").replace("/blob/", "/")
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return r.text
    return pathlib.Path(p).read_text(encoding="utf-8")

_dec_re = re.compile(r'^[0-9]+$')
_hex_re = re.compile(r'^[0-9A-Fa-f]+$')

def _to_int(token: str) -> int:
    """把 U+XXXX / 0xXXXX / 十进制 / 纯十六进制 解析为整数码点。"""
    s = token.strip()
    if not s: raise ValueError("empty token")
    if s.lower().startswith('u+'): return int(s[2:], 16)
    if s.lower().startswith('0x'): return int(s[2:], 16)
    if _dec_re.fullmatch(s):       return int(s, 10)
    if _hex_re.fullmatch(s):       return int(s, 16)
    raise ValueError(f"Bad codepoint token: {token}")

def parse_slices(txt: str) -> List[Set[int]]:
    """
    解析 nam-files 的 slices：
    - 识别 'subsets { ... }' 块，每块一个切片
    - 支持 'codepoints: N'（十进制/十六进制），以及 '..' 或 '-' 区间
    - 保留文件原顺序（CSS unicode-range 选择遵循定义顺序）
    """
    slices: List[Set[int]] = []
    in_block = False
    current: Set[int] = set()
    for raw in txt.splitlines():
        # 去掉行尾注释
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if line.startswith('subsets'):
            if line.endswith('{'):
                if in_block and current:
                    slices.append(current); current = set()
                in_block = True
            continue
        if line.startswith('}'):
            if in_block:
                if current: slices.append(current)
                current = set(); in_block = False
            continue
        if not in_block:
            continue

        if line.startswith('codepoints'):
            _, rest = line.split(':', 1)
            rest = rest.strip().replace('—', '-').replace('–', '-')
            for tok in re.split(r'[\s,;]+', rest):
                if not tok: continue
                if '..' in tok or '-' in tok:
                    a, b = re.split(r'\.\.|-', tok, maxsplit=1)
                    lo, hi = _to_int(a), _to_int(b)
                    if lo > hi: lo, hi = hi, lo
                    current.update(range(lo, hi + 1))
                else:
                    current.add(_to_int(tok))
    if in_block and current:
        slices.append(current)
    return [s for s in slices if s]

def compact_ranges(codepoints: Set[int]) -> List[Tuple[int, int]]:
    if not codepoints: return []
    s = sorted(codepoints)
    out, start, prev = [], s[0], s[0]
    for cp in s[1:]:
        if cp == prev + 1:
            prev = cp; continue
        out.append((start, prev)); start = prev = cp
    out.append((start, prev))
    return out

def ranges_to_css(ranges: List[Tuple[int, int]]) -> str:
    parts = []
    for a, b in ranges:
        parts.append(f"U+{a:04X}" if a == b else f"U+{a:04X}-{b:04X}")
    return ", ".join(parts)

def sanitize_filename(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', name.strip())

# ---------------- Font coverage ----------------

def font_coverage(font_path: pathlib.Path) -> Set[int]:
    """读取字体的 cmap，返回字体实际覆盖的 Unicode 码点集合。"""
    from fontTools.ttLib import TTFont
    cps: Set[int] = set()
    with TTFont(str(font_path), lazy=True, recalcBBoxes=False, recalcTimestamp=False) as font:
        if 'cmap' not in font: return cps
        for t in font['cmap'].tables:
            cps.update(t.cmap.keys())
    return cps

# ---------------- Subset runner ----------------

def build_subset_cmd(unicodes_arg: str, src_font: pathlib.Path, dst_path: pathlib.Path) -> List[str]:
    """
    优先使用 CLI（fonttools subset / pyftsubset），找不到再用模块：python -m fontTools.subset
    显式加上 --ignore-missing-unicodes，避免因缺字失败；并生成 WOFF2。
    """
    common = [
        f"--output-file={str(dst_path)}",
        "--flavor=woff2",
        "--layout-features=*",
        "--drop-tables+=DSIG",
        "--no-hinting",
        "--ignore-missing-unicodes",   # 明确忽略缺失的 Unicode（默认如此；这里显式声明）
        f"--unicodes={unicodes_arg}",
    ]
    # 1) fonttools subset
    ft = shutil.which("fonttools")
    if ft:
        return [ft, "subset", str(src_font), *common]
    # 2) pyftsubset
    pfs = shutil.which("pyftsubset")
    if pfs:
        return [pfs, str(src_font), *common]
    # 3) python -m fontTools.subset（注意大小写）
    return [sys.executable, "-m", "fontTools.subset", str(src_font), *common]

def run_subset(src_font: pathlib.Path, dst_path: pathlib.Path, ur_css: str):
    """执行子集化；将 U+XXXX-YYYY 转为 fonttools 接受的十六进制串。"""
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    unicodes_arg = ur_css.replace("U+", "").replace(" ", "")
    cmd = build_subset_cmd(unicodes_arg, src_font, dst_path)
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"subset failed: {e}; cmd={' '.join(cmd)}") from e

# ---------------- Main ----------------

def main():
    ap = argparse.ArgumentParser(description="按 Google slices(简体中文) 重切非可变字体，生成 WOFF2 和 CSS（权重 400）")
    ap.add_argument("--font", required=True, help="本地字体路径（.ttf/.otf）")
    ap.add_argument("--slices", default=DEFAULT_SLICES_URL, help="slices 源（URL 或本地路径）")
    ap.add_argument("--outdir", default=".", help="输出目录（可用 . 表示当前目录）")
    ap.add_argument("--family", default=None, help="CSS 中的 font-family 名称（默认取字体文件名）")
    ap.add_argument("--style", default="normal", choices=["normal", "italic"], help="CSS font-style")
    ap.add_argument("--weight", default="400", help="CSS font-weight（非可变字体建议填具体值，如 400）")
    ap.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1), help="并行切片数")
    args = ap.parse_args()

    font_path = pathlib.Path(args.font).resolve()
    outdir = pathlib.Path(args.outdir).resolve(); outdir.mkdir(parents=True, exist_ok=True)
    family_css = args.family or font_path.stem
    family_file = sanitize_filename(family_css)

    # 1) 读取 slices 与字体覆盖
    txt = fetch_text(args.slices)
    slices = parse_slices(txt)
    if not slices:
        print("未能从 slices 解析任何切片；请检查 raw 链接或换用固定 release。", file=sys.stderr)
        sys.exit(2)

    covered = font_coverage(font_path)
    total = len(slices)
    print(f"解析到 {total} 个切片，开始子集化（WOFF2, weight {args.weight}, {args.style}）……")

    # 2) 并行子集化（跳过与字体无交集的切片，如 emoji）
    css_rules: List[Tuple[int, str]] = []
    skipped = 0

    def work(i: int, cps: Set[int]):
        inter = cps & covered
        if not inter:
            return i, None  # skip
        rng = compact_ranges(inter)
        ur = ranges_to_css(rng)
        outname = f"{family_file}-slice{i:03d}.woff2"
        run_subset(font_path, outdir / outname, ur)
        css = (
            "@font-face{"
            f"font-family:'{family_css}';"
            f"font-style:{args.style};"
            f"font-weight:{args.weight};"
            f"src:url('{outname}') format('woff2');"
            "font-display:swap;"
            f"unicode-range:{ur};"
            "}"
        )
        return i, css

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = [ex.submit(work, idx+1, cps) for idx, cps in enumerate(slices)]
        done = 0
        for fu in as_completed(futs):
            i, css = fu.result()
            done += 1
            if css is None:
                skipped += 1
            else:
                css_rules.append((i, css))
            if done % 10 == 0 or done == total:
                print(f"…进度 {done}/{total}（已跳过 {skipped}）")

    css_rules.sort(key=lambda x: x[0])
    css_text = "/* generated from nam-files slices; keep order for unicode-range prioritization */\n" + \
               "\n".join(rule for _, rule in css_rules)

    css_path = outdir / f"{family_file}.sc-slices.css"
    css_path.write_text(css_text, encoding="utf-8")

    print(f"✅ 完成：总 {total} 片，输出 {len(css_rules)} 片（跳过 {skipped}）到 {outdir}")
    print(f"✅ CSS 已生成：{css_path}")
    print("提示：把 CSS 引入页面即可；若希望保留 hinting，请去掉 --no-hinting；需要 ttf 输出可自行改 --flavor。")

if __name__ == "__main__":
    main()
