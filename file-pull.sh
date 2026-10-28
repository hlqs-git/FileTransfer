#!/bin/bash
# Linux download client: Bash 4.2+, curl 7.29+, GNU coreutils.
set -eo pipefail
umask 077
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/file-transfer-common.sh"
usage() {
    cat <<'EOF'
Usage: file-pull.sh [MANIFEST] [--output PATH] [--auth VALUE] [--retries 2]
                   [--low-speed-time 120]
Environment: FILE_TRANSFER_MANIFEST, FILE_TRANSFER_OUTPUT,
             FILE_TRANSFER_AUTH, FILE_TRANSFER_RETRIES
             FILE_TRANSFER_LOW_SPEED_TIME (seconds; 0 disables low-speed timeout)
Verified chunks are reused from the output directory's .file-transfer/ folder.
EOF
}
init_config
parse_args pull "$@"
(( ${#POSITIONAL[@]} == 0 )) || MANIFEST=${POSITIONAL[0]}
[[ -f $MANIFEST && -r $MANIFEST ]] || die '清单不存在或不可读'
for cmd in dirname basename wc sleep rmdir; do need "$cmd"; done
# Snapshot the manifest once so validation, state identity and transfer agree.
TEXT=$(cat -- "$MANIFEST")
HASH= NAME= VERSION= SIZE= CHUNK_SIZE= EXPIRES=
SEEN='|'
HASHES=(); URLS=()
while IFS= read -r line || [[ -n $line ]]; do
    line=${line%$'\r'}
    [[ -n $line ]] || continue
    if [[ $line == *'|'* ]]; then
        digest=${line%%|*}; link=${line#*|}
        valid_hash "$digest" && valid_url "$link" || die '清单包含无效分片 MD5 或链接'
        HASHES+=("${digest,,}"); URLS+=("$link")
    else
        [[ $line == *:* ]] || die '清单行格式错误'
        key=${line%%:*}; value=${line#*:}
        case "$key" in HASH|NAME|VERSION|SIZE|CHUNK_SIZE|EXPIRES) ;; *) die "未知清单字段: $key" ;; esac
        [[ $SEEN != *"|$key|"* ]] || die "清单包含重复字段: $key"
        SEEN+="$key|"
        case "$key" in
            HASH) HASH=${value,,} ;; NAME) NAME=$value ;;
            VERSION|SIZE|CHUNK_SIZE|EXPIRES)
                number "$key" "$value"
                printf -v "$key" '%s' "$NUMBER" ;;
        esac
    fi
done <<< "$TEXT"
valid_hash "$HASH" || die '清单缺少有效 HASH'
safe_name "$NAME"
[[ -n $OUTPUT ]] || OUTPUT=$SAFE_NAME
[[ -z $VERSION || $VERSION == 1 || $VERSION == 2 ]] || die '不支持的清单版本'
[[ -z $CHUNK_SIZE || $CHUNK_SIZE -gt 0 ]] || die '清单分片大小必须大于零'
if [[ $VERSION == 2 ]]; then
    [[ -n $SIZE && -n $CHUNK_SIZE && -n $EXPIRES ]] || die '版本 2 清单缺少元数据'
    expected=$((SIZE / CHUNK_SIZE + (SIZE % CHUNK_SIZE != 0)))
    (( expected == ${#HASHES[@]} )) || die '清单分片数量与文件大小不匹配'
fi
if (( ${#HASHES[@]} == 0 )); then
    [[ $HASH == d41d8cd98f00b204e9800998ecf8427e && ( -z $SIZE || $SIZE == 0 ) ]] || die '非空文件的清单缺少分片'
fi
[[ $EXPIRES != 0 ]] || log '提示: 一次性下载链接可能无法重试或恢复。'
[[ ! -d $OUTPUT ]] || die '输出路径不能是目录'
[[ ! -e $OUTPUT || ! $MANIFEST -ef $OUTPUT ]] || die '输出路径不能覆盖清单'
OUT_DIR=$(CDPATH= cd -- "$(dirname -- "$OUTPUT")" && pwd) || die '输出目录不存在'
OUT_NAME=$(basename -- "$OUTPUT")
[[ $OUT_NAME != . && $OUT_NAME != .. && $OUT_NAME != *[[:cntrl:]]* ]] || die '输出文件名无效'
TARGET="$OUT_DIR/$OUT_NAME"
FINGERPRINT=$(printf '%s\n%s' "$TEXT" "$OUT_NAME" | md5sum | awk '{print $1}')
ROOT="$OUT_DIR/.file-transfer"
[[ ! -L $ROOT ]] || die '状态目录不能是符号链接'
mkdir -p -- "$ROOT"
STATE="$ROOT/shell-$FINGERPRINT"
[[ ! -L $STATE ]] || die '分片状态目录不能是符号链接'
mkdir -p -- "$STATE"
LOCK="$STATE/lock"
mkdir -- "$LOCK" 2>/dev/null || die "同一下载已在运行或上次异常退出遗留锁: $LOCK；确认无任务后可手动 rmdir"
ASSEMBLY=
cleanup() {
    [[ -z $ASSEMBLY ]] || rm -f -- "$ASSEMBLY"
    rm -f -- "$STATE/incoming"
    rmdir -- "$LOCK" 2>/dev/null || :
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
PARTS=()
for ((idx=0; idx<${#HASHES[@]}; idx++)); do
    printf -v part '%s/p_%012d' "$STATE" "$idx"
    [[ ! -L $part ]] || die '缓存分片不能是符号链接'
    PARTS+=("$part")
    if [[ -f $part && $(hash_file "$part") == "${HASHES[idx]}" ]]; then
        log "分片 $((idx + 1))/${#HASHES[@]} 已校验，复用"
        continue
    fi
    attempt=0
    while :; do
        rm -f -- "$STATE/incoming"
        if request GET "${URLS[idx]}" "$STATE/incoming" && [[ $(hash_file "$STATE/incoming") == "${HASHES[idx]}" ]]; then
            mv -fT -- "$STATE/incoming" "$part"
            break
        fi
        rm -f -- "$STATE/incoming"
        retry_delay "$attempt" || die "分片 $((idx + 1)) 下载或校验失败（HTTP ${HTTP_STATUS:-未知}）；已验证分片保留供重试"
        attempt=$((attempt + 1))
    done
    log "分片 $((idx + 1))/${#HASHES[@]} 下载完成"
done
log '合并并校验完整文件...'
ASSEMBLY=$(mktemp "$OUT_DIR/.file-transfer-output.XXXXXXXX")
for part in "${PARTS[@]}"; do cat -- "$part" >> "$ASSEMBLY"; done
[[ $(hash_file "$ASSEMBLY") == "$HASH" ]] || die '最终文件 MD5 不匹配，原目标未替换'
[[ -z $SIZE || $(wc -c < "$ASSEMBLY") -eq $SIZE ]] || die '最终文件大小不匹配，原目标未替换'
mv -fT -- "$ASSEMBLY" "$TARGET"
ASSEMBLY=
for part in "${PARTS[@]}"; do rm -f -- "$part"; done
# Remove only this invocation's files, never recursively delete download state.
cleanup
trap - EXIT
rmdir -- "$STATE" 2>/dev/null || :
rmdir -- "$ROOT" 2>/dev/null || :
log '恢复完成，完整文件校验通过。'
printf '%s\n' "$OUTPUT"
