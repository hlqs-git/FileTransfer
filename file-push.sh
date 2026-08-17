#!/bin/bash
# --- 配置 ---
AUTH="$auth"
URL="$url"
CHUNK_SIZE="90M" # 留出 10MB 余量给 Cloudflare 头部

[ -z "$1" ] && { echo "Usage: $0 <file_name>"; exit 1; }
FILE="$1"
MANIFEST="manifest.txt"

echo "============================================"
echo "������ 准备大文件分片上传: $FILE"
echo "============================================"

# 1. 计算总 MD5
echo "[1/3] 计算全局 MD5..."
TOTAL_MD5=$(md5sum "$FILE" | awk '{print $1}')
echo "HASH:$TOTAL_MD5" > "$MANIFEST"
echo "NAME:$FILE" >> "$MANIFEST"

# 2. 分片
echo "[2/3] 物理分片中..."
split -b "$CHUNK_SIZE" -d -a 3 "$FILE" "part_"

# 3. 逐个计算分片 MD5 并上传
echo "[3/3] 顺序上传分片..."
for p in $(ls part_[0-9]* | sort -n); do
    PMD5=$(md5sum "$p" | awk '{print $1}')
    echo -n "正在上传 $p (MD5: $PMD5) ... "
    
    # 使用 --connect-timeout 应对高延迟
    RESP=$(curl -s --connect-timeout 30 -H "Authorization: $AUTH" "$URL" -T "$p")
    DL_LINK=$(echo "$RESP" | grep -o "http://r2.gmyj.org/[^ ]*\.bin")
    
    if [ -n "$DL_LINK" ]; then
        echo "成功"
        # 记录格式: PART_MD5 | URL
        echo "$PMD5|$DL_LINK" >> "$MANIFEST"
    else
        echo "失败！响应: $RESP"
        exit 1
    fi
done

rm -f part_[0-9]*
echo "✅ 上传完成！清单已生成至 $MANIFEST"
