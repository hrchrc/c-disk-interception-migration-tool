#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI 独立控件类（从 main.py 抽出）

包含：
- NumericTableWidgetItem：数值型表格项
- WideEditorDelegate：说明列宽编辑委托
- _format_size / _apply_size_item_color：大小格式化辅助函数
"""
from PySide6.QtWidgets import QTableWidgetItem, QStyledItemDelegate, QLineEdit
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor


class NumericTableWidgetItem(QTableWidgetItem):
    """数值型表格项 - 按UserRole存储的数值排序，而非按显示文本字符串排序
    用于"大小(MB)"列，确保点击列标题时按数值大小排序（10 > 9，而非字符串"10" < "9"）
    """
    def __lt__(self, other):
        try:
            my_val = self.data(Qt.UserRole)
            other_val = other.data(Qt.UserRole) if other else None
            if my_val is None and other_val is None:
                return super().__lt__(other)
            if my_val is None:
                return True
            if other_val is None:
                return False
            return float(my_val) < float(other_val)
        except Exception:
            return super().__lt__(other)


def _format_size(size_mb):
    """格式化大小显示：<1MB用KB显示，>=1MB用MB显示
    :param size_mb: 大小（MB，float，保留6位小数）
    :return: 显示文本字符串
    """
    try:
        val = float(size_mb)
    except (ValueError, TypeError):
        return str(size_mb)
    if val <= 0:
        return "0B"  # 真正的空目录
    if val < 1.0:
        kb = val * 1024
        if kb < 1:
            # 小于1KB，显示字节（用 round 避免浮点误差导致少1字节）
            return f"{int(round(kb * 1024))}B"
        return f"{kb:.1f}KB"
    return f"{val:.1f}MB"


def _apply_size_item_color(item, size_mb):
    """根据目录大小给表格大小单元格上色（仅标注小目录）

    - <1MB（KB/B 级别）：橙色，提示小目录
    - >=1MB：默认色（不上色，避免表格颜色过多）
    - 0字节：不上色（文本已是"0B"）
    """
    try:
        val = float(size_mb)
    except (ValueError, TypeError):
        return
    if val <= 0:
        return  # 空目录不上色
    if val < 1.0:
        from PySide6.QtGui import QColor
        item.setForeground(QColor("#FB8C00"))  # 橙色：小目录（KB/B级）


class WideEditorDelegate(QStyledItemDelegate):
    """说明列专用编辑委托 - 编辑时设置输入框最小宽度和自适应高度
    解决默认编辑框太小的问题：最小宽度420px，最小高度30px
    单元格过窄时向右扩展，并自动避免超出表格右边界
    """
    MIN_WIDTH = 420
    MIN_HEIGHT = 30

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        # 如果是QLineEdit，设置最小宽度和高度，确保输入框足够大
        if isinstance(editor, QLineEdit):
            editor.setMinimumWidth(self.MIN_WIDTH)
            editor.setMinimumHeight(self.MIN_HEIGHT)
            # 文字边距加大，避免贴边
            editor.setTextMargins(6, 2, 6, 2)
        return editor

    def updateEditorGeometry(self, editor, option, index):
        # 先用默认几何
        super().updateEditorGeometry(editor, option, index)
        rect = option.rect
        # 单元格太窄时，编辑器扩展到最小宽度
        if editor.minimumWidth() > rect.width():
            rect.setWidth(self.MIN_WIDTH)
        # 单元格太矮时，扩展到最小高度
        if editor.minimumHeight() > rect.height():
            rect.setHeight(self.MIN_HEIGHT)
        # 避免编辑器超出父控件（表格）右边界：必要时左移
        try:
            table = editor.parent().parent()
            if table is not None:
                table_right = table.width() - 8  # 留8px边距
                if rect.right() > table_right:
                    rect.moveRight(table_right)
                    if rect.width() < self.MIN_WIDTH:
                        # 表格太窄装不下，至少保证宽度
                        rect.setWidth(self.MIN_WIDTH)
        except Exception:
            pass
        editor.setGeometry(rect)
