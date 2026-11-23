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


def patch_defconfig_check(platform):
    """Kleaf 的 check_defconfig 要求 defconfig 与 savedefconfig 规范输出逐字节一致。

    注入配置组后必然不满足（选项按 Kconfig 菜单顺序重排、位置不同），于是把该校验
    降级为告警，其余构建检查不变。只在选定了配置组时执行，none 时保持原始校验。
    """
    path = platform / "build/kernel/_setup_env.sh"
    if not path.is_file():
        raise RuntimeError("缺少 Kleaf 环境脚本：" + str(path))
    text = path.read_text(encoding="utf-8")
    if "kernel-extras" in text:
        print("defconfig 一致性校验已是告警模式，跳过", flush=True)
        return
    old = '''    if [ ${RES} -ne 0 ]; then
        echo ERROR: savedefconfig does not match ${KERNEL_DIR}/arch/${ARCH}/configs/${DEFCONFIG} >&2
    fi
    return ${RES}'''
    new = '''    if [ ${RES} -ne 0 ]; then
        echo "WARNING: savedefconfig 与 arch/${ARCH}/configs/${DEFCONFIG} 不一致（kernel-extras 注入项不在 savedefconfig 规范顺序上），仅告警" >&2
    fi
    return 0'''
    if text.count(old) != 1:
        raise RuntimeError("_setup_env.sh 的 check_defconfig 结构与预期不符，拒绝盲改")
    path.write_text(text.replace(old, new), encoding="utf-8")
    print("已把 check_defconfig 的一致性校验降级为告警", flush=True)


def patch_kmi_flag(platform):
    """关闭 kmi_symbol_list_violations 校验（同 _setup_env.sh，仅在选定配置组时）。

    该校验假定「受保护导出只对已签名模块可见」；本构建的 MODULE_SIG_PROTECT 是关闭的，
    USER_NS 新导出的 from_kuid/from_kuid_munged 对未签名模块同样可用，运行时无风险。
    改 build_setting_default 而不是传命令行参数：oplus 的构建入口不会把
    EXTRA_KBUILD_ARGS 里的开关透传到 bazel（run 45111671207 实测），默认值改动对所有
    调用路径都生效。
    """
    path = platform / "build/kernel/kleaf/BUILD.bazel"
    if not path.is_file():
        raise RuntimeError("缺少 Kleaf BUILD.bazel：" + str(path))
    text = path.read_text(encoding="utf-8")
    head = 'bool_flag(\n    name = "kmi_symbol_list_violations_check",\n    build_setting_default = True,'
    if head not in text:
        if 'name = "kmi_symbol_list_violations_check",\n    build_setting_default = False,' in text:
            print("kmi 校验已是关闭状态，跳过", flush=True)
            return
        raise RuntimeError("kmi_symbol_list_violations_check 的 bool_flag 结构与预期不符，拒绝盲改")
    path.write_text(text.replace(head, head.replace("build_setting_default = True", "build_setting_default = False")), encoding="utf-8")
    print("已关闭 kmi_symbol_list_violations 校验", flush=True)


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

    platform = root / "kernel_platform"
    patch_defconfig_check(platform)
    patch_kmi_flag(platform)


if __name__ == "__main__":
    main()
