#!/bin/bash
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TEST_DIR=$(mktemp -d)
trap 'rm -rf -- "$TEST_DIR"' EXIT
mkdir -p "$TEST_DIR/bin"
cat > "$TEST_DIR/bin/curl" <<'EOF'
#!/bin/bash
set -eu
output= upload= url= http1=0 speed_time=disabled
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) output=$2; shift 2 ;;
        -T) upload=$2; shift 2 ;;
        -w) shift 2 ;;
        --http1.1)
            [ "${LEGACY_CURL:-0}" != 1 ] || { echo 'unknown option --http1.1' >&2; exit 2; }
            http1=1; shift ;;
        --version) printf 'curl fixture\n'; exit 0 ;;
        --speed-time) speed_time=$2; shift 2 ;;
        -H) printf '%s\n' "$2" >> "$CASE/headers"; shift 2 ;;
        --connect-timeout|--max-time|--speed-time|--speed-limit|--proto|--proto-redir) shift 2 ;;
        -k|--insecure) exit 99 ;;
        http://*|https://*) url=$1; shift ;;
        *) shift ;;
    esac
done
if [ "${REQUIRE_HTTP1:-0}" = 1 ] && [ "$http1" != 1 ]; then
    echo 'simulated HTTP/2 PROTOCOL_ERROR' >&2
    exit 92
fi
if [ -n "${EXPECT_SPEED_TIME:-}" ] && [ "$speed_time" != "$EXPECT_SPEED_TIME" ]; then
    echo 'simulated low-speed timeout policy mismatch' >&2
    exit 28
fi
printf '%s\n' "$url" >> "$CASE/calls"
n=$(wc -l < "$CASE/calls")
if [ "${FAIL_ALWAYS:-0}" = 1 ] || [ "$n" -le "${FAIL_FIRST:-0}" ]; then
    [ -z "$output" ] || printf broken > "$output"
    exit 22
fi
if [ -n "$upload" ]; then
    if [ "${AMBIGUOUS:-0}" = 1 ]; then
        printf 'https://download.example/one https://download.example/two\n' > "$output"
    else
        printf 'https://download.example/chunk/%s?token=a&b=c\n' "$n" > "$output"
    fi
else
    case "$url" in
        */one) printf hello > "$output" ;;
        */two) printf world > "$output" ;;
        *) printf hello > "$output" ;;
    esac
fi
printf 200
EOF
chmod +x "$TEST_DIR/bin/curl"
export PATH="$TEST_DIR/bin:$PATH"
export FILE_TRANSFER_RETRIES=0 FILE_TRANSFER_URL=https://upload.example FILE_TRANSFER_AUTH=test-token
unset auth url || :
setup() {
    CASE="$TEST_DIR/$1"; export CASE
    mkdir -p "$CASE"
    cd "$CASE"
    unset FAIL_ALWAYS FAIL_FIRST AMBIGUOUS REQUIRE_HTTP1 EXPECT_SPEED_TIME FILE_TRANSFER_LOW_SPEED_TIME LEGACY_CURL
}
push() { bash "$PROJECT_DIR/file-push.sh" "$@" >stdout 2>stderr; }
pull() { bash "$PROJECT_DIR/file-pull.sh" "$@" >stdout 2>stderr; }
fail() { echo "FAIL: $*" >&2; exit 1; }
manifest() {
    printf 'HASH:fc5e038d38a57032085441e7fe7010b0\nNAME:archive.bin\n'
    printf '5d41402abc4b2a76b9719d911017c592|https://download.example/one\n'
    printf '7d793037a0760186574b0282f2f435e7|https://download.example/two\n'
}
test_push_failure_preserves_manifest() {
    setup push-failure
    printf hello > source
    printf previous > manifest.txt
    printf unrelated > part_000
    FAIL_ALWAYS=1; export FAIL_ALWAYS
    if push source; then fail 'upload failure must return nonzero'; fi
    [ "$(cat manifest.txt)" = previous ] || fail 'old manifest overwritten'
    [ "$(cat part_000)" = unrelated ] || fail 'unrelated part deleted'
}
test_push_options_retry_and_chunks() {
    setup push-options
    mkdir 'source dir'
    printf helloworld > 'source dir/-data file'
    printf unrelated > part_999
    FAIL_FIRST=1; export FAIL_FIRST
    push 'source dir/-data file' --chunk-size 5 --retries 1 --manifest 'custom list' --expires 120 || fail 'configured upload failed'
    grep -q '^NAME:-data file$' 'custom list' || fail 'basename missing'
    [ "$(grep -c '|' 'custom list')" = 2 ] || fail 'wrong number of chunks'
    grep -q '^HASH:fc5e038d38a57032085441e7fe7010b0$' 'custom list' || fail 'wrong file hash'
    grep -q 'https://download.example/chunk/' 'custom list' || fail 'download URL lost'
    grep -q '^X-Expiration-Seconds: 120$' headers || fail 'expiration missing'
    grep -q '^Authorization: test-token$' headers || fail 'auth missing'
    [ "$(cat part_999)" = unrelated ] || fail 'old part touched'
}
test_ambiguous_response_rejected() {
    setup push-ambiguous
    printf hello > source
    AMBIGUOUS=1; export AMBIGUOUS
    if push source; then fail 'ambiguous response accepted'; fi
    [ ! -e manifest.txt ] || fail 'failed upload published manifest'
}
test_pull_failure_preserves_target_and_resumes() {
    setup pull-resume
    manifest > manifest.txt
    printf previous > archive.bin
    mkdir bin
    cat > bin/curl <<EOF
#!/bin/bash
case "\$*" in */two*) exit 22 ;; esac
exec "$TEST_DIR/bin/curl" "\$@"
EOF
    chmod +x bin/curl
    if PATH="$CASE/bin:$PATH" pull; then fail 'download failure accepted'; fi
    [ "$(cat archive.bin)" = previous ] || fail 'target overwritten on failure'
    : > calls
    pull || fail 'resume failed'
    [ "$(cat archive.bin)" = helloworld ] || fail 'wrong resumed content'
    [ "$(wc -l < calls)" -eq 1 ] || fail 'verified chunk not reused'
}
test_pull_final_hash_failure() {
    setup pull-hash
    manifest | sed 's/fc5e038d38a57032085441e7fe7010b0/00000000000000000000000000000000/' > manifest.txt
    printf previous > archive.bin
    if pull; then fail 'bad final hash accepted'; fi
    [ "$(cat archive.bin)" = previous ] || fail 'target overwritten after hash mismatch'
}
test_legacy_and_python_manifests() {
    for name in '/mnt/source/-data file' 'C:\source\-data file'; do
        setup "legacy-$RANDOM"
        {
            manifest | sed '/^NAME:/d'
            printf 'NAME:%s\nVERSION:2\nSIZE:10\nCHUNK_SIZE:5\nEXPIRES:3600\n' "$name"
        } | sed 's/$/\r/' > manifest.txt
        pull || fail 'legacy/Python manifest rejected'
        [ "$(cat -- './-data file')" = helloworld ] || fail 'unsafe basename handling'
    done
}
test_bad_manifest_before_network() {
    for bad in 'bad|https://download.example/one' '5d41402abc4b2a76b9719d911017c592|file:///etc/passwd' 'NAME:..' 'HASH:00000000000000000000000000000000'; do
        setup "invalid-$RANDOM"
        { manifest; printf '%s\n' "$bad"; } > manifest.txt
        if pull; then fail 'invalid manifest accepted'; fi
        [ ! -e calls ] || fail 'network used before validating manifest'
    done
}
test_empty_roundtrip() {
    setup empty
    : > source
    push source || fail 'empty upload failed'
    [ ! -e calls ] || fail 'empty upload used network'
    pull manifest.txt --output restored || fail 'empty restore failed'
    [ -f restored ] && [ ! -s restored ] || fail 'empty restore is not empty'
}
test_cli_validation() {
    setup validation
    printf hello > source
    if push source --retries invalid; then fail 'invalid retry accepted'; fi
    [ ! -e calls ] || fail 'invalid retry used network'
    if push source --chunk-size 0; then fail 'zero chunk size accepted'; fi
    if push source --url file:///tmp/out; then fail 'non-HTTP upload accepted'; fi
    printf hello > CON
    if push CON; then fail 'Windows reserved name produced incompatible manifest'; fi
}
test_http1_and_low_speed_policy() {
    setup transport-policy
    printf hello > source
    REQUIRE_HTTP1=1 EXPECT_SPEED_TIME=120; export REQUIRE_HTTP1 EXPECT_SPEED_TIME
    push source || fail 'HTTP/1.1 upload with relaxed default timeout failed'
    EXPECT_SPEED_TIME=180; export EXPECT_SPEED_TIME
    pull manifest.txt --output restored --low-speed-time 180 || fail 'download timeout override failed'
    [ "$(cat restored)" = hello ] || fail 'wrong downloaded content'
    EXPECT_SPEED_TIME=disabled FILE_TRANSFER_LOW_SPEED_TIME=0
    export EXPECT_SPEED_TIME FILE_TRANSFER_LOW_SPEED_TIME
    push source || fail 'disabling low-speed protection failed'
    EXPECT_SPEED_TIME=45; export EXPECT_SPEED_TIME
    push source --low-speed-time 45 || fail 'CLI timeout must override environment'
    : > calls
    if pull --low-speed-time invalid; then fail 'invalid timeout accepted'; fi
    if push source --low-speed-time -1; then fail 'negative timeout accepted'; fi
    [ ! -s calls ] || fail 'invalid timeout used network'
}
test_legacy_curl_without_http1_option() {
    setup legacy-curl
    printf hello > source
    LEGACY_CURL=1; export LEGACY_CURL
    push source || fail 'curl 7.29 upload must not receive unsupported --http1.1'
    pull manifest.txt --output restored || fail 'curl 7.29 download must not receive unsupported --http1.1'
    [ "$(cat restored)" = hello ] || fail 'legacy curl content mismatch'
}
for test in test_legacy_curl_without_http1_option test_http1_and_low_speed_policy test_push_failure_preserves_manifest test_push_options_retry_and_chunks test_ambiguous_response_rejected test_pull_failure_preserves_target_and_resumes test_pull_final_hash_failure test_legacy_and_python_manifests test_bad_manifest_before_network test_empty_roundtrip test_cli_validation; do
    "$test"
    echo "PASS: $test"
done
echo 'All file transfer tests passed.'
