"""musictidy — 音乐库元数据识别 / 写标签 / 归档整理工具。

流水线: scan → match → write → organize → export
"""
__version__ = "0.1.4"

from . import pipeline  # noqa: F401
