#!/bin/sh

set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

run_shell() {
    if [ -n "${BUSYBOX:-}" ]; then
        "$BUSYBOX" sh "$@"
    else
        bash "$@"
    fi
}

prepare_script() {
    script_name=$1
    destination=$2
    cp "$PROJECT_DIR/$script_name" "$destination"
    if [ -n "${BUSYBOX:-}" ]; then
        "$BUSYBOX" dos2unix "$destination" >/dev/null 2>&1
    else
        sed -i 's/\r$//' "$destination"
    fi
}

test_pull_restores_legacy_absolute_name_as_basename() {
    for file_name in archive.tar.gz -b; do
        case_dir="$TEST_DIR/pull-$file_name"
        mkdir -p "$case_dir/bin"
        prepare_script file-pull.sh "$case_dir/file-pull.sh"

        cat > "$case_dir/bin/sysctl" <<'EOF'
#!/bin/sh
echo 'net.ipv4.tcp_congestion_control = bbr'
EOF
        cat > "$case_dir/bin/curl" <<'EOF'
#!/bin/sh
while [ "$#" -gt 0 ]; do
    if [ "$1" = "-o" ]; then
        output=$2
        shift 2
    else
        shift
    fi
done
printf hello > "$output"
EOF
        chmod +x "$case_dir/bin/sysctl" "$case_dir/bin/curl"

        cat > "$case_dir/manifest.txt" <<EOF
HASH:5d41402abc4b2a76b9719d911017c592
NAME:/mnt/source/$file_name
5d41402abc4b2a76b9719d911017c592|https://example.test/part.bin
EOF

        (
            cd "$case_dir"
            PATH="$case_dir/bin:$PATH" run_shell ./file-pull.sh >/dev/null
        )

        [ "$(cat "$case_dir/$file_name")" = "hello" ] || {
            echo "FAIL: file-pull.sh did not safely restore $file_name" >&2
            return 1
        }
    done
}

test_push_records_only_basename() {
    case_dir="$TEST_DIR/push"
    mkdir -p "$case_dir/bin" "$case_dir/source"
    prepare_script file-push.sh "$case_dir/file-push.sh"

    cat > "$case_dir/run-push.sh" <<'EOF'
#!/bin/sh
split() {
    cp "$6" "${7}000"
}
curl() {
    echo 'http://r2.gmyj.org/test.bin'
}
. ./file-push.sh "$1"
EOF
    printf hello > "$case_dir/source/archive.tar.gz"

    (
        cd "$case_dir"
        auth=test url=https://example.test \
            run_shell ./run-push.sh "$case_dir/source/archive.tar.gz" >/dev/null
    )

    actual_name=$(grep '^NAME:' "$case_dir/manifest.txt")
    [ "$actual_name" = 'NAME:archive.tar.gz' ] || {
        echo "FAIL: expected portable NAME entry, got: $actual_name" >&2
        return 1
    }
    grep -q '^5d41402abc4b2a76b9719d911017c592|http://r2.gmyj.org/test.bin$' \
        "$case_dir/manifest.txt" || {
        echo 'FAIL: file-push.sh did not complete a usable manifest' >&2
        return 1
    }
}

test_pull_restores_legacy_absolute_name_as_basename
test_push_records_only_basename
echo 'All file transfer tests passed.'
