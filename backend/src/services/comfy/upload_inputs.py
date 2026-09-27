"""桥节点文件类入参的校验与解码(data URI → 上传到 sidecar 的字节)。

文件类参数只收两种形态:
- `data:<mime>;base64,<payload>` —— 调用方随请求带上的文件,桥把它上传到 sidecar 的
  input 目录,再把 sidecar 给的文件名写进图里;
- **裸文件名**(sidecar input 目录里已有的文件,如模板占位图)—— 兼容旧调用方。

其余一律拒:URL(`http://…`,桥不替调用方抓远程资源 —— SSRF)、路径(`a/b.png`、
`../x`)、ComfyUI 的注解后缀(`x.png [output]` 会让 LoadImage 去读 output/temp 目录)。
"""
from __future__ import annotations

import base64
import binascii
import re

# 已知 mime → sidecar 上的扩展名。VHS_LoadVideo / LoadImage 按扩展名认文件,
# `video/quicktime` 直接取子类型会得到 `.quicktime`,VHS 的上传清单就认不出来了。
# 视频只列 VHS_LoadVideo 认的容器(webm/mp4/mkv/mov;gif 走 image/gif)。
_VIDEO_MIMES = frozenset({"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"})
_EXT_BY_MIME = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp",
    "image/gif": "gif", "image/bmp": "bmp", "image/tiff": "tiff",
    "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
    "video/x-matroska": "mkv",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/flac": "flac",
    "audio/ogg": "ogg",
}
# 声明了具体媒体类型的参数,data URI 的 mime 大类必须对得上(image 字段收不了视频)。
# media/file/binary 是「任意素材」,不设限(老模板的语义)。
_KIND_PREFIX = {"image": "image/", "video": "video/", "audio": "audio/"}
_SAFE_SUBTYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,31}$")
_UNSAFE_FILENAME = re.compile(r"[/\\\[\]]|\.\.|://|^\s|\s$")


# 桥**会上传**的 mapping 类型(大小写敏感,按 mapping 里的原样比对)。桥的上传分支与
# `omit_when_empty` 剪枝都只认这个集合;PUT mapping 校验 `omit_when_empty` 时也用它
# (comfy_templates.py),保证「存得进去」==「桥会生效」。注意没有 `binary`。
UPLOAD_TYPES = frozenset({"media", "image", "file", "audio", "video"})


class UploadInputError(ValueError):
    """文件类入参不合法(调用方的错,不是 sidecar 的错)。"""


def decode_data_uri(key: str, param_type: str, value: str) -> tuple[bytes, str, str]:
    """`data:<mime>;base64,<payload>` → (字节, 扩展名, mime);不合法抛 UploadInputError。"""
    header, sep, payload = value.partition(",")
    if not sep or not header.startswith("data:"):
        raise UploadInputError(f"参数 {key}:不是合法的 data URI")
    meta = header[len("data:"):].split(";")
    mime = (meta[0] or "").strip().lower()
    if "base64" not in (m.strip().lower() for m in meta[1:]):
        raise UploadInputError(f"参数 {key}:data URI 必须是 base64 编码")
    if not mime or "/" not in mime:
        raise UploadInputError(f"参数 {key}:data URI 缺少 mime 类型")
    prefix = _KIND_PREFIX.get(str(param_type or "").lower())
    if prefix and not mime.startswith(prefix):
        raise UploadInputError(f"参数 {key}:需要 {prefix}* 类型的文件,收到 {mime}")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as e:
        raise UploadInputError(f"参数 {key}:data URI 的 base64 内容不合法") from e
    if not raw:
        raise UploadInputError(f"参数 {key}:文件内容为空")
    if mime.startswith("video/") and mime not in _VIDEO_MIMES:
        raise UploadInputError(f"参数 {key}:不支持的视频格式 {mime}(只收 mp4/webm/mov/mkv)")
    ext = _EXT_BY_MIME.get(mime)
    if ext is None:
        subtype = mime.rsplit("/", 1)[-1]
        if not _SAFE_SUBTYPE.match(subtype):
            raise UploadInputError(f"参数 {key}:不支持的文件类型 {mime}")
        ext = subtype
    return raw, ext, mime


def check_plain_filename(key: str, value: str) -> str:
    """非 data URI 的字符串只允许是 sidecar input 目录里的**裸文件名**。"""
    if not value or _UNSAFE_FILENAME.search(value):
        raise UploadInputError(
            f"参数 {key}:文件参数只接受 data URI(data:<mime>;base64,…)或 sidecar 上已有的"
            "文件名,不接受 URL、路径或注解后缀")
    return value
