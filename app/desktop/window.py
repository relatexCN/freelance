"""Desktop shell: local lifecycle, remote administration and API playground."""
import json
import threading
import time
from pathlib import Path

from dotenv import dotenv_values
from pydantic import ValidationError
from PySide6.QtCore import QThread, QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QCompleter, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.admin.store import AliasEdit, ConfigStore, ProviderEdit
from .client import GatewayClient
from .local import LocalGateway, prepare_workspace


TEMPLATES = {
    "自定义": ("", ""),
    "OpenRouter": ("openrouter", "https://openrouter.ai/api/v1"),
    "Ollama Cloud": ("ollama", "https://ollama.com/v1"),
    "NVIDIA": ("nvidia", "https://integrate.api.nvidia.com/v1"),
}


class Worker(QThread):
    result = Signal(object)
    error = Signal(str)
    chunk = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.cancelled = threading.Event()

    def run(self):
        try:
            self.result.emit(self.operation(self))
        except ValidationError:
            self.error.emit("输入无效，请检查供应商名称、地址和超时等字段")
        except Exception as exc:
            # Network exception strings can contain URLs; keep transport failures generic.
            self.error.emit(str(exc) if isinstance(exc, ValueError) else "操作失败，请检查连接及文件权限")


def table(headers):
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.horizontalHeader().setStretchLastSection(True)
    return widget


def fill(widget, rows):
    widget.setRowCount(len(rows))
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            widget.setItem(row, column, QTableWidgetItem(str(value if value is not None else "—")))
    widget.resizeColumnsToContents()


class MainWindow(QMainWindow):
    OVERVIEW_INTERVAL = 15
    MODEL_INTERVAL = 60

    def __init__(self, workspace):
        super().__init__()
        self.setWindowTitle("Freelance · 模型网关")
        self.resize(1100, 820)
        self.worker = None
        self.status_worker = None
        self._status_generation = 0
        self._last_status = None
        self._connected = False
        self._runtime_api_key = None
        self._needs_load = True
        self._load_config = False
        self._load_user_requested = False
        self._next_overview_at = 0
        self._next_models_at = 0
        self._close_after_status = False
        self._restart_timer_after_cancel = False
        self.configuration = None
        self.local_store = None
        self.local_gateway = None
        self.setStyleSheet("QGroupBox { font-weight: bold; } QPushButton { padding: 6px 12px; } QLineEdit { padding: 4px; }")
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        title = QLabel("Freelance 模型网关")
        title.setStyleSheet("font-size: 24px; font-weight: bold; padding: 8px;")
        layout.addWidget(title)
        self.connection = QGroupBox("连接")
        connection_layout = QFormLayout(self.connection)
        self.mode = QComboBox()
        self.mode.addItems(["本地网关", "远程网关"])
        connection_layout.addRow("运行模式", self.mode)
        self.workspace = QLineEdit(str(workspace))
        workspace_row = QHBoxLayout()
        workspace_row.addWidget(self.workspace)
        choose = QPushButton("选择目录")
        choose.clicked.connect(self.choose_workspace)
        workspace_row.addWidget(choose)
        self.choose_button = choose
        connection_layout.addRow("本地配置目录", workspace_row)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(8000)
        connection_layout.addRow("本地端口", self.port)
        self.url = QLineEdit("http://127.0.0.1:8000")
        connection_layout.addRow("网关地址", self.url)
        self.gateway_key = QLineEdit()
        key_row = QHBoxLayout()
        key_row.addWidget(self.gateway_key)
        self.regenerate_button = QPushButton("重新生成 Key")
        self.regenerate_button.setToolTip("生成并保存本地网关 API_KEY；重启后生效")
        self.regenerate_button.clicked.connect(self.regenerate_key)
        key_row.addWidget(self.regenerate_button)
        connection_layout.addRow("调用 Key", key_row)
        self.admin_key = QLineEdit()
        connection_layout.addRow("管理员 Key", self.admin_key)
        self.gateway_key.setPlaceholderText("远程模式：输入 API_KEY")
        self.admin_key.setPlaceholderText("远程配置管理：输入 ADMIN_API_KEY")
        controls = QHBoxLayout()
        self.start_button = QPushButton("启动本地网关")
        self.stop_button = QPushButton("停止本地网关")
        self.restart_button = QPushButton("重启本地网关")
        self.refresh_button = QPushButton("连接")
        for button in (self.start_button, self.stop_button, self.restart_button, self.refresh_button):
            controls.addWidget(button)
        connection_layout.addRow(controls)
        self.start_button.clicked.connect(lambda: self.lifecycle("start"))
        self.stop_button.clicked.connect(lambda: self.lifecycle("stop"))
        self.restart_button.clicked.connect(lambda: self.lifecycle("restart"))
        self.refresh_button.clicked.connect(self.refresh)
        for field in (self.url, self.gateway_key, self.admin_key):
            field.textEdited.connect(self.connection_edited)
        layout.addWidget(self.connection)
        self.notice = QLabel("选择本地模式配置供应商，或输入远程网关地址连接。")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.build_overview()
        self.build_providers()
        self.build_playground()
        self.build_help()
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.port.valueChanged.connect(self.local_url)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll_status)
        self.timer.start()
        self.mode_changed()

    @property
    def is_local(self):
        return self.mode.currentIndex() == 0

    def page(self, name, scroll=False):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setWidget(widget)
            self.tabs.addTab(area, name)
        else:
            self.tabs.addTab(widget, name)
        return layout

    def build_overview(self):
        layout = self.page("运行概览")
        self.summary = QLabel("尚未连接。统计为当前进程累计值，重启后清零。")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.health_table = table(["供应商", "可用 Key", "Key 数量", "冷却剩余秒", "连续失败"])
        layout.addWidget(self.health_table)
        self.metrics_table = table(["模型", "请求", "成功计数", "失败计数", "平均耗时 ms"])
        layout.addWidget(self.metrics_table)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumHeight(120)
        log_button = QPushButton("查看本地启动日志")
        log_button.clicked.connect(self.show_logs)
        layout.addWidget(log_button)
        layout.addWidget(self.logs)

    def build_providers(self):
        layout = self.page("供应商配置", scroll=True)
        self.reload_button = QPushButton("重新加载本地配置")
        self.reload_button.clicked.connect(self.reload_config)
        layout.addWidget(self.reload_button)
        self.provider_table = table(["名称", "地址", "默认模型", "超时秒", "Key 状态"])
        self.provider_table.setMinimumHeight(100)
        self.provider_table.setMaximumHeight(150)
        self.provider_table.itemSelectionChanged.connect(self.select_provider)
        layout.addWidget(self.provider_table)
        form = QFormLayout()
        self.template = QComboBox()
        self.template.addItems(TEMPLATES)
        self.template.currentTextChanged.connect(self.apply_template)
        form.addRow("新增模板", self.template)
        self.provider_name = QLineEdit()
        self.provider_url = QLineEdit()
        self.provider_model = QLineEdit()
        self.provider_timeout = QSpinBox()
        self.provider_timeout.setRange(1, 3600)
        self.provider_timeout.setValue(120)
        self.provider_key = QLineEdit()
        self.provider_key.setPlaceholderText("留空保留现有 Key；多个 Key 用英文逗号分隔")
        self.clear_key = QCheckBox("清空现有 Key（停用该供应商的调用凭据）")
        for label, widget in (("供应商名称", self.provider_name), ("Base URL", self.provider_url),
                              ("默认模型", self.provider_model), ("超时秒", self.provider_timeout),
                              ("供应商 Key", self.provider_key), ("", self.clear_key)):
            form.addRow(label, widget)
        layout.addLayout(form)
        save = QPushButton("保存供应商配置")
        save.clicked.connect(self.save_provider)
        layout.addWidget(save)
        layout.addWidget(QLabel("保存后需要重启。修改已有供应商会保留高级参数；配置文件会重排 YAML 并保留一份备份。"))
        self.alias_toggle = QPushButton("高级配置：模型别名")
        self.alias_toggle.setCheckable(True)
        layout.addWidget(self.alias_toggle)
        self.alias_box = QGroupBox("模型别名")
        self.build_aliases(QVBoxLayout(self.alias_box))
        layout.addWidget(self.alias_box)
        self.alias_box.hide()
        self.alias_toggle.toggled.connect(self.alias_box.setVisible)
        layout.addStretch()

    def build_aliases(self, layout):
        self.alias_table = table(["客户端别名", "供应商", "上游模型"])
        self.alias_table.setMinimumHeight(100)
        self.alias_table.setMaximumHeight(180)
        self.alias_table.itemSelectionChanged.connect(self.select_alias)
        layout.addWidget(self.alias_table)
        form = QFormLayout()
        self.alias_name, self.alias_provider, self.alias_model = QLineEdit(), QComboBox(), QLineEdit()
        form.addRow("客户端别名", self.alias_name)
        form.addRow("供应商", self.alias_provider)
        form.addRow("上游模型 ID", self.alias_model)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        save, delete = QPushButton("保存别名"), QPushButton("删除所填别名")
        save.clicked.connect(self.save_alias)
        delete.clicked.connect(self.delete_alias)
        buttons.addWidget(save)
        buttons.addWidget(delete)
        layout.addLayout(buttons)
        layout.addWidget(QLabel("auto、free、best-free 为保留名称；当前 free 名称不保证免费调用。"))

    def build_playground(self):
        layout = self.page("调用测试")
        self.playground_page = layout.parentWidget()
        top = QHBoxLayout()
        self.test_model = QComboBox()
        self.test_model.setEditable(True)
        self.test_model.setPlaceholderText("搜索或输入模型名称 / 别名")
        self.test_model.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.test_model.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.test_model.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.test_model.addItem("auto")
        self.multi = QCheckBox("多模型独立回答（使用各供应商默认模型）")
        top.addWidget(QLabel("模型"))
        top.addWidget(self.test_model, 1)
        top.addWidget(self.multi)
        layout.addLayout(top)
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("输入测试问题。单模型以流式输出；多模型返回独立结果，不自动汇总。")
        self.prompt.setMaximumHeight(120)
        layout.addWidget(self.prompt)
        controls = QHBoxLayout()
        send, self.cancel_button = QPushButton("发送测试"), QPushButton("取消流式接收")
        send.clicked.connect(self.send_test)
        self.cancel_button.clicked.connect(self.cancel_test)
        self.cancel_button.setEnabled(False)
        controls.addWidget(send)
        controls.addWidget(self.cancel_button)
        layout.addLayout(controls)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output)

    def build_help(self):
        layout = self.page("接入指引")
        self.help_text = QPlainTextEdit()
        self.help_text.setReadOnly(True)
        layout.addWidget(self.help_text)
        copy = QPushButton("复制 API Base URL")
        copy.clicked.connect(lambda: self.copy_base_url())
        layout.addWidget(copy)

    def copy_base_url(self):
        from PySide6.QtWidgets import QApplication
        try:
            QApplication.clipboard().setText(self.client().url + "/v1")
            self.notice.setText("API Base URL 已复制")
        except ValueError as exc:
            self.problem(str(exc))

    def update_help(self):
        url = self.url.text().rstrip("/")
        self.help_text.setPlainText(
            f"OpenAI 兼容客户端接入\n\nBase URL: {url}/v1\n"
            "API Key: 使用调用 Key，勿使用管理员 Key\nModel: auto 或配置的模型别名\n\n"
            f"curl {url}/v1/chat/completions \\\n"
            "  -H \"Authorization: Bearer <API_KEY>\" \\\n  -H \"Content-Type: application/json\" \\\n  -d '{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}'\n\n"
            "本地配置不依赖网关运行；保存后重启生效。远程配置需要服务器设置 ADMIN_API_KEY。\n"
            "本地停止只作用于本客户端启动的进程。CLI 启动的服务请在远程模式连接。\n"
            "关闭窗口可保留本地服务；再次打开同一配置目录可重新连接并停止。\n"
            "统计是进程内累计值；流式成功计数可能仅代表收到首包。没有额度和费用统计。\n"
            "远程网络建议使用 HTTPS；连接信息和远程 Key 不持久化。"
        )

    def choose_workspace(self):
        if self.local_gateway and self.local_gateway.owned_process():
            self.problem("请先停止当前本地网关，再切换配置目录")
            return
        selected = QFileDialog.getExistingDirectory(self, "选择配置目录", self.workspace.text())
        if selected:
            self.workspace.setText(selected)
            self.mode_changed()

    def mode_changed(self):
        self.invalidate_status()
        previous_root = self.local_gateway.root if self.local_gateway else None
        active_key = self._runtime_api_key
        self._last_status = None
        self._connected = False
        self._needs_load = True
        self._load_config = False
        self._load_user_requested = False
        self._next_overview_at = self._next_models_at = 0
        self.configuration = None
        fill(self.provider_table, [])
        fill(self.alias_table, [])
        fill(self.health_table, [])
        fill(self.metrics_table, [])
        self.test_model.clear()
        self.test_model.addItem("auto")
        self.gateway_key.clear()
        self.admin_key.clear()
        self.provider_key.clear()
        self.url.setReadOnly(self.is_local)
        for widget in (self.workspace, self.choose_button, self.port, self.start_button, self.stop_button, self.restart_button):
            widget.setEnabled(self.is_local)
        for button in (self.start_button, self.stop_button, self.restart_button, self.reload_button):
            button.setVisible(self.is_local)
        self.refresh_button.setVisible(not self.is_local)
        self.regenerate_button.setVisible(self.is_local)
        self.regenerate_button.setEnabled(False)
        # Workspace selection is explicit to avoid switching while a process is running.
        self.workspace.setReadOnly(True)
        if self.is_local:
            try:
                root, keys = prepare_workspace(self.workspace.text())
                self.local_store = ConfigStore(root / "config.yaml")
                self.local_gateway = LocalGateway(root)
                self._runtime_api_key = active_key if previous_root == root and self.local_gateway.owned_process() else None
                runtime_revision = self.local_gateway.runtime_revision()
                if runtime_revision:
                    self.local_store.runtime_revision = runtime_revision
                existing_port = self.local_gateway.port()
                if existing_port:
                    self.port.setValue(existing_port)
                self.gateway_key.setText(keys["API_KEY"])
                self.admin_key.setText(keys["ADMIN_API_KEY"])
                self.regenerate_button.setEnabled("API_KEY" not in self.local_store.external_env_names)
                self.local_url()
                self.apply_config(self.local_store.read())
                self.notice.setText("本地配置已加载，可先配置供应商，再启动网关。")
            except (OSError, ValueError):
                self.problem("无法加载本地配置，请检查目录权限和 YAML 格式")
        else:
            self._runtime_api_key = active_key
            self.notice.setText("输入远程地址和调用 Key；修改配置另需管理员 Key。")
        self.update_help()

    def local_url(self):
        self.invalidate_status()
        if self.is_local:
            self.url.setText(f"http://127.0.0.1:{self.port.value()}")
        self.update_help()

    def connection_edited(self):
        if not self.is_local:
            self.invalidate_status()
            self._connected = False
            self._last_status = None
            self.configuration = None
            self.notice.setText("连接信息已修改，请点击连接。")

    def reload_config(self):
        if self.is_local and self.local_store and not self.worker:
            self.run(lambda worker: self.local_store.read(), self.apply_config)

    def regenerate_key(self):
        if not self.is_local or not self.local_store or not self.configuration or self.worker:
            return
        store = self.local_store
        revision = self.configuration["revision"]
        active_key = self.client().api_key if self.local_gateway and self.local_gateway.owned_process() else None

        def done(result):
            self._runtime_api_key = active_key
            self.gateway_key.setText(result["api_key"])
            self.apply_config(result["configuration"])
            self.notice.setText("新调用 Key 已生成并保存到 .env，启动或重启后生效。" +
                                ("当前连接继续使用运行中的旧 Key。" if active_key is not None else ""))
        self.run(lambda worker: store.regenerate_gateway_key(revision), done)

    def request_load(self, config=False):
        self.invalidate_status()
        self._needs_load = True
        self._load_config = config
        self._load_user_requested = True
        self._last_status = None
        self._next_overview_at = self._next_models_at = 0
        QTimer.singleShot(0, self.poll_status)

    def client(self):
        key = self._runtime_api_key if self.is_local and self._runtime_api_key is not None else self.gateway_key.text()
        return GatewayClient(self.url.text(), key, self.admin_key.text())

    def problem(self, message):
        for widget in (self.gateway_key, self.admin_key, self.provider_key):
            key = widget.text()
            if key:
                message = message.replace(key, "[REDACTED]")
        self.notice.setText(message)
        QMessageBox.warning(self, "操作未完成", message)

    def invalidate_status(self):
        self._status_generation += 1
        if self.status_worker:
            self.status_worker.cancelled.set()

    def run(self, operation, done, stream=False, quiet=False):
        if self.worker:
            return
        self.invalidate_status()
        self.connection.setEnabled(False)
        self.tabs.setEnabled(False)
        if stream:
            self.tabs.setEnabled(True)
            for index in range(self.tabs.count()):
                self.tabs.setTabEnabled(index, self.tabs.widget(index) is self.playground_page)
            self.prompt.setEnabled(False)
            self.test_model.setEnabled(False)
            self.multi.setEnabled(False)
            self.cancel_button.setEnabled(True)
        worker = Worker(operation, self)
        self.worker = worker
        worker.result.connect(done)
        worker.error.connect(lambda message: self.notice.setText(message) if quiet else self.problem(message))
        worker.chunk.connect(self.append_output)
        worker.finished.connect(self.finished)
        worker.start()

    def finished(self):
        worker, self.worker = self.worker, None
        self.connection.setEnabled(True)
        self.tabs.setEnabled(True)
        for index in range(self.tabs.count()):
            self.tabs.setTabEnabled(index, True)
        self.prompt.setEnabled(True)
        self.test_model.setEnabled(True)
        self.multi.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if worker:
            worker.deleteLater()
        if self._needs_load and self._load_user_requested:
            QTimer.singleShot(0, self.poll_status)

    def append_output(self, text):
        cursor = self.output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self.output.setTextCursor(cursor)

    def lifecycle(self, action):
        if not self.is_local or self.worker or not self.local_gateway:
            return
        gateway, port = self.local_gateway, self.port.value()
        if action in {"stop", "restart"} and not gateway.owned_process():
            self.problem("没有本客户端拥有的运行进程；不会停止 CLI 或其他程序启动的服务")
            return
        if action in {"stop", "restart"}:
            if QMessageBox.question(self, "确认本地操作", "停止会中断正在进行的调用，是否继续？") != QMessageBox.StandardButton.Yes:
                return
        self.notice.setText("正在操作本地网关…")

        def operation(worker):
            if action in {"stop", "restart"}:
                gateway.stop()
            if action != "stop":
                return gateway.start(port)
            return {"message": "本地网关已停止"}

        def done(result):
            if "port" in result:
                if result.get("started", True):
                    self._runtime_api_key = None
                self.port.setValue(result["port"])
                self.local_store.runtime_revision = gateway.runtime_revision() or self.local_store.revision()
                self.apply_config(self.local_store.read())
                self.request_load()
            else:
                self._runtime_api_key = None
                self._last_status = None
                self._needs_load = False
                fill(self.health_table, [])
                fill(self.metrics_table, [])
                self.summary.setText("本地网关已停止。")
            self.notice.setText(result["message"])
            self.port.setEnabled(not bool(gateway.owned_process()))
        self.run(operation, done)

    def poll_status(self):
        if self.worker or self.status_worker or self._close_after_status:
            return
        if self.is_local:
            if not self.local_gateway or not self.local_gateway.owned_process():
                if self.local_gateway and self.local_gateway.child and self.local_gateway.child.poll() is not None and self.local_gateway.state_path.exists():
                    self.notice.setText("本地网关进程已退出，请查看启动日志；可修正配置后重新启动。")
                return
        elif not self._connected:
            return
        try:
            client = self.client()
        except ValueError:
            return
        generation = self._status_generation
        gateway = self.local_gateway
        local = self.is_local
        loading = self._last_status == "ready" and (self._needs_load or time.monotonic() >= self._next_overview_at)
        include_models = self._needs_load or time.monotonic() >= self._next_models_at
        include_config = self._load_config
        worker = Worker(lambda task: client.overview(task.cancelled, include_models, include_config) if loading
                        else client.status(task.cancelled), self)
        self.status_worker = worker

        def current():
            return (generation == self._status_generation and gateway is self.local_gateway
                    and local == self.is_local and not self.worker and not worker.cancelled.is_set()
                    and client.url == self.url.text().rstrip("/") and client.admin_key == self.admin_key.text()
                    and client.api_key == (self._runtime_api_key if self.is_local and self._runtime_api_key is not None else self.gateway_key.text()))

        def done(result):
            if not current() or not isinstance(result, dict):
                return
            state = result.get("state", result.get("status"))
            if state != self._last_status:
                self._last_status = state
                self.notice.setText(("本地" if local else "远程") + "网关：" + str(state))
                if state == "ready":
                    self._needs_load = True
                    self.summary.setText("网关已就绪，正在自动加载运行数据…")
                else:
                    self.summary.setText("网关：" + str(state) + "；就绪后自动加载数据。")
            if loading and state == "ready" and "snapshot" in result:
                if result.get("config"):
                    self.apply_config(result["config"])
                self.apply_runtime(result, client)
                self._needs_load = self._load_config = False
                self._next_overview_at = time.monotonic() + self.OVERVIEW_INTERVAL
                if "models" in result:
                    self._next_models_at = time.monotonic() + self.MODEL_INTERVAL
                if self._load_user_requested:
                    self.notice.setText("网关已就绪，运行数据已加载。" + ("配置修改待重启。" if self.configuration and self.configuration.get("pending_restart") else ""))
                self._load_user_requested = False

        def failed(message):
            if not current():
                return
            if loading:
                self._needs_load = False
                self._next_overview_at = time.monotonic() + self.OVERVIEW_INTERVAL
                self.summary.setText("网关已就绪，但运行数据加载失败；稍后自动重试，或点击启动 / 连接重试。")
                if self._load_user_requested:
                    self.notice.setText(self.summary.text())
                self._load_user_requested = False
            elif self._last_status != "unreachable":
                self._last_status = "unreachable"
                self.notice.setText(message)

        worker.result.connect(done)
        worker.error.connect(failed)
        worker.finished.connect(lambda: self.status_finished(worker))
        worker.start()

    def status_finished(self, worker):
        if self.status_worker is worker:
            self.status_worker = None
        worker.deleteLater()
        if self._close_after_status:
            self._close_after_status = False
            QTimer.singleShot(0, self.close)
        elif self._needs_load and self._last_status == "ready":
            QTimer.singleShot(0, self.poll_status)

    def refresh(self):
        if self.worker or self.is_local:
            return
        try:
            client = self.client()
        except ValueError as exc:
            self.problem(str(exc))
            return
        self._connected = True
        self.url.setText(client.url)
        self.notice.setText("正在连接远程网关，就绪后自动加载数据…")
        self.update_help()
        self.request_load(config=True)

    def apply_runtime(self, result, client):
        snap = result["snapshot"]
        fill(self.health_table, [[p.get(k) for k in ("provider", "available_keys", "key_count", "cooldown_remaining_seconds", "consecutive_failures")] for p in snap["providers"]])
        fill(self.metrics_table, [[m.get(k) for k in ("target", "request_count", "success_count", "error_count", "avg_latency_ms")] for m in snap["metrics"]])
        if "models" in result and not self.test_model.hasFocus() and not self.test_model.view().isVisible():
            models = result["models"].get("data", [])
            selected = self.test_model.currentText()
            self.test_model.clear()
            for model in models:
                self.test_model.addItem(model["id"], model)
                self.test_model.setItemData(self.test_model.count() - 1,
                                           "归属：" + str(model.get("owned_by", "—")) + "；路由：" + str(model.get("routing_type", "真实模型")),
                                           Qt.ItemDataRole.ToolTipRole)
            self.test_model.setCurrentText(selected or "auto")
        self.summary.setText("已连接，运行概览自动更新。统计为单进程累计值；流式成功计数可能仅代表首包。" + ("没有管理员 Key，配置管理不可用。" if not client.admin_key else ""))

    def apply_config(self, configuration):
        self.configuration = configuration
        self.provider_key.clear()
        self.clear_key.setChecked(False)
        fill(self.provider_table, [[name, item["base_url"], item["default_model"], item["timeout"],
                                    f"{item['key_count']} 个" + ("（环境覆盖）" if item.get("key_environment_override") else "")]
                                   for name, item in configuration["providers"].items()])
        fill(self.alias_table, [[name, item["provider"], item["model"]] for name, item in configuration["models"].items()])
        self.alias_provider.clear()
        self.alias_provider.addItems(list(configuration["providers"]))
        if configuration["pending_restart"]:
            self.notice.setText("配置已保存，重启后生效。远程服务请通过服务器 CLI 或服务管理器重启。")

    def select_provider(self):
        row = self.provider_table.currentRow()
        if row < 0 or not self.configuration:
            return
        item = self.provider_table.item(row, 0)
        if not item:
            return
        name = item.text()
        config = self.configuration["providers"][name]
        self.provider_name.setText(name)
        self.provider_url.setText(config["base_url"])
        self.provider_model.setText(config["default_model"])
        self.provider_timeout.setValue(int(config["timeout"]))
        self.provider_key.clear()
        self.clear_key.setChecked(False)
        if self.is_local and self.local_store:
            try:
                self.provider_key.setText(self.local_store.provider_key(name))
            except (OSError, ValueError):
                self.notice.setText("无法读取本地供应商 Key，请检查配置文件。")

    def apply_template(self, label):
        name, url = TEMPLATES[label]
        self.provider_name.setText(name)
        self.provider_url.setText(url)
        self.provider_model.clear()
        self.provider_key.clear()
        self.clear_key.setChecked(False)

    def select_alias(self):
        row = self.alias_table.currentRow()
        if row >= 0 and self.alias_table.item(row, 0):
            self.alias_name.setText(self.alias_table.item(row, 0).text())
            self.alias_provider.setCurrentText(self.alias_table.item(row, 1).text())
            self.alias_model.setText(self.alias_table.item(row, 2).text())

    def save_change(self, kind, edit):
        if self.worker:
            return
        if not self.configuration:
            self.problem("请先加载配置；远程模式需要管理员 Key")
            return
        revision = self.configuration["revision"]
        try:
            client = self.client()
        except ValueError as exc:
            self.problem(str(exc))
            return
        local, store = self.is_local, self.local_store

        def operation(worker):
            if local:
                if kind == "provider":
                    return store.provider(revision, edit)
                if kind == "alias":
                    return store.alias(revision, edit)
                return store.delete_alias(revision, edit)
            if kind == "provider":
                return client.request("PUT", "/admin/providers", {"revision": revision, "provider": edit.model_dump()}, admin=True)
            if kind == "alias":
                return client.request("PUT", "/admin/aliases", {"revision": revision, "route": edit.model_dump()}, admin=True)
            return client.request("DELETE", "/admin/aliases", {"revision": revision, "alias": edit}, admin=True)
        self.run(operation, self.apply_config)

    def save_provider(self):
        try:
            name = self.provider_name.text().strip()
            key = "" if self.clear_key.isChecked() else self.provider_key.text().strip() or None
            if key is not None and not self.clear_key.isChecked() and self.is_local and self.local_store:
                if key == self.local_store.provider_key(name):
                    key = None
            edit = ProviderEdit(name=name, base_url=self.provider_url.text().strip(),
                                default_model=self.provider_model.text().strip(), timeout=self.provider_timeout.value(),
                                api_key=key)
            self.save_change("provider", edit)
        except ValidationError:
            self.problem("供应商输入无效，请检查名称、地址和 Key；地址不能包含凭据或查询参数")
        except (ValueError, OSError):
            self.problem("无法读取供应商配置，请重新加载后再保存")

    def save_alias(self):
        try:
            edit = AliasEdit(alias=self.alias_name.text().strip(), provider=self.alias_provider.currentText(), model=self.alias_model.text().strip())
            self.save_change("alias", edit)
        except ValidationError:
            self.problem("别名输入无效，请填写供应商和模型，且勿使用保留名称")

    def delete_alias(self):
        if QMessageBox.question(self, "删除别名", "确认删除所填模型别名？") == QMessageBox.StandardButton.Yes:
            self.save_change("delete", self.alias_name.text().strip())

    def send_test(self):
        if self.worker:
            return
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            self.problem("请输入测试问题")
            return
        try:
            client = self.client()
        except ValueError as exc:
            self.problem(str(exc))
            return
        multi = self.multi.isChecked()
        body = {"messages": [{"role": "user", "content": prompt}]}
        self.output.clear()
        self.notice.setText("正在调用模型…")
        if multi:
            body["parallel"] = True
            self.run(lambda worker: client.request("POST", "/v1/chat/completions/multi", body),
                     lambda result: (self.output.setPlainText(json.dumps(result, ensure_ascii=False, indent=2)), self.notice.setText("多模型调用结束：" + result.get("status", "请查看结果"))))
        else:
            body.update(model=self.test_model.currentText() or "auto", stream=True)
            self.run(lambda worker: client.stream(body, worker.chunk.emit, worker.cancelled),
                     lambda result: self.notice.setText(result), stream=True)

    def cancel_test(self):
        if self.worker:
            self.worker.cancelled.set()
            self.notice.setText("正在取消接收；上游可能仍消耗额度。")

    def show_logs(self):
        if not self.is_local or not self.local_gateway:
            self.problem("远程启动日志请在服务器查看")
            return
        content = self.local_gateway.log_tail()
        for value in dotenv_values(self.local_gateway.root / ".env").values():
            for secret in (value or "").split(","):
                if len(secret.strip()) >= 8:
                    content = content.replace(secret.strip(), "[REDACTED]")
        self.logs.setPlainText(content)

    def closeEvent(self, event):
        if self.worker:
            self.notice.setText("请等待当前操作结束；流式测试可先取消接收。")
            event.ignore()
            return
        if self.status_worker:
            self._restart_timer_after_cancel = self.timer.isActive()
            self.timer.stop()
            self._close_after_status = True
            self.invalidate_status()
            event.ignore()
            return
        if self.local_gateway and self.local_gateway.owned_process():
            box = QMessageBox(self)
            box.setWindowTitle("关闭客户端")
            box.setText("本地网关仍在运行。")
            keep = box.addButton("保留服务并退出", QMessageBox.ButtonRole.AcceptRole)
            stop = box.addButton("停止服务并退出", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() == stop:
                self.run(lambda worker: self.local_gateway.stop(), lambda _: QTimer.singleShot(0, self.close))
                event.ignore()
                return
            if box.clickedButton() != keep:
                if self._restart_timer_after_cancel:
                    self.timer.start()
                    self._restart_timer_after_cancel = False
                event.ignore()
                return
        self.timer.stop()
        event.accept()
