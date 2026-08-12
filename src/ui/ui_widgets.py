#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI 独立控件类（从 main.py 抽出）

包含：
- NumericTableWidgetItem：数值型表格项
- WideEditorDelegate：说明列宽编辑委托
- _format_size / _apply_size_item_color：大小格式化辅助函数
- NotifyBubble / show_notify_bubble：右下角悬浮提醒气泡
"""
from PySide6.QtWidgets import (QTableWidgetItem, QStyledItemDelegate, QLineEdit,
                               QWidget, QFrame, QLabel, QPushButton, QVBoxLayout,
                               QHBoxLayout, QApplication)
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation
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


class NoElideDelegate(QStyledItemDelegate):
    """绘制层强制无省略号 delegate（QSS 环境下 view 级 ElideNone 无效）

    实测：程序有全局 QSS（MODERN_QSS）时所有控件走 QStyleSheetStyle
    渲染路径，view.setTextElideMode(Qt.ElideNone) 完全不生效，
    省略号"..."照常出现（带 QSS 渲染对比 5007 vs 5007 像素，零差异）。

    此 delegate 重写 paint()：背景/选中态/图标由样式绘制（文本置空），
    文本用 painter.drawText + setClipRect 硬裁剪绘制——不经过样式的
    省略逻辑，任何 QSS 环境下都无"..."（窄列时文本在单元格边界硬切，
    完整路径悬停 tooltip 可看）。
    """
    def paint(self, painter, option, index):
        try:
            from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem
            from PySide6.QtGui import QPalette
            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            text = opt.text
            opt.text = ""  # 样式只画背景/选中态/图标，不画文本
            style = opt.widget.style() if opt.widget else QApplication.style()
            style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
            if text:
                painter.save()
                rect = opt.rect.adjusted(4, 0, -4, 0)
                painter.setClipRect(opt.rect)  # 硬裁剪：不省略、不溢出相邻列
                painter.setFont(opt.font)
                # 颜色：QSS 下 palette 不同步 selection-color（palette.HighlightedText
                # 实测仍是白色），选中文字必须显式与 MODERN_QSS 一致(#263238)，
                # 否则白字浅蓝底看不清
                if opt.state & QStyle.State_Selected:
                    color = QColor("#263238")
                else:
                    color = opt.palette.color(QPalette.Text)
                if not (opt.state & QStyle.State_Enabled):
                    color = opt.palette.color(QPalette.Disabled, QPalette.Text)
                painter.setPen(color)
                # 对齐：尊重 item 自身设置（数字列右对齐），默认左对齐垂直居中
                alignment = opt.displayAlignment
                if not alignment:
                    alignment = Qt.AlignVCenter | Qt.AlignLeft
                painter.drawText(rect, alignment, text)
                painter.restore()
        except Exception:
            super().paint(painter, option, index)


class WideEditorDelegate(NoElideDelegate):
    """说明列专用编辑委托 - 编辑时设置输入框最小宽度和自适应高度
    解决默认编辑框太小的问题：最小宽度420px，最小高度30px
    单元格过窄时向右扩展，并自动避免超出表格右边界
    继承 NoElideDelegate：绘制同样无省略号（与整表 delegate 一致）
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


class NotifyBubble(QWidget):
    """右下角悬浮提醒气泡（软件内自定义小窗，无边框置顶）

    用于"用户目录写入提醒"：不弹系统大对话框，显示 10 秒后渐渐淡出。
    同一时间仅显示一条，新消息替换旧消息；带"不再提醒"按钮。
    文案创建时走 i18n.tr() 翻译。
    """
    DISPLAY_MS = 10000  # 显示时长（10 秒）
    FADE_MS = 800       # 淡出时长（渐隐动画）

    def __init__(self, title, message, on_dont_notify=None, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)  # 圆角透明背景
        self._on_dont_notify_cb = on_dont_notify
        self.setWindowTitle(title)

        # 卡片样式：深色圆角，右下角小窗
        card = QFrame(self)
        card.setObjectName("bubbleCard")
        card.setStyleSheet("""
            #bubbleCard { background: rgba(38, 38, 38, 235); border: 1px solid #555;
                          border-radius: 8px; }
            #bubbleTitle { color: #FFD54F; font-weight: bold; font-size: 13px; }
            #bubbleMsg { color: #EEEEEE; font-size: 12px; }
            QPushButton#bubbleBtn { background: #4E6EF2; color: white; border: none;
                                    border-radius: 4px; padding: 4px 10px; font-size: 12px; }
            QPushButton#bubbleBtn:hover { background: #5B7BFF; }
            QPushButton#bubbleClose { background: transparent; color: #AAAAAA;
                                      border: none; font-size: 14px; padding: 0 4px; }
            QPushButton#bubbleClose:hover { color: white; }
        """)

        try:
            from i18n import tr
        except Exception:
            tr = lambda t: t  # noqa: E731

        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        title_lbl = QLabel(tr(title))
        title_lbl.setObjectName("bubbleTitle")
        msg_lbl = QLabel(message)
        msg_lbl.setObjectName("bubbleMsg")
        msg_lbl.setWordWrap(True)
        msg_lbl.setTextFormat(Qt.PlainText)  # 路径可能含 < > &，强制纯文本避免按 HTML 解析
        msg_lbl.setMaximumWidth(340)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        btn_dont = QPushButton(tr("不再提醒"))
        btn_dont.setObjectName("bubbleBtn")
        btn_close = QPushButton("✕")
        btn_close.setObjectName("bubbleClose")
        btn_close.setToolTip(tr("关闭"))
        btn_row.addStretch(1)
        btn_row.addWidget(btn_dont)
        btn_row.addWidget(btn_close)
        lay.addWidget(title_lbl)
        lay.addWidget(msg_lbl)
        lay.addLayout(btn_row)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        btn_dont.clicked.connect(self._on_dont_notify)
        btn_close.clicked.connect(self.close)

        # 10 秒后自动淡出
        self._anim = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self._timer.start(self.DISPLAY_MS)

    def _on_dont_notify(self):
        """点"不再提醒"：先调回调（关闭配置），再关闭气泡"""
        try:
            if self._on_dont_notify_cb:
                self._on_dont_notify_cb()
        except Exception:
            pass
        self.close()

    def _fade_out(self):
        """透明度渐变淡出后关闭"""
        try:
            self._anim = QPropertyAnimation(self, b"windowOpacity", self)
            self._anim.setDuration(self.FADE_MS)
            self._anim.setStartValue(1.0)
            self._anim.setEndValue(0.0)
            self._anim.finished.connect(self.close)
            self._anim.start()
        except Exception:
            self.close()

    def show_bubble(self):
        """右下角定位后显示（不抢输入焦点）"""
        self.adjustSize()
        try:
            screen = QApplication.primaryScreen()
            geo = screen.availableGeometry() if screen else None
            if geo is not None:
                self.move(geo.right() - self.width() - 16,
                          geo.bottom() - self.height() - 16)
        except Exception:
            pass
        self.show()
        self.raise_()


# 同一时间仅显示一条气泡（新消息替换旧消息）
_bubble_singleton = None


def show_notify_bubble(title, message, on_dont_notify=None):
    """显示右下角悬浮提醒气泡（软件内自定义小窗）

    :param title: 标题（走 i18n 翻译）
    :param message: 消息正文（含路径等动态内容，不参与整体翻译）
    :param on_dont_notify: 点"不再提醒"按钮的回调（UI 线程执行）
    """
    global _bubble_singleton
    try:
        if _bubble_singleton is not None:
            try:
                _bubble_singleton.close()
            except Exception:
                pass
        _bubble_singleton = NotifyBubble(title, message, on_dont_notify)
        _bubble_singleton.show_bubble()
    except Exception:
        pass
