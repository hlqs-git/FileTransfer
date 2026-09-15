#!/bin/bash

MANIFEST="manifest.txt"
# 1. 自动开启 BBR 优化（如果未开启）
if [[ $(sysctl net.ipv4.tcp_congestion_control | awk '{print $3}') != "bbr" ]]; then
    echo "提示: 检测到未开启 BBR，正在尝试临时优化网络..."
    sudo sysctl -w net.core.default_qdisc=fq > /dev/null 2>&1
    sudo sysctl -w net.ipv4.tcp_congestion_control=bbr > /dev/null 2>&1
fi

[ ! -f "$MANIFEST" ] && { echo "错误: 清单不存在"; exit 1; }

MANIFEST_NAME=$(grep '^NAME:' "$MANIFEST" | cut -d':' -f2-)
ORIG_NAME=$(basename -- "$MANIFEST_NAME")
ORIG_MD5=$(grep '^HASH:' "$MANIFEST" | cut -d':' -f2-)
TMP_DIR="restore_work"
mkdir -p "$TMP_DIR" && cd "$TMP_DIR"

echo "============================================"
echo "📥 启动安全恢复: $ORIG_NAME"
echo "============================================"

idx=0
# 仅解析 MD5|URL 格式的行
grep "|" "../$MANIFEST" | while read -r line; do
    idx=$((idx + 1))
    T_MD5=$(echo "$line" | cut -d'|' -f1)
    URL=$(echo "$line" | cut -d'|' -f2)
    OUT=$(printf "p_%03d.bin" $idx)

    echo -n "[分片 $idx] 正在传输... "
    # 使用最通用的短参数：
    # -L (跟随重定向), -k (忽略证书错误), -f (HTTP错误时报错)
    # -y 10 -Y 30 (低速限时重试)
    #curl -L -k -f -y 10 -Y 30 --connect-timeout 60 -o "$OUT" "$URL"
    curl -L -k -f -H "Authorization: hlqs" -y 10 -Y 30 --connect-timeout 60 -o "$OUT" "$URL"

    if [ $? -ne 0 ]; then
        echo "❌ 失败！链接可能已在本次请求中失效。"
        exit 1
    fi

    # 下载完立刻校验，防止坏包
    C_MD5=$(md5sum "$OUT" | awk '{print $1}')
    if [ "$T_MD5" != "$C_MD5" ]; then
        echo "❌ 校验失败！分片损坏。"
        exit 1
    fi
    echo "OK"
done

echo "正在合并并验证总文件..."
cat p_*.bin > "../$ORIG_NAME"
cd ..
FINAL_MD5=$(md5sum -- "$ORIG_NAME" | awk '{print $1}')

if [ "$FINAL_MD5" == "$ORIG_MD5" ]; then
    echo "✅ 成功恢复：$ORIG_NAME"
    rm -rf "$TMP_DIR"
else
    echo "❌ 最终文件 MD5 不匹配！"
    exit 1
fi
