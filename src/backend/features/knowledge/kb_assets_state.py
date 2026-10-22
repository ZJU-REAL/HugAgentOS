"""kb assets state responsibilities."""
from __future__ import annotations
import os

ROW_TYPE_IMAGE = "image"


ASSET_ROW_PREFIX = "a_"


MIN_ASSET_BYTES = int(os.getenv("KB_ASSET_MIN_BYTES", "4096"))


MAX_CAPTIONS_PER_DOC = int(os.getenv("KB_ASSET_CAPTION_MAX_PER_DOC", "200"))


MIN_ASSET_EDGE_PX = int(os.getenv("KB_ASSET_MIN_EDGE_PX", "64"))


CAPTION_BATCH = max(1, int(os.getenv("KB_ASSET_CAPTION_BATCH", "2")))


MAX_TEXT_CONTENT_CHARS = max(200, int(os.getenv("KB_ASSET_TEXT_MAX_CHARS", "4000")))


CAPTION_FOCUS = (
    "这是文档中的插图，识别结果会被向量化用于知识库检索。请重点覆盖："
    "图的类型（照片/流程图/架构图/折线图/柱状图/表格截图等）、"
    "图中出现的全部文字与数字（坐标轴、图例、标签、单位）、"
    "以及图所反映的关键结论或趋势。读不出来的一律写进 uncertainty，不要猜。"
)
