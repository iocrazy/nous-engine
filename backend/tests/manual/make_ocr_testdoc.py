"""生成一张**内容已知**的测试文档图,给 OCR 模型做逐字可核的准确率评测。

为什么不直接拿现成的扫描件/截图:那些的 ground truth 要人工抄一遍,既费事又会引入
抄写错误。这里反过来 —— 先定文本,再渲染成图,GT 就是源码里的常量,accuracy 可以
按字符级严格算(见 bench_ocr_asr_embed.py 的 _cer)。

刻意混进 OCR 容易错的东西:中英混排、全角/半角标点、数字与单位、表格对齐、
形近字(0/O、1/l、二/2)、上下标式的括号编号。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
OUT = Path(__file__).with_name("_ocr_testdoc.png")

TITLE = "nous-engine 显存分配实测报告"
BODY = [
    "一、背景",
    "本机三卡布局:GPU 0 与 GPU 2 为 RTX 3090(24GB,sm_86),GPU 1 为 RTX PRO 6000",
    "Blackwell(96GB,sm_120)。两张 3090 之间有 NVLink(NV4,4 链路各 14 GB/s)。",
    "",
    "二、关键发现",
    "1. gpu_memory_utilization 是「占整卡容量的比例」,不是绝对值。同一个 0.26 在",
    "   96GB 卡上是 24.7GB,在 24GB 卡上只有 6.2GB,相差 4 倍。",
    "2. WeMM-Embedding-2B 的权重仅 4.53GiB,但视觉塔带多模态 dummy 输入的 profiling",
    "   峰值达 11.95GiB —— 非权重开销不随参数量缩放。",
    "3. MOSS ASR 权重只有 1.80 GB;在 Pro 6000 上占用 17.5GiB 属于虚胖。",
    "",
    "三、实测数据",
]
TABLE_HEAD = ["模型", "权重(GiB)", "峰值激活", "KV cache", "稳态占用"]
TABLE_ROWS = [
    ["WeMM-2B", "4.53", "11.95", "8.22", "13.4"],
    ["WeMM-4B", "8.60", "12.40", "7.66", "17.6"],
    ["Unlimited-OCR", "6.21", "未测", "未测", "18.9"],
    ["MOSS ASR", "1.80", "N/A", "3.58", "8.3"],
]
FOOTER = [
    "",
    "备注(1):以上数字来自 nvidia-smi 与 vLLM 启动日志,非声明值。",
    "备注(2):460.2s 播客转写 wall time —— Pro 6000 为 6.85s,RTX 3090 为 11s。",
    "联系方式:ops@nous-engine.local / 内网 10.0.0.10:8000",
]

# 逐字核对用的 ground truth —— 渲染进图里的全部文本,顺序一致。
GROUND_TRUTH = "\n".join(
    [TITLE, *BODY, " ".join(TABLE_HEAD), *[" ".join(r) for r in TABLE_ROWS], *FOOTER]
)


def build(width: int = 1240) -> Path:
    f_title = ImageFont.truetype(FONT_BOLD, 34)
    f_h = ImageFont.truetype(FONT_BOLD, 20)
    f_b = ImageFont.truetype(FONT_REG, 20)
    f_s = ImageFont.truetype(FONT_REG, 17)

    img = Image.new("RGB", (width, 1000), "white")
    d = ImageDraw.Draw(img)
    y = 40
    d.text((60, y), TITLE, font=f_title, fill="black")
    y += 60
    d.line([(60, y), (width - 60, y)], fill="black", width=2)
    y += 24

    for line in BODY:
        font = f_h if line and line[0] in "一二三四五" else f_b
        d.text((60, y), line, font=font, fill="black")
        y += 30 if line else 14

    # 表格 —— 固定列宽,画框线,考验版面结构还原
    y += 10
    cols = [60, 280, 430, 580, 730, 900]
    d.rectangle([cols[0], y, cols[-1], y + 34 * (len(TABLE_ROWS) + 1)], outline="black")
    for i, h in enumerate(TABLE_HEAD):
        d.text((cols[i] + 10, y + 8), h, font=f_h, fill="black")
    y += 34
    for row in TABLE_ROWS:
        d.line([(cols[0], y), (cols[-1], y)], fill="black")
        for i, cell in enumerate(row):
            d.text((cols[i] + 10, y + 8), cell, font=f_b, fill="black")
        y += 34
    for c in cols:
        d.line([(c, y - 34 * (len(TABLE_ROWS) + 1)), (c, y)], fill="black")

    y += 22
    for line in FOOTER:
        d.text((60, y), line, font=f_s, fill="black")
        y += 26

    img = img.crop((0, 0, width, min(y + 40, img.height)))
    img.save(OUT)
    return OUT


if __name__ == "__main__":
    p = build()
    print(f"已生成 {p}  ({Image.open(p).size[0]}×{Image.open(p).size[1]})")
    print(f"ground truth 字符数: {len(GROUND_TRUTH)}")
