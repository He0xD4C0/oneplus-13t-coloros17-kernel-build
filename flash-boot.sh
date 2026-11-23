#!/usr/bin/env sh
# 设备端刷写 boot 分区：自动定位当前槽位，先备份并回测，再写入并回读校验。
#
#   sh flash-boot.sh status
#   sh flash-boot.sh backup
#   sh flash-boot.sh flash boot.img [--slot a|b]
#   sh flash-boot.sh rollback [备份文件]
#
# 只用 su + dd，不做镜像重打包（不改 ramdisk/dtb/header）。
# 目标不是块设备（非 /dev/block/...）时默认拒绝，除非显式 --allow-file，
# 因此可以在普通文件上完整演练流程而不碰任何分区。
set -eu
LC_ALL=C
export LC_ALL

BY_NAME=${BY_NAME:-/dev/block/by-name}
BACKUP_DIR=${BACKUP_DIR:-/data/adb/boot-backups}
CHUNK=1048576
ALLOW_FILE=0
SLOT=""
CMD=${1:-}
[ $# -gt 0 ] && shift || true
IMAGE=""

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --slot) [ $# -ge 2 ] || die "--slot 缺少值"; SLOT=$2; shift 2 ;;
        --allow-file) ALLOW_FILE=1; shift ;;
        -*) die "未知参数：$1" ;;
        *) [ -z "$IMAGE" ] || die "多余参数：$1"; IMAGE=$1; shift ;;
    esac
done

[ -n "$CMD" ] || die "用法：$0 {status|backup|flash <img>|rollback [img]} [--slot a|b] [--allow-file]"

active_slot() {
    s=$(getprop ro.boot.slot_suffix 2>/dev/null || true)
    [ -n "$s" ] || s=$(getprop ro.boot.slot 2>/dev/null || true)
    printf '%s' "$s"
}

want_slot() {
    if [ -n "$SLOT" ]; then
        case "$SLOT" in
            a|b|_a|_b) ;;
            *) die "--slot 只能是 a 或 b" ;;
        esac
        case "$SLOT" in _*) printf '%s' "$SLOT" ;; *) printf '_%s' "$SLOT" ;; esac
    else
        s=$(active_slot)
        [ -n "$s" ] || die "读不到当前槽位（ro.boot.slot_suffix 为空）；请用 --slot a|b 指定"
        printf '%s' "$s"
    fi
}

part_of() {
    p="$BY_NAME/boot$1"
    [ -e "$p" ] || die "找不到分区：$p"
    printf '%s' "$p"
}

is_block() { [ -b "$1" ]; }

part_size() {
    if is_block "$1"; then
        blockdev --getsize64 "$1" 2>/dev/null || die "无法读取分区大小：$1"
    else
        stat -c %s "$1" 2>/dev/null || die "无法读取文件大小：$1"
    fi
}

hash_of() { sha256sum "$1" | awk '{print $1}'; }

hash_of_bytes() {
    dd if="$1" bs="$CHUNK" count="$2" 2>/dev/null | sha256sum | awk '{print $1}'
}

looks_like_boot() {
    [ "$(head -c 8 "$1" 2>/dev/null)" = "ANDROID!" ] || return 1
}

check_target() {
    if is_block "$1"; then
        [ "$(id -u)" -eq 0 ] || die "写入 $1 需要 root（请用 su -c 运行）"
    else
        [ "$ALLOW_FILE" -eq 1 ] || die "$1 不是块设备；若只是演练流程请加 --allow-file"
        info "目标 $1 不是块设备：演练模式，不会碰任何分区"
    fi
}

do_status() {
    sfx=$(want_slot)
    part=$(part_of "$sfx")
    info "当前槽位：${sfx}（ro.boot.slot_suffix=$(active_slot)）"
    info "目标分区：$part"
    if is_block "$part"; then
        info "分区大小：$(part_size "$part") 字节"
        info "分区摘要：$(hash_of_bytes "$part" "$(part_size "$part")")"
    else
        info "分区大小：$(part_size "$part") 字节（普通文件）"
    fi
    if [ -d "$BACKUP_DIR" ]; then
        info "已有备份："
        ls -1t "$BACKUP_DIR"/boot"$sfx"-*.img 2>/dev/null | head -5 | sed 's/^/  /' || info "  （无）"
    else
        info "备份目录不存在：$BACKUP_DIR"
    fi
}

do_backup() {
    sfx=$(want_slot)
    part=$(part_of "$sfx")
    size=$(part_size "$part")
    check_target "$part"
    mkdir -p "$BACKUP_DIR"
    live=$(hash_of_bytes "$part" "$size")
    out="$BACKUP_DIR/boot$sfx-$(date -u +%Y%m%dT%H%M%SZ)-$(printf '%s' "$live" | cut -c1-12).img"
    dd if="$part" of="$out" bs="$CHUNK" count=$((size / CHUNK + (size % CHUNK > 0))) 2>/dev/null || die "备份写入失败"
    got=$(hash_of_bytes "$out" "$size")
    [ "$got" = "$live" ] || die "备份回测不一致：分区 $live / 备份 $got"
    printf '%s  %s\n' "$got" "$(basename "$out")" > "$out.sha256"
    printf '{"slot":"%s","partition":"%s","image":"%s","sha256":"%s","bytes":%s,"at":"%s"}\n' \
        "$sfx" "$part" "$(basename "$out")" "$got" "$size" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$out.json"
    info "备份完成：$out"
    info "备份 SHA256：$got（与分区实测一致）"
    printf '%s' "$out"
}

do_write() {
    src=$1
    require_magic=${2:-1}
    [ -f "$src" ] || die "找不到镜像：$src"
    if [ "$require_magic" -eq 1 ]; then
        looks_like_boot "$src" || die "$src 不是 Android boot 镜像（首 8 字节不是 ANDROID!）"
    else
        [ "$(stat -c %s "$src")" -eq "$(part_size "$(part_of "$(want_slot)")")" ] \
            || die "回滚要求备份大小与分区一致（$src）"
    fi
    sfx=$(want_slot)
    part=$(part_of "$sfx")
    psize=$(part_size "$part")
    isize=$(stat -c %s "$src")
    [ "$isize" -le "$psize" ] || die "镜像 $isize 字节大于分区 $psize 字节，拒绝写入"
    [ "$isize" -eq "$psize" ] || info "注意：镜像比分区小 $((psize - isize)) 字节，尾部保留旧内容"
    check_target "$part"
    act=$(active_slot)
    [ -z "$act" ] || [ "$sfx" = "$act" ] || info "注意：写入的 ${sfx} 不是当前活动槽位 ${act}"

    info "目标：$part（$psize 字节）  镜像：$src（$isize 字节）"
    if [ -d "$BACKUP_DIR" ] && ls "$BACKUP_DIR"/boot"$sfx"-*.img >/dev/null 2>&1; then
        info "已存在备份，跳过自动备份：$(ls -1t "$BACKUP_DIR"/boot"$sfx"-*.img | head -1)"
    else
        info "没有备份，先备份当前内容"
        do_backup >/dev/null
    fi

    isha=$(hash_of "$src")
    dd if="$src" of="$part" bs="$CHUNK" 2>/dev/null || die "写入失败"
    sync
    got=$(hash_of_bytes "$part" "$isize")
    [ "$got" = "$isha" ] || die "回读校验失败：镜像 $isha / 分区 $got（请立即 rollback）"
    info "写入完成，回读 SHA256 一致：$got"
    info "重启生效：reboot"
}

do_rollback() {
    sfx=$(want_slot)
    if [ -n "$IMAGE" ]; then
        src=$IMAGE
    else
        src=$(ls -1t "$BACKUP_DIR"/boot"$sfx"-*.img 2>/dev/null | head -1 || true)
        [ -n "$src" ] || die "$BACKUP_DIR 下没有 boot$sfx 的备份"
    fi
    [ -f "$src" ] || die "找不到备份：$src"
    if [ -f "$src.sha256" ]; then
        want=$(awk '{print $1}' "$src.sha256")
        got=$(hash_of "$src")
        [ "$want" = "$got" ] || die "备份文件摘要与记录不符（记录 $want / 实际 $got），拒绝回滚"
        info "备份摘要核对通过：$got"
    fi
    info "回滚：$src → $(part_of "$sfx")"
    do_write "$src" 0
}

case "$CMD" in
    status) do_status ;;
    backup) do_backup >/dev/null ;;
    flash) [ -n "$IMAGE" ] || die "flash 需要镜像路径"; do_write "$IMAGE" 1 ;;
    rollback) do_rollback ;;
    *) die "未知命令：$CMD" ;;
esac
