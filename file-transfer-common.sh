#!/bin/bash
# Shared helpers for Bash 4.2 / curl 7.29. Not an executable entry point.
export LC_ALL=C

die() { printf '错误: %s\n' "$*" >&2; exit 1; }
usage_error() { printf '参数错误: %s\n' "$*" >&2; exit 2; }
log() { printf '%s\n' "$*" >&2; }
need() { command -v "$1" >/dev/null 2>&1 || die "缺少命令: $1"; }
hash_file() { md5sum < "$1" | awk '{print $1}'; }
valid_hash() { [[ $1 =~ ^[[:xdigit:]]{32}$ ]]; }
valid_url() {
    # curl also enforces allowed protocols, including redirects.
    [[ $1 =~ ^https?://[^/?#[:space:]]+([/?][^[:space:]\|\<\>\"\']*)?$ ]] &&
        [[ $1 != *'#'* && $1 != *'@'* && $1 != *\\* && $1 != *[[:cntrl:]]* ]]
}
number() {
    [[ $2 =~ ^[0-9]+$ ]] || usage_error "$1 必须是非负整数"
    local value=$2
    while [[ ${#value} -gt 1 && $value == 0* ]]; do value=${value#0}; done
    [[ ${#value} -le 18 ]] || usage_error "$1 过大"
    NUMBER=$((10#$value))
}
chunk_bytes() {
    local value=${1^^} unit multiplier
    [[ $value =~ ^([0-9]+)(B|K|KB|KIB|M|MB|MIB|G|GB|GIB)?$ ]] || usage_error '分片大小格式无效，例如 90M、512KiB'
    unit=${BASH_REMATCH[2]}
    number '--chunk-size' "${BASH_REMATCH[1]}"
    case "$unit" in
        K|KB|KIB) multiplier=1024 ;;
        M|MB|MIB) multiplier=1048576 ;;
        G|GB|GIB) multiplier=1073741824 ;;
        *) multiplier=1 ;;
    esac
    (( NUMBER > 0 && NUMBER <= 9223372036854775807 / multiplier )) || usage_error '分片大小超出有效范围'
    CHUNK_BYTES=$((NUMBER * multiplier))
}
safe_name() {
    local value=${1//\\//} stem
    value=${value##*/}
    [[ $value =~ ^[A-Za-z]: ]] && value=${value:2}
    [[ -n $value && $value != . && $value != .. && $value != *[[:cntrl:]]* &&
       $value != *['<>:"|?*']* && $value != *' ' && $value != *. ]] || die '清单文件名不安全'
    stem=${value%%.*}
    case "${stem^^}" in
        CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]) die '清单文件名是 Windows 保留名称，无法跨平台恢复' ;;
    esac
    SAFE_NAME=$value
}
init_config() {
    AUTH=${FILE_TRANSFER_AUTH-${auth-}}
    URL=${FILE_TRANSFER_URL-${url-}}
    MANIFEST=${FILE_TRANSFER_MANIFEST-manifest.txt}
    CHUNK_SIZE=${FILE_TRANSFER_CHUNK_SIZE-90M}
    RETRIES=${FILE_TRANSFER_RETRIES-2}
    LOW_SPEED_TIME=${FILE_TRANSFER_LOW_SPEED_TIME-120}
    EXPIRES=${FILE_TRANSFER_EXPIRES-3600}
    OUTPUT=${FILE_TRANSFER_OUTPUT-}
    POSITIONAL=()
}
parse_args() {
    local mode=$1 key
    shift
    while (( $# )); do
        key=$1; shift
        case "$key" in
            -h|--help) usage; exit 0 ;;
            --) POSITIONAL+=("$@"); break ;;
            --auth|--retries|--output|--url|--manifest|--chunk-size|--expires|--low-speed-time)
                (( $# )) || usage_error "$key 缺少值"
                case "$mode:$key" in
                    push:--output|pull:--url|pull:--manifest|pull:--chunk-size|pull:--expires) usage_error "不支持参数 $key" ;;
                esac
                case "$key" in
                    --auth) AUTH=$1 ;; --retries) RETRIES=$1 ;; --output) OUTPUT=$1 ;;
                    --url) URL=$1 ;; --manifest) MANIFEST=$1 ;; --chunk-size) CHUNK_SIZE=$1 ;; --expires) EXPIRES=$1 ;;
                    --low-speed-time) LOW_SPEED_TIME=$1 ;;
                esac
                shift ;;
            -*) usage_error "未知参数 $key；以 - 开头的文件请使用 ./文件名 或 --" ;;
            *) POSITIONAL+=("$key") ;;
        esac
    done
    (( ${#POSITIONAL[@]} <= 1 )) || usage_error '只接受一个输入文件'
    number '--retries' "$RETRIES"; RETRIES=$NUMBER
    (( RETRIES <= 100 )) || usage_error '重试次数不能超过 100'
    number '--low-speed-time' "$LOW_SPEED_TIME"; LOW_SPEED_TIME=$NUMBER
    (( LOW_SPEED_TIME <= 2147483647 )) || usage_error '低速超时时间过大'
    [[ $AUTH != *[[:cntrl:]]* ]] || usage_error '认证值不能包含控制字符'
    local cmd
    for cmd in curl md5sum awk mktemp mv mkdir rm cat; do need "$cmd"; done
    # --http1.1 appeared in curl 7.33; stock 7.29 already uses HTTP/1.1.
    # Probe option support locally, without contacting any server.
    HTTP1_ARGS=()
    if curl -q --http1.1 --version >/dev/null 2>&1; then
        HTTP1_ARGS=(--http1.1)
    fi
}

# Writes response/body into $3. Only successful 2xx responses are accepted.
# No curl --retry: explicitly cover transport failures and corrupt chunks too.
request() {
    local method=$1 request_url=$2 destination=$3 source=${4-} status redirect origin current hop
    [[ $request_url =~ ^https?://[^/?#]+ ]] || return 1
    origin=${BASH_REMATCH[0],,}
    HTTP_STATUS=
    for ((hop=0; hop<=5; hop++)); do
        # HTTP/2 negotiation can fail on older client/proxy/server combinations.
        local args=(-q -g "${HTTP1_ARGS[@]}" -sS -f --connect-timeout 30
            --proto '=http,https' -o "$destination" -w $'%{http_code}\n%{redirect_url}')
        # Allow time for the upload service to finish processing a large PUT.
        (( LOW_SPEED_TIME == 0 )) || args+=(--speed-time "$LOW_SPEED_TIME" --speed-limit 30)
        [[ $request_url =~ ^https?://[^/?#]+ ]] || return 1
        current=${BASH_REMATCH[0],,}
        # Old curl versions can forward custom auth headers across origins with -L.
        [[ -z $AUTH || $current != "$origin" ]] || args+=(-H "Authorization: $AUTH")
        if [[ $method == PUT ]]; then
            args+=(-T "$source")
            (( EXPIRES == 0 )) || args+=(-H "X-Expiration-Seconds: $EXPIRES")
        fi
        if status=$(curl "${args[@]}" "$request_url"); then
            HTTP_STATUS=${status%%$'\n'*}
            HTTP_STATUS=${HTTP_STATUS%$'\r'}
            [[ $HTTP_STATUS =~ ^2[0-9][0-9]$ ]] && return 0
            [[ $method == GET && $HTTP_STATUS =~ ^30[12378]$ && $status == *$'\n'* ]] || return 1
            redirect=${status#*$'\n'}
            redirect=${redirect%$'\r'}
            valid_url "$redirect" || return 1
            [[ $request_url != https://* || $redirect == https://* ]] || return 1
            request_url=$redirect
        else
            HTTP_STATUS=${status%%$'\n'*}
            HTTP_STATUS=${HTTP_STATUS%$'\r'}
            return 1
        fi
    done
    return 1
}
retry_delay() {
    case "$HTTP_STATUS" in 401|403|404) return 1 ;; esac
    (( $1 < RETRIES )) || return 1
    log "请求失败，准备重试 ($(( $1 + 1 ))/$RETRIES)..."
    sleep "$(( $1 < 4 ? (1 << $1) : 16 ))"
}
