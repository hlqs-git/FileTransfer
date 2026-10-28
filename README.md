# FileTransfer

一个面向 Cloudflare R2 上传服务的大文件分片传输工具。Python 客户端支持 Windows 和 Linux，可并发上传、并发下载、自动重试、MD5 校验。旧 Linux 设备可使用无需 Python 的 Shell 客户端，支持顺序传输、自动重试及已校验分片恢复；两种客户端清单互通。

## Python 客户端环境要求

- Python 3.10 或更高版本
- 只使用 Python 标准库，无需安装第三方依赖
- 服务端兼容 [`bashupload-r2`](https://github.com/hlqs-git/bashupload-r2) 的 PUT 上传接口

## 快速开始

Windows PowerShell：

```powershell
$env:FILE_TRANSFER_URL = "https://r2.example.com"
$env:FILE_TRANSFER_AUTH = "your-token"
python .\file-transfer.py push C:\path\archive.zip
python .\file-transfer.py pull .\manifest.txt
```

Linux：

```bash
export FILE_TRANSFER_URL="https://r2.example.com"
export FILE_TRANSFER_AUTH="your-token"
python3 ./file-transfer.py push /path/archive.tar.gz
python3 ./file-transfer.py pull ./manifest.txt
```

上传成功后默认生成 `manifest.txt`。将该清单复制到目标机器，再执行 `pull` 即可恢复原文件。

命令行参数优先于同名环境变量。例如：

```powershell
python .\file-transfer.py push .\archive.zip --url https://r2.example.com --auth your-token
python .\file-transfer.py pull .\manifest.txt --output .\restored.zip --workers 8
```

未启用认证的服务端可以省略 `--auth` 和 `FILE_TRANSFER_AUTH`。

## 命令参数

### `push`

```text
python file-transfer.py push FILE [options]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `FILE` | 必填 | 要上传的文件 |
| `--url URL` | `FILE_TRANSFER_URL` | HTTP/HTTPS 上传地址；未配置时退出码为 2 |
| `--auth VALUE` | `FILE_TRANSFER_AUTH` | 可选的 `Authorization` 请求头值 |
| `--manifest PATH` | `manifest.txt` | 输出清单路径 |
| `--chunk-size SIZE` | `90M` | 分片大小，支持 `B`、`K/KiB`、`M/MiB`、`G/GiB` |
| `--workers N` | `4` | 并发数，范围 1–16 |
| `--retries N` | `2` | 每个请求失败后的额外重试次数；默认共尝试 3 次 |
| `--expires SECONDS` | `3600` | 下载链接有效期，默认一小时 |

`--expires 0` 不发送有效期请求头，服务端会生成一次性链接。一次下载后对象可能立即删除，因此重试、断点续传或多次下载都可能失败；只在明确需要一次性下载时使用。

各分片在同一次命令中会独立重试，但上传目前不能跨命令续传。只有全部分片上传成功后才会原子写入清单，避免发布不完整清单。

### `pull`

```text
python file-transfer.py pull [MANIFEST] [options]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `MANIFEST` | `manifest.txt` | 下载清单路径 |
| `--output PATH` | 清单中的文件名 | 恢复文件的保存路径 |
| `--auth VALUE` | `FILE_TRANSFER_AUTH` | 下载地址需要认证时使用 |
| `--workers N` | `4` | 并发数，范围 1–16 |
| `--retries N` | `2` | 每个请求失败后的额外重试次数；默认共尝试 3 次 |

下载完成且分片 MD5 正确后，分片保存在输出目录下的 `.file-transfer/` 状态目录中。命令中断后重新执行相同清单，会复用已验证的完整分片；损坏或不完整的分片会重新下载。最终文件先在临时文件中组装和校验，再原子替换目标文件，因此失败不会破坏已有目标文件。

## 清单和兼容性

新版清单在原有 `HASH`、`NAME` 和 `MD5|URL` 行之外增加版本、文件大小、分片大小和链接有效期。Python 客户端仍可读取旧版清单，包括旧清单中的 Linux 或 Windows 绝对路径；恢复时只采用安全的文件名部分。

清单包含临时下载链接，应像敏感数据一样妥善保管。链接受服务端的有效期或下载次数限制，并非永久地址。

## Linux Shell 方案（无需 Python）

旧设备可以直接使用 Shell 客户端，无需安装或升级 Python。兼容基线为 **Bash 4.2、curl 7.29 和 GNU coreutils**，面向 CentOS 7/8、Rocky Linux 和 Ubuntu。需要 `curl`、`md5sum`、`split`、`awk`、`grep`、`mktemp` 等常见系统工具。

请将 `file-push.sh`、`file-pull.sh`、`file-transfer-common.sh` 放在同一目录。使用 Bash 执行，不要使用 `sh file-push.sh`。以下命令在文件所在目录运行：

```bash
chmod +x file-push.sh file-pull.sh
export FILE_TRANSFER_URL="https://r2.example.com"
export FILE_TRANSFER_AUTH="your-token"
./file-push.sh /path/archive.tar.gz
./file-pull.sh
```

也可以指定参数，参数优先于环境变量：

```bash
./file-push.sh '/path/archive file.tar.gz' \
  --manifest ./archive.manifest --chunk-size 90M --retries 2 --expires 3600
./file-pull.sh ./archive.manifest --output /data/archive.tar.gz --retries 2
```

| 参数 | 上传 | 下载 | 默认值 / 环境变量 |
| --- | --- | --- | --- |
| 输入文件 | 必填 | 可选清单路径 | 下载默认 `manifest.txt`，也可设置 `FILE_TRANSFER_MANIFEST` |
| `--url` | 支持 | — | `FILE_TRANSFER_URL`，未设置时兼容旧 `url` |
| `--auth` | 支持 | 支持 | `FILE_TRANSFER_AUTH`，未设置时兼容旧 `auth`；可传空字符串关闭认证 |
| `--manifest` | 支持 | 使用位置参数 | `manifest.txt` / `FILE_TRANSFER_MANIFEST` |
| `--chunk-size` | 支持 | — | `90M` / `FILE_TRANSFER_CHUNK_SIZE`；支持字节及 K/KB/KiB、M/MB/MiB、G/GB/GiB，按 1024 换算 |
| `--retries` | 支持 | 支持 | `2` / `FILE_TRANSFER_RETRIES`；额外重试次数，范围 0–100 |
| `--low-speed-time` | 支持 | 支持 | `120` / `FILE_TRANSFER_LOW_SPEED_TIME`；低速超时秒数，范围 0–2147483647，0 表示关闭低速检测 |
| `--expires` | 支持 | — | `3600` / `FILE_TRANSFER_EXPIRES`；范围 0–2147483647 秒 |
| `--output` | — | 支持 | 清单文件名 / `FILE_TRANSFER_OUTPUT` |

上传和下载均为**顺序传输，固定使用 HTTP/1.1**，避免部分旧设备或链路协商 HTTP/2 后出现 `PROTOCOL_ERROR`。每个请求连接超时为 30 秒，默认连续 120 秒低于 30 字节/秒会失败；`--low-speed-time` 可调整该窗口，0 表示关闭低速检测（连接超时仍生效，但不再限制传输停滞时间）。总传输耗时超过该窗口并不必然失败，低速检测针对持续低速，不是总时长限制。重试等待为 1、2、4、8、16 秒，之后保持 16 秒。HTTP 401、403、404 立即失败，不重复请求；下载分片校验不匹配也受重试次数限制。上传响应必须包含唯一的 HTTP/HTTPS 下载链接，不再绑定域名或 `.bin` 后缀。

如果大分片上传结束后服务端处理较慢，可延长低速等待窗口后重试：

```bash
./file-push.sh zabbix.tar.gz --low-speed-time 300
```

脚本会先离线检测 curl 是否支持 `--http1.1`，支持时显式指定；CentOS 7 原版 curl 7.29 不支持该选项，沿用其默认 HTTP/1.1，避免未知参数错误。该选项从 curl 7.33.0 起提供，参见 [curl 官方选项历史](https://curl.se/docs/optionsall.html)。

上传采用独立临时目录进行物理分片，磁盘需要容纳一份源文件大小的分片；结束时清理临时分片。清单仅在所有分片成功、源文件与分片快照校验一致后才替换，失败保留原清单。上传暂不支持跨命令续传；已经上传的远程对象由服务端有效期管理。清单和输出目录需提前创建。

下载支持旧版清单、Python 版本 2 清单及 CRLF 换行；旧清单中的 Linux/Windows 路径仅取文件名。重新运行相同清单及输出文件时，会校验并复用输出目录 `.file-transfer/shell-*` 中的完整分片；损坏分片重新下载，不使用 HTTP 字节级续传。完整文件先在输出目录的临时文件中合并，MD5 和清单提供的文件大小均正确后才替换目标。失败保留已有目标文件及已验证分片，成功后清理本次状态。Shell 与 Python 的清单互通，下载缓存各自独立。

同一清单及输出文件通过状态目录中的 `lock` 目录防止并发操作。正常结束或收到 INT/TERM/HUP 会释放锁；断电或 `kill -9` 可能遗留锁。确认没有相关任务运行后，按错误提示对该 `lock` 目录执行 `rmdir` 再重试。

默认验证 HTTPS 证书，不修改 BBR 或其他系统设置。旧设备证书错误应更新系统 CA 或配置 curl 的 `CURL_CA_BUNDLE`。下载最多跟随 5 次 HTTP 重定向，跨站时移除认证，不允许 HTTPS 降级 HTTP；上传重定向直接报错，应将上传地址配置为最终 PUT 接口。

`--expires 0` 不发送有效期请求头，服务端可能生成一次性链接，一次下载后即删除，不能保证重试和恢复。日志及分片进度输出到标准错误，标准输出仅输出成功后的清单或目标路径；不显示 Python 客户端的速度、ETA 等字节进度。以 `-` 开头的文件名使用 `./文件名`，或者放在 `--` 后。

Shell 客户端会拒绝包含控制字符、`<>:"|?*`、以空格/点结尾的文件名以及 Windows 保留名称（如 `CON`、`NUL.txt`），与 Python 清单的文件名限制保持一致。清单中的临时下载链接应妥善保管。

## 测试

```bash
python -m unittest discover -s tests -p "test_*.py" -v
python -m py_compile file-transfer.py tests/test_file_transfer.py
bash tests/file-transfer-test.sh
```

Python 测试套件同时包含使用本机 HTTP 服务的 Shell 集成测试，验证两种客户端的清单互通、重试和认证重定向行为；这些测试需要 Bash 和 curl，业务脚本本身不需要 Python。未找到 Bash 时会跳过 Shell 集成测试，可通过 `FILE_TRANSFER_TEST_BASH` 指定 Bash 路径。

## 传输进度

Python 客户端的 `push` 和 `pull` 默认在标准错误输出中显示整体进度，无需增加参数：

```text
upload [########------------]  40.0% 400.0 MiB / 1000.0 MiB | 20.0 MiB/s | ETA 30s | elapsed 20s | chunks 4/10 | transferring
```

进度包括百分比、已传输/总大小、平均网络速度、预计剩余时间、耗时和已完成分片数。终端中原地刷新（最多每秒 5 次），重定向日志时每 5 秒输出一次传输更新；阶段变化和分片完成会立即输出。标准输出仍只输出清单或目标文件路径，方便脚本使用。

上传前会显示源文件校验阶段，下载后会显示合并和最终校验阶段；只有清单写入或最终文件校验、保存成功后才显示 `complete`。传输字节达到 100% 时，服务端确认或本地校验可能仍在进行，应以完成状态为准。

并发分片按字节汇总。重试或上传重定向会扣除该分片上次尝试的进度，因此百分比可能回退，不会重复累计；网络速度包含重传流量。断点续传复用的已验证分片计入进度，但不计入网络速度。旧版清单缺少总大小时显示已下载字节数、速度和分片数，不显示百分比，预计剩余时间显示 `--`。
