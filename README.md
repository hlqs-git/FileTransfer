# FileTransfer

一个面向 Cloudflare R2 上传服务的大文件分片传输工具。统一的 Python 客户端支持 Windows 和 Linux，可并发上传、并发下载、自动重试、MD5 校验，并兼容原有 Bash 脚本生成的清单。

## 环境要求

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

## 旧版 Bash 脚本

`file-push.sh` 和 `file-pull.sh` 继续保留，供已安装 `curl`、`md5sum`、`split`、`awk` 等 GNU/Linux 工具的环境使用：

```bash
chmod +x file-push.sh file-pull.sh
./file-push.sh /path/archive.tar.gz
./file-pull.sh
```

新 Python 客户端是 Windows 和 Linux 的推荐入口；Bash 脚本不提供新的并发与跨平台能力。

## 测试

```bash
python -m unittest discover -s tests -p "test_*.py" -v
python -m py_compile file-transfer.py tests/test_file_transfer.py
```
