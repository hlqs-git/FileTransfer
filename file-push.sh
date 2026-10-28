#!/bin/bash
# Linux upload client: Bash 4.2+, curl 7.29+, GNU coreutils.
# Bash 4.2 treats empty arrays as unset with nounset, including empty files.
set -eo pipefail
umask 077
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
. "$SCRIPT_DIR/file-transfer-common.sh"
usage() {
    cat <<'EOF'
Usage: file-push.sh FILE [--url URL] [--auth VALUE] [--manifest PATH]
                         [--chunk-size 90M] [--retries 2] [--expires 3600]
                         [--low-speed-time 120]
Environment: FILE_TRANSFER_URL, FILE_TRANSFER_AUTH, FILE_TRANSFER_MANIFEST,
             FILE_TRANSFER_CHUNK_SIZE, FILE_TRANSFER_RETRIES, FILE_TRANSFER_EXPIRES
             FILE_TRANSFER_LOW_SPEED_TIME (seconds; 0 disables low-speed timeout)
Legacy upload settings: url, auth. Use -- before a filename starting with '-'.
EOF
}
init_config
parse_args push "$@"
(( ${#POSITIONAL[@]} == 1 )) || { usage >&2; exit 2; }
FILE=${POSITIONAL[0]}
[[ -f $FILE && -r $FILE ]] || die '源文件不存在、不可读或不是普通文件'
valid_url "$URL" || usage_error '请通过 --url 或 FILE_TRANSFER_URL 配置 HTTP/HTTPS 上传地址'
chunk_bytes "$CHUNK_SIZE"
number '--expires' "$EXPIRES"; EXPIRES=$NUMBER
(( EXPIRES <= 2147483647 )) || usage_error '链接有效期过大'
(( EXPIRES != 0 )) || log '提示: 一次性链接可能在下载后失效，无法保证重试及恢复。'
for cmd in split grep wc dirname basename sleep; do need "$cmd"; done
safe_name "$(basename -- "$FILE")"
FILE_NAME=$SAFE_NAME
MANIFEST_DIR=$(dirname -- "$MANIFEST")
[[ -d $MANIFEST_DIR && ! -d $MANIFEST ]] || die '清单输出目录不存在或清单路径是目录'
# Never overwrite the input with a manifest.
[[ ! -e $MANIFEST || ! $FILE -ef $MANIFEST ]] || die '清单路径不能与源文件相同'
WORK=
PENDING=
cleanup() {
    [[ -z $PENDING ]] || rm -f -- "$PENDING"
    [[ -z $WORK ]] || rm -rf -- "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
WORK=$(mktemp -d "${TMPDIR:-/tmp}/file-push.XXXXXXXX")
PENDING=$(mktemp "$MANIFEST_DIR/.file-transfer-manifest.XXXXXXXX")
log '[1/3] 计算文件 MD5...'
TOTAL_MD5=$(hash_file "$FILE")
SIZE=$(wc -c < "$FILE"); SIZE=${SIZE//[[:space:]]/}
printf 'HASH:%s\nNAME:%s\nVERSION:2\nSIZE:%s\nCHUNK_SIZE:%s\nEXPIRES:%s\n' \
    "$TOTAL_MD5" "$FILE_NAME" "$SIZE" "$CHUNK_BYTES" "$EXPIRES" > "$PENDING"
log '[2/3] 在独立临时目录分片...'
# Fixed-width names sort correctly and are private to this invocation.
split -b "$CHUNK_BYTES" -d -a 12 -- "$FILE" "$WORK/part_"
shopt -s nullglob
PARTS=("$WORK"/part_*)
log "[3/3] 顺序上传 ${#PARTS[@]} 个分片..."
idx=0
for part in "${PARTS[@]}"; do
    idx=$((idx + 1))
    PMD5=$(hash_file "$part")
    attempt=0
    while :; do
        if request PUT "$URL" "$WORK/response" "$part"; then
            # Accept one HTTP(S) URL, independent of deployment domain/suffix.
            mapfile -t LINKS < <(grep -Eo "https?://[^[:space:]<>\"']+" "$WORK/response" || :)
            (( ${#LINKS[@]} == 1 )) || die "分片 $idx 的上传响应必须包含唯一下载链接"
            valid_url "${LINKS[0]}" || die "分片 $idx 返回无效下载链接"
            printf '%s|%s\n' "$PMD5" "${LINKS[0]}" >> "$PENDING"
            break
        fi
        retry_delay "$attempt" || die "分片 $idx 上传失败（HTTP ${HTTP_STATUS:-未知}），原清单未替换"
        attempt=$((attempt + 1))
    done
    log "分片 $idx/${#PARTS[@]} 上传完成"
done
# Detect a source changing during hashing/splitting before publishing links.
[[ $(hash_file "$FILE") == "$TOTAL_MD5" && $(wc -c < "$FILE") -eq $SIZE ]] || die '源文件在上传期间发生变化，未发布清单'
# The concatenated snapshot must also match the manifest, even if the source changed back.
SNAPSHOT_MD5=$( { for part in "${PARTS[@]}"; do cat -- "$part"; done; } | md5sum | awk '{print $1}')
[[ $SNAPSHOT_MD5 == "$TOTAL_MD5" ]] || die '分片快照与源文件校验不一致，未发布清单'
mv -fT -- "$PENDING" "$MANIFEST"
PENDING=
log '上传完成，清单已保存。'
printf '%s\n' "$MANIFEST"
