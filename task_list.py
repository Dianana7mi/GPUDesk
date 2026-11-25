"""Compact task history and per-task stop controls shared by both pages."""

from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, PushButton, TableWidget


class TaskList(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.active = {}
        self.completed = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(CaptionLabel("监控任务", self))
        self.table = TableWidget(self)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["目标", "状态", "操作"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 90)
        self.table.setFixedHeight(137)
        layout.addWidget(self.table)
        self.hide()

    def add(self, worker, target, stop):
        self.table.insertRow(0)
        target_cell = QTableWidgetItem(target)
        target_cell.setToolTip(target)
        status = QTableWidgetItem("监控中")
        button = PushButton("停止", self.table)
        button.clicked.connect(lambda: self.request_stop(worker))
        self.table.setItem(0, 0, target_cell)
        self.table.setItem(0, 1, status)
        self.table.setCellWidget(0, 2, button)
        self.active[worker] = (status, button, stop)
        self.show()

    def set_status(self, worker, message):
        if worker in self.active:
            item = self.active[worker][0]
            item.setText(message)
            item.setToolTip(message)

    def request_stop(self, worker):
        if worker in self.active:
            _, button, stop = self.active[worker]
            self.set_status(worker, "正在停止…")
            button.setEnabled(False)
            stop()

    def stop_all(self):
        for worker in list(self.active):
            self.request_stop(worker)

    def finish(self, worker, fallback="已停止"):
        if worker not in self.active:
            return
        cell, button, _ = self.active.pop(worker)
        message = getattr(worker, "final_status", fallback)
        cell.setText(message)
        cell.setToolTip(message)
        button.setText("已结束")
        button.setEnabled(False)
        self.completed.append(cell)
        if len(self.completed) > 20:
            self.table.removeRow(self.completed.pop(0).row())
