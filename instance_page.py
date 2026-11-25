"""Qt controls and background workers for the existing-instance workflow."""

from datetime import datetime
from threading import Event

from PySide6.QtCore import QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCompleter, QDoubleSpinBox, QHeaderView, QHBoxLayout, QSpinBox, QTableWidgetItem,
    QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, CaptionLabel, CheckBox, ComboBox, EditableComboBox, FluentIcon as FIF, HeaderCardWidget,
    MessageBox, PlainTextEdit, PrimaryPushButton, PushButton, SegmentedWidget,
    TableWidget, TitleLabel,
)

from cloning import CLONEABLE_STATES, GIB, CloneCoordinator, CloneSettings, source_region, watch_clone
from gpuhawk_core import AutoDL
from instances import STATUS_NAMES, instance_state, send_email, stock_text, watch_instance
from task_list import TaskList


CONSOLE = "https://www.autodl.com/console/instance/list"


class InstanceWorker(QThread):
    loaded = Signal(list)
    hosts_loaded = Signal(list)
    snapshot = Signal(dict)
    notice = Signal(str, str)
    result = Signal(bool, str)
    clone_submitted = Signal(dict)
    progress = Signal(str)
    failed = Signal(str)

    def __init__(self, token, sub_user, params=None, *, clone=None, hosts=None, coordinator=None):
        super().__init__()
        self.token, self.sub_user, self.params = token, sub_user, params
        self.clone, self.hosts = clone, hosts
        self.coordinator = coordinator
        self.stop = Event()

    def run(self):
        api = AutoDL(self.token, self.sub_user)
        try:
            if self.clone:
                result = watch_clone(api, self.clone, self.stop, self.snapshot.emit, self.progress.emit,
                                     self.coordinator)
                if result:
                    self.clone_submitted.emit(result)
                else:
                    self.progress.emit("克隆监控已停止。")
            elif self.hosts:
                source, model = self.hosts
                _, signs = source_region(api.region_groups(), source)
                self.hosts_loaded.emit([
                    item for batch in api.machine_pages(signs, model, 0, cancelled=self.stop.is_set)
                    for item in batch
                    if item.get("machine_id") and item["machine_id"] != source.get("machine_id")
                    and item.get("region_sign") in signs and item.get("gpu_name") == model
                ])
            elif self.params is None:
                self.loaded.emit(api.instances(self.stop.is_set))
            else:
                if self.params[2] and self.coordinator and not self.coordinator.wait_for_source(
                    api, self.params[0], self.stop, self.params[1], self.progress.emit
                ):
                    return
                self.result.emit(*watch_instance(
                    api, *self.params, self.stop, self.snapshot.emit, self.notice.emit
                ))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            api.s.close()


class EmailWorker(QThread):
    delivered = Signal(bool, str)

    def __init__(self, settings, title, message):
        super().__init__()
        self.config, self.title, self.message = settings, title, message

    def run(self):
        self.delivered.emit(*send_email(self.config, self.title, self.message))


class InstancePage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.setObjectName("instancePage")
        self.window = window
        self.worker = None
        self.clone_coordinator = CloneCoordinator()
        self.items = {}
        self.rows = {}
        self.loaded_account = None
        self.last_snapshots = {}
        self.config = window.settings

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 16, 28, 18)
        root.setSpacing(12)
        root.addWidget(TitleLabel("已有实例", self))
        root.addWidget(CaptionLabel("选择 AutoDL 标准容器实例，等待原机开机或克隆到同地区的新主机。", self))

        controls = HeaderCardWidget(self)
        controls.setTitle("监控设置")
        controls.setBorderRadius(8)
        controls.viewLayout.setContentsMargins(16, 12, 16, 12)
        fields = QVBoxLayout()
        fields.setSpacing(10)
        controls.viewLayout.addLayout(fields)
        self.mode = SegmentedWidget(controls)
        self.mode.addItem("notify", "只监控提醒")
        self.mode.addItem("auto", "有卡自动开机")
        self.mode.addItem("clone", "有卡自动克隆")
        self.mode.setCurrentItem("notify")
        fields.addWidget(self.mode)

        row = QHBoxLayout()
        self.interval = QSpinBox()
        self.interval.setRange(5, 600)
        self.interval.setMinimumWidth(100)
        self.interval.setValue(self.config.value("instances/interval", 15, type=int))
        self.interval.setSuffix(" 秒")
        self.limit = QDoubleSpinBox()
        self.limit.setDecimals(3)
        self.limit.setMinimumWidth(140)
        self.limit.setRange(0, 9999)
        self.limit.setValue(self.config.value("instances/limit", 0.0, type=float))
        self.limit.setSpecialValueText("不限")
        self.limit.setSuffix(" 元/时")
        self.restart = CheckBox("允许关机并从无卡模式切换")
        row.addWidget(BodyLabel("轮询间隔"))
        row.addWidget(self.interval)
        row.addSpacing(12)
        self.price_label = BodyLabel("实例时价上限")
        row.addWidget(self.price_label)
        row.addWidget(self.limit)
        row.addSpacing(12)
        row.addWidget(self.restart)
        row.addStretch()
        fields.addLayout(row)
        self.restart.setEnabled(False)
        self.limit.setEnabled(False)
        self.mode.currentItemChanged.connect(self.update_controls)
        self.clone_panel = QWidget()
        clone_layout = QVBoxLayout(self.clone_panel)
        clone_layout.setContentsMargins(0, 0, 0, 0)
        clone_row = QHBoxLayout()
        self.clone_gpu = EditableComboBox()
        self.clone_gpu.setPlaceholderText("选择或搜索型号")
        self.clone_gpu.setMinimumWidth(145)
        self.clone_gpu.setText(self.config.value("clone/gpu", ""))
        self.clone_num = QSpinBox()
        self.clone_num.setRange(1, 16)
        self.clone_num.setValue(self.config.value("clone/num", 1, type=int))
        self.clone_host = ComboBox()
        self.clone_host.addItem("任意匹配主机", userData="")
        self.clone_host.setMinimumWidth(170)
        self.clone_host.setMaximumWidth(260)
        self.load_hosts_btn = PushButton(FIF.SYNC, "查找主机")
        self.load_hosts_btn.clicked.connect(self.load_hosts)
        clone_row.addWidget(BodyLabel("目标 GPU"))
        clone_row.addWidget(self.clone_gpu)
        clone_row.addWidget(BodyLabel("卡数"))
        clone_row.addWidget(self.clone_num)
        clone_row.addWidget(self.clone_host)
        clone_row.addWidget(self.load_hosts_btn)
        clone_row.addStretch()
        clone_layout.addLayout(clone_row)
        lower_row = QHBoxLayout()
        self.clone_data = CheckBox("同时克隆数据盘")
        self.clone_data.setChecked(self.config.value("clone/data", True, type=bool))
        self.clone_hint = CaptionLabel("请选择要克隆的源实例。")
        self.clone_hint.setWordWrap(True)
        lower_row.addWidget(self.clone_data)
        lower_row.addWidget(self.clone_hint, 1)
        clone_layout.addLayout(lower_row)
        fields.addWidget(self.clone_panel)
        self.clone_gpu.textChanged.connect(self.reset_hosts)
        root.addWidget(controls)

        header = QHBoxLayout()
        self.refresh = PushButton(FIF.SYNC, "加载实例")
        self.refresh.clicked.connect(self.load)
        self.start = PrimaryPushButton(FIF.PLAY, "添加并开始")
        self.start.clicked.connect(self.toggle)
        console = PushButton(FIF.LINK, "打开 AutoDL 控制台")
        console.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(CONSOLE)))
        header.addWidget(self.refresh)
        header.addWidget(console)
        header.addStretch()
        header.addWidget(self.start)
        root.addLayout(header)

        self.table = TableWidget(self)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["实例名称", "实例 ID", "状态", "规格", "空闲 / 总量"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        for col in (0, 1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.on_selection)
        root.addWidget(self.table, 1)
        self.tasks = TaskList(self)
        root.addWidget(self.tasks)

        self.status = BodyLabel("加载实例列表后，选中要监控的实例。", self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.log = PlainTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setFixedHeight(108)
        self.log.document().setMaximumBlockCount(150)
        self.log.setPlaceholderText("实例监控日志")
        root.addWidget(self.log)
        self.update_controls()

    def account(self):
        return self.window.token, self.window.sub_user_sw.isChecked()

    def update_controls(self, *_):
        active = self.worker is not None
        automatic = self.mode.currentRouteKey() == "auto"
        cloning = self.mode.currentRouteKey() == "clone"
        self.refresh.setEnabled(not active)
        self.start.setEnabled(not active and self.selected_item() is not None)
        self.mode.setEnabled(not active)
        self.table.setEnabled(not active)
        self.interval.setEnabled(not active)
        self.limit.setEnabled(not active and (automatic or cloning))
        self.restart.setEnabled(not active and automatic)
        self.restart.setVisible(not cloning)
        self.price_label.setText("GPU 总时价上限" if cloning else "实例时价上限")
        self.clone_panel.setVisible(cloning)
        self.clone_panel.setEnabled(not active)
        self.load_hosts_btn.setEnabled(not active and self.table.currentRow() >= 0)

    def selected_item(self):
        row = self.table.currentRow()
        cell = self.table.item(row, 1) if row >= 0 else None
        return self.items.get(cell.text()) if cell else None

    def on_selection(self):
        self.reset_hosts()
        item = self.selected_item()
        if item:
            try:
                region, _ = source_region(self.window.groups, item)
            except ValueError:
                region = item.get("region_sign") or "未知"
            self.clone_hint.setText(f"源实例所在地区：{region}。请先关闭源实例。")
        else:
            self.clone_hint.setText("请选择要克隆的源实例。")
        self.update_controls()

    def set_gpu_models(self, models):
        saved = self.clone_gpu.text()
        self.clone_gpu.blockSignals(True)
        self.clone_gpu.clear()
        self.clone_gpu.addItems(list(models))
        completer = QCompleter(list(models), self.clone_gpu)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.clone_gpu.setCompleter(completer)
        self.clone_gpu.setText(saved)
        self.clone_gpu.blockSignals(False)

    def reset_hosts(self, *_):
        self.clone_host.clear()
        self.clone_host.addItem("任意匹配主机", userData="")
        self.clone_host.setCurrentIndex(0)

    def validate_target(self):
        source = self.selected_item()
        gpu = self.clone_gpu.text().strip()
        if not source or self.loaded_account != self.account():
            self.log_message("请加载实例并选中源实例。")
            return None
        if not gpu or gpu not in self.window.catalog:
            self.log_message("请在「目标 GPU」里选择已加载的 AutoDL 型号。")
            return None
        return source, gpu

    def load_hosts(self):
        target = self.validate_target()
        if target is None:
            return
        self.log_message("正在查询同地区目标主机…")
        worker = InstanceWorker(*self.account(), hosts=target)
        worker.hosts_loaded.connect(self.on_hosts_loaded)
        self.run_worker(worker)

    def on_hosts_loaded(self, machines):
        selected = self.clone_host.currentData()
        self.reset_hosts()
        known = set()
        for machine in machines:
            machine_id = machine["machine_id"]
            if machine_id in known:
                continue
            known.add(machine_id)
            name = machine.get("machine_alias") or machine_id
            label = name if name == machine_id else f"{name} ({machine_id})"
            self.clone_host.addItem(f"{label} · 空闲 {machine.get('gpu_idle_num') or 0}", userData=machine_id)
            if machine_id == selected:
                self.clone_host.setCurrentIndex(self.clone_host.count() - 1)
        self.log_message(f"找到 {len(known)} 台同地区主机。可固定一台，也可等待任意匹配主机。")

    def log_message(self, text):
        self.status.setText(text)
        self.log.appendPlainText(f"[{datetime.now():%H:%M:%S}] {text}")

    def load(self):
        token, sub_user = self.account()
        if not token:
            self.log_message("请先在设置中保存 AutoDL 网页登录 Token。")
            self.window.switchTo(self.window.settings_page)
            return
        self.loaded_account = None
        self.last_snapshots.clear()
        self.items.clear()
        self.rows.clear()
        self.table.setRowCount(0)
        worker = InstanceWorker(token, sub_user)
        worker.loaded.connect(self.on_loaded)
        self.log_message("正在加载 AutoDL 标准容器实例…")
        self.run_worker(worker)

    def toggle(self):
        if self.worker:
            return
        if self.loaded_account != self.account():
            self.log_message("登录信息已变更，请重新加载实例。")
            return
        row = self.table.currentRow()
        if row < 0:
            return
        if self.mode.currentRouteKey() == "clone":
            self.start_clone()
            return
        uuid = self.table.item(row, 1).text()
        item = self.items[uuid]
        name = item.get("name") or uuid
        automatic = self.mode.currentRouteKey() == "auto"
        if any(w.params and (w.params[0], w.params[2]) == (uuid, automatic) for w in self.tasks.active):
            self.log_message("该实例已有同类监控任务。")
            return
        if automatic and any(w.clone and w.clone.source_uuid == uuid for w in self.tasks.active):
            self.log_message("此实例正在等待克隆，请先停止该实例的克隆任务。")
            return
        restart = automatic and self.restart.isChecked()
        price = self.limit.value()
        if automatic:
            description = f"目标实例：{name}\n实例 ID：{uuid}\n按量实例时价上限：{f'¥{price:.3f}/时' if price else '不限'}"
            description += "\n\n有卡开机后，按量实例开始计费。"
            if restart:
                description += "\n允许关闭无卡运行中的实例，当前进程会中断。关机后如果空卡被占用，会继续等待。"
            box = MessageBox("确认自动开机", description, self.window)
            box.yesButton.setText("开始监控")
            box.cancelButton.setText("取消")
            if not box.exec():
                return
        self.config.setValue("instances/interval", self.interval.value())
        self.config.setValue("instances/limit", price)
        self.config.setValue("instances/uuid", uuid)
        params = (uuid, self.interval.value(), automatic, restart, price)
        worker = InstanceWorker(*self.account(), params, coordinator=self.clone_coordinator)
        worker.snapshot.connect(self.on_snapshot)
        worker.notice.connect(self.on_notice)
        worker.result.connect(self.on_result)
        worker.progress.connect(self.on_progress)
        self.log_message(f"监控 {name}，每 {self.interval.value()} 秒检查一次。")
        mode = "自动开机" if automatic else "只监控"
        self.run_worker(worker, f"{mode} · {name} ({uuid})")

    def start_clone(self):
        target = self.validate_target()
        if target is None:
            return
        source, gpu = target
        if source.get("status") not in CLONEABLE_STATES and source["uuid"] not in self.clone_coordinator.inflight:
            self.log_message("请保存运行进度，在 AutoDL 关机后重新加载源实例。")
            return
        try:
            region, _ = source_region(self.window.groups, source)
        except ValueError as exc:
            self.log_message(str(exc))
            return
        copy_data = self.clone_data.isChecked()
        expansion = (source.get("expand_data_disk_size") or 0) if copy_data else 0
        settings = CloneSettings(source["uuid"], gpu, self.clone_host.currentData() or "", self.clone_num.value(),
                                 self.limit.value(), copy_data, expansion, self.interval.value())
        if any(w.params and w.params[2] and w.params[0] == settings.source_uuid for w in self.tasks.active):
            self.log_message("此实例正在等待自动开机，请先停止该实例的自动开机任务。")
            return
        if any(w.clone and (w.clone.source_uuid, w.clone.gpu, w.clone.machine_id, w.clone.num) == (
            settings.source_uuid, settings.gpu, settings.machine_id, settings.num
        ) for w in self.tasks.active):
            self.log_message("这个源实例已有相同目标和卡数的克隆任务。")
            return
        name = source.get("name") or settings.source_uuid
        host = self.clone_host.currentText()
        price = f"¥{settings.hourly_cap:.3f}/时" if settings.hourly_cap else "不限"
        disk = "系统盘和数据盘" if copy_data else "系统盘"
        if expansion:
            disk += f"（按源实例扩容 {expansion / GIB:g} GiB，另计磁盘日费）"
        box = MessageBox(
            "确认自动克隆",
            f"源实例：{name}\n实例 ID：{settings.source_uuid}\n目标：{region} · {gpu} × {settings.num}\n"
            f"主机：{host}\nGPU 总时价上限：{price}\n复制：{disk}\n\n"
            "此任务会提交一次按量克隆，新实例可能开机并计费。其他克隆任务会继续监控。",
            self.window,
        )
        box.yesButton.setText("开始监控")
        box.cancelButton.setText("取消")
        if not box.exec():
            return
        self.config.setValue("instances/uuid", source["uuid"])
        self.config.setValue("instances/interval", settings.interval)
        self.config.setValue("instances/limit", settings.hourly_cap)
        self.config.setValue("clone/gpu", gpu)
        self.config.setValue("clone/num", settings.num)
        self.config.setValue("clone/data", copy_data)
        worker = InstanceWorker(*self.account(), clone=settings, coordinator=self.clone_coordinator)
        worker.snapshot.connect(self.on_snapshot)
        worker.progress.connect(self.on_progress)
        worker.clone_submitted.connect(self.on_clone_submitted)
        self.log_message(f"开始监控克隆目标 · {region} · {gpu} × {settings.num}")
        self.run_worker(worker, f"自动克隆 · {name} → {gpu} × {settings.num} · {region} · {host}")

    def on_clone_submitted(self, result):
        self.sender().final_status = f"已提交 · {result['uuid'] or result['machine_id']}"
        target = result["uuid"] or "请在 AutoDL 实例列表查看 ID"
        message = f"{result['region']} · {result['machine_id']} · 新实例 {target} · 请在控制台查看拷贝进度。"
        self.log_message(f"克隆已提交 · {message}")
        self.window._push("AutoDL 克隆已提交", message)
        self.window.toast("success", "克隆已提交", message, 6500)

    def run_worker(self, worker, target=None):
        if target:
            self.tasks.add(worker, target, worker.stop.set)
        else:
            self.worker = worker
        worker.failed.connect(self.on_failure)
        worker.finished.connect(self.on_finished)
        self.update_controls()
        self.window._run(worker)

    def stop(self):
        self.tasks.stop_all()
        if self.worker:
            self.worker.stop.set()
            self.log_message("正在停止，等待当前请求结束…")
            self.update_controls()

    def on_loaded(self, items):
        worker = self.sender()
        self.loaded_account = (worker.token, worker.sub_user)
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            self.rows[item["uuid"]] = row
            self.render_item(item)
        prior = self.config.value("instances/uuid", "")
        if items:
            self.table.selectRow(self.rows.get(prior, 0))
        self.log_message(f"已加载 {len(items)} 台标准容器实例。" if items else "账号下暂无标准容器实例。")

    def render_item(self, item):
        uuid = item["uuid"]
        self.items[uuid] = item
        state = STATUS_NAMES.get(item.get("status"), item.get("status") or "未知")
        if instance_state(item) == "cpu":
            state += " · 无卡"
        elif instance_state(item) == "gpu":
            state += " · 有卡"
        model = item.get("snapshot_gpu_alias_name") or "—"
        count = item.get("req_gpu_amount") or "—"
        values = [item.get("name") or uuid, uuid, state, f"{model} × {count}", stock_text(item)]
        row = self.rows[uuid]
        for col, value in enumerate(values):
            cell = self.table.item(row, col)
            if cell is None:
                cell = QTableWidgetItem()
                self.table.setItem(row, col, cell)
            cell.setText(str(value))

    def on_snapshot(self, item):
        if item["uuid"] not in self.rows:
            return
        self.render_item(item)
        snapshot = (item.get("status"), item.get("start_mode"), stock_text(item))
        if snapshot != self.last_snapshots.get(item["uuid"]):
            row = self.rows[item["uuid"]]
            name = item.get("name") or item["uuid"]
            message = f"{self.table.item(row, 2).text()} · 空闲 / 总量 {snapshot[2]}"
            self.log_message(f"{name} · {message}")
            self.last_snapshots[item["uuid"]] = snapshot
        if not self.sender().clone and not self.sender().stop.is_set():
            self.tasks.set_status(self.sender(), f"监控中 · 空闲 / 总量 {snapshot[2]}")

    def on_progress(self, text):
        worker = self.sender()
        if not worker.stop.is_set():
            self.tasks.set_status(worker, text)
        target = worker.clone
        label = f"{target.gpu} × {target.num}" if target else worker.params[0]
        self.log_message(f"[{label}] {text}")

    def on_notice(self, title, text):
        self.log_message(f"{title} · {text}")
        self.window._push(title, text)

    def on_result(self, started, text):
        self.sender().final_status = text
        self.log_message(text)
        if started:
            title = "AutoDL 实例有卡运行"
            self.window._push(title, text)
            self.window.toast("success", title, text)
            self.window.confetti.burst()
            self.window.send_instance_email(title, text)

    def on_failure(self, detail):
        worker = self.sender()
        worker.final_status = detail
        self.log_message(f"操作已停止 · {detail}")
        self.window.toast("error", "实例操作已停止", detail, 6500)
        if worker in self.tasks.active:
            self.window._push("AutoDL 实例监控已停止", detail)

    def on_finished(self):
        worker = self.sender()
        if worker is self.worker:
            self.worker = None
        else:
            self.tasks.finish(worker)
        self.update_controls()
