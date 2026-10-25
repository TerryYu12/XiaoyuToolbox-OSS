# -*- coding: utf-8 -*-
"""disk-cleaner: 磁盘扫描 + 智能分类 + 安全清理工具（支持多盘 C/D/E/F）。

学习自 WindowsCleaner / MangoDisk / oh-my-disk-cleaner，
结合本机实战经验定制规则库。规则盘符解耦：扫哪个盘自动筛适用规则。
"""
__version__ = "0.2.0"

from .scanner import scan, top_dirs, top_files, human, ScanResult
from .rules import (ALL_RULES, COMMON_RULES, LOCAL_RULES, classify, Rule,
                    rules_for_drive, drive_of, expand_placeholders, rule_drive)
from .analyzer import analyze, summarize, Candidate
from .cleaner import clean, CleanReport, is_protected

__all__ = [
    "scan", "top_dirs", "top_files", "human", "ScanResult",
    "ALL_RULES", "COMMON_RULES", "LOCAL_RULES", "classify", "Rule",
    "rules_for_drive", "drive_of", "expand_placeholders", "rule_drive",
    "analyze", "summarize", "Candidate",
    "clean", "CleanReport", "is_protected",
]
