#!/usr/bin/env python3
"""按 KERNEL_EXTRAS 选定配置组，注入两棵树的 arch/arm64/configs/gki_defconfig。

gki_defconfig 是 //common:kernel_aarch64_config 与 //msm-kernel:sun_perf_config
的共同基线，因此两棵树都必须写，否则 boot.img 所在的 msm-kernel 侧缺项。

注入是幂等的：已有的同名赋值（含 Kconfig 生成的「is not set」行）先删除，
再写进带界标的块的末尾，重复执行不会让文件增长或错位。
"""
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK_BEGIN = "# >>> kernel-extras (由 ci-install-extras.py 生成，勿手改)"
MARK_END = "# <<< kernel-extras"
TREES = ("common", "msm-kernel")


def symbol_of(line):
    s = line.strip()
    if s.startswith("# "):
        name = s[2:].split(" ")[0]
        return name if name.startswith("CONFIG_") else None
    if s.startswith("CONFIG_"):
        return s.split("=")[0]
    return None


def main():
    groups_all = json.loads((HERE / "kernel-extras.json").read_text(encoding="utf-8"))["groups"]
    raw = os.environ.get("KERNEL_EXTRAS", "").strip()
    groups = [g.strip() for g in raw.split(",") if g.strip() and g.strip() != "none"]
    unknown = [g for g in groups if g not in groups_all]
    if unknown:
        raise SystemExit("未知配置组：" + ", ".join(unknown))
    if not groups:
        print("KERNEL_EXTRAS 为空或 none：不改动 defconfig", flush=True)
        return

    symbols = []
    for g in groups:
        for item in groups_all[g]:
            if item not in symbols:
                symbols.append(item)
    keys = {s.split("=")[0] for s in symbols}

    root = Path(os.environ["TREE_ROOT"]).resolve()
    if os.name != "posix" or not str(root).startswith("/mnt/work/"):
        raise RuntimeError("此脚本只允许在云端 /mnt/work 一次性构建树运行")
    report = Path("/mnt/work/config-extras-report")
    report.mkdir(exist_ok=True)

    for tree_name in TREES:
        path = root / "kernel_platform" / tree_name / "arch/arm64/configs/gki_defconfig"
        if not path.is_file():
            raise RuntimeError("缺少 defconfig：" + str(path))
        kept, skip = [], False
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped == MARK_BEGIN:
                skip = True
                continue
            if stripped == MARK_END:
                skip = False
                continue
            if skip or symbol_of(line) in keys:
                continue
            kept.append(line)
        block = [MARK_BEGIN, "# groups: " + ",".join(groups)] + symbols + [MARK_END]
        path.write_text("\n".join(kept).rstrip("\n") + "\n" + "\n".join(block) + "\n", encoding="utf-8")
        (report / (tree_name + "-gki_defconfig")).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        print("已注入配置组 %s → %s（%d 项）" % (",".join(groups), tree_name, len(symbols)), flush=True)


if __name__ == "__main__":
    main()
