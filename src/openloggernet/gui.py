"""`oln gui`: a desktop window for a Campbell logger on USB, in the manner of LoggerNet's Connect
screen and Device Configuration Utility. Needs the `gui` extra (PySide6):

    uv tool install "openloggernet[gui]"
    oln gui

Connecting opens the logger's serial port and keeps it, as LoggerNet does, so nothing else can
use that logger until Disconnect. Every PakBus call runs on one worker thread, in order; the window
only queues work and shows results.
"""

import datetime
import math
import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import cli, flash, os_update, pakbus, records
from . import settings as settings_lib
from .pakbus import PakBusError, PakBusLink

# --- the worker ------------------------------------------------------------------------------


class Worker(QObject):
    """Runs jobs that use the link, one at a time, on its own thread.

    A job is a function of the link. Its result, or its error, comes back to the window through a
    signal, so callbacks run on the GUI thread.
    """

    finished = Signal(object, object)  # callback, result
    failed = Signal(str)
    progress = Signal(object, int, int)  # callback, done, total

    def __init__(self) -> None:
        super().__init__()
        self.link: PakBusLink | None = None
        self.port: str | None = None
        self.jobs: queue.Queue = queue.Queue()
        self.busy = False
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, job: Callable, done: Callable | None = None, error: Callable[[str], None] | None = None) -> None:
        self.jobs.put((job, done, error))

    def idle(self) -> bool:
        return not self.busy and self.jobs.empty()

    def _loop(self) -> None:
        while True:
            job, done, error = self.jobs.get()
            self.busy = True
            try:
                result = job(self)
            except Exception as exc:  # reported in the window, never kills the worker
                text = str(exc) or type(exc).__name__
                if error:
                    self.finished.emit(error, text)
                else:
                    self.failed.emit(text)
            else:
                if done:
                    self.finished.emit(done, result)
            finally:
                self.busy = False

    # -- helpers used inside jobs --

    def need(self) -> PakBusLink:
        if self.link is None:
            raise PakBusError("not connected")
        return self.link

    def file_control(self, name: str, code: int) -> dict:
        link = self.need()
        resp, hold_off = link.file_control(name, code)
        if resp != 0:
            raise PakBusError(f"file control {code}: response code {resp}")
        if hold_off:
            time.sleep(hold_off)
            if flash.reconnect(link, self.port, model=link.model) is None:
                raise PakBusError("the logger's USB port did not come back")
        return cli.program_status(link)


# --- small widgets -----------------------------------------------------------------------------


class Plot(QWidget):
    """A plain line plot of one field's recent values."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(160)
        self.title = ""
        self.points: deque = deque(maxlen=600)

    def reset(self, title: str) -> None:
        self.title = title
        self.points.clear()
        self.update()

    def add(self, value) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            self.points.append(float(value))
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt's name)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(48, 18, -8, -8)
        painter.fillRect(self.rect(), self.palette().base())
        painter.setPen(self.palette().text().color())
        painter.drawText(8, 14, self.title or "Click a value to plot it")
        if len(self.points) < 2:
            return
        low, high = min(self.points), max(self.points)
        if high == low:
            high, low = high + 0.5, low - 0.5
        painter.drawText(4, rect.top() + 10, f"{high:.4g}")
        painter.drawText(4, rect.bottom(), f"{low:.4g}")
        painter.setPen(QPen(QColor(120, 120, 120), 1, Qt.PenStyle.DotLine))
        painter.drawRect(rect)
        step = rect.width() / max(1, (self.points.maxlen or len(self.points)) - 1)
        start = rect.right() - step * (len(self.points) - 1)
        line = QPolygonF(
            [
                QPointF(start + i * step, rect.bottom() - (v - low) / (high - low) * rect.height())
                for i, v in enumerate(self.points)
            ]
        )
        painter.setPen(QPen(QColor(30, 120, 220), 2))
        painter.drawPolyline(line)


def table_widget(headers: list[str], stretch: int | None = None) -> QTableWidget:
    """A read-only table. Column `stretch` takes the spare width; otherwise the last one does."""
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    if stretch is None:
        header.setStretchLastSection(True)
    else:
        # A long value (a route list, a compile result) would otherwise widen the whole window.
        header.setSectionResizeMode(stretch, QHeaderView.ResizeMode.Stretch)
    return table


def fill(table: QTableWidget, rows: list[list]) -> None:
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            item = QTableWidgetItem("" if value is None else str(value))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(r, c, item)


def shown(value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, datetime.datetime):
        return records.timestamp(value)
    return str(value).strip()


# --- the window --------------------------------------------------------------------------------


class Window(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("open-loggernet")
        self.resize(1180, 760)
        self.worker = Worker()
        self.worker.finished.connect(lambda callback, value: callback(value))
        self.worker.failed.connect(self.show_error)
        self.worker.progress.connect(lambda callback, done, total: callback(done, total))
        self.tables: list = []
        self.settings: dict = {}
        self.setting_defs: dict = {}
        self.terminal_tran: int | None = None

        splitter = QSplitter()
        splitter.addWidget(self._station_list())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._station_tab(), "Station")
        self.tabs.addTab(self._tables_tab(), "Tables && Collect")
        self.tabs.addTab(self._monitor_tab(), "Monitor")
        self.tabs.addTab(self._files_tab(), "Files && Program")
        self.tabs.addTab(self._settings_tab(), "Settings")
        self.tabs.addTab(self._terminal_tab(), "Terminal")
        self.tabs.addTab(self._os_tab(), "Operating system")
        self.tabs.setEnabled(False)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)
        self.statusBar().showMessage("Choose a logger and Connect")

        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self.monitor_tick)
        self.terminal_timer = QTimer(self)
        self.terminal_timer.timeout.connect(self.terminal_poll)
        self.refresh_ports()

    # -- plumbing --

    def run(self, job: Callable, done: Callable | None = None, busy: str | None = None) -> None:
        if busy:
            self.statusBar().showMessage(busy)
        self.worker.submit(job, done)

    def show_error(self, text: str) -> None:
        self.statusBar().showMessage(f"Error: {text}")
        if "not connected" not in text:
            QMessageBox.warning(self, "Logger", text)

    def confirm(self, text: str) -> bool:
        answer = QMessageBox.question(self, "Logger", text)
        return answer == QMessageBox.StandardButton.Yes

    # -- station list --

    def _station_list(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.addWidget(QLabel("<b>Loggers on USB</b>"))
        self.ports = QListWidget()
        layout.addWidget(self.ports)
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self.refresh_ports)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        layout.addWidget(rescan)
        layout.addWidget(self.connect_button)
        self.clock_label = QLabel("")
        self.clock_label.setWordWrap(True)
        layout.addWidget(self.clock_label)
        box.setMaximumWidth(260)
        return box

    def refresh_ports(self) -> None:
        self.ports.clear()
        for port in pakbus.list_loggers():
            item = QListWidgetItem(f"{port.model or '?'}  ({port.device})")
            item.setData(Qt.ItemDataRole.UserRole, port.device)
            self.ports.addItem(item)
        if self.ports.count():
            self.ports.setCurrentRow(0)

    def toggle_connection(self) -> None:
        if self.worker.link is not None:
            self.stop_monitor()
            self.close_terminal()

            def disconnect(w: Worker) -> None:
                if w.link:
                    w.link.close()
                w.link = None

            self.run(disconnect, lambda _: self.connected(None))
            return
        item = self.ports.currentItem()
        if item is None:
            return
        port = item.data(Qt.ItemDataRole.UserRole)

        def connect(w: Worker) -> str:
            link = PakBusLink(port)
            if not link.hello():
                link.close()
                raise PakBusError(f"the logger on {port} did not answer; is LoggerNet connected to it?")
            w.link, w.port = link, port
            return port

        self.run(connect, self.connected, f"Connecting to {port}…")

    def connected(self, port: str | None) -> None:
        self.tabs.setEnabled(port is not None)
        self.ports.setEnabled(port is None)
        self.connect_button.setText("Disconnect" if port else "Connect")
        self.statusBar().showMessage(f"Connected to {port}" if port else "Disconnected")
        if port:
            self.refresh_station()
            self.refresh_tables()
            self.refresh_files()

    # -- station --

    def _station_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        left = QVBoxLayout()
        info = QGroupBox("Datalogger")
        self.info_form = QFormLayout(info)
        self.info_labels = {}
        for key, label in [
            ("model", "Model"),
            ("serial_number", "Serial number"),
            ("station_name", "Station name"),
            ("os_version", "OS version"),
            ("program_name", "Program"),
            ("state", "State"),
            ("power_up_program", "Power-up program"),
            ("compile_result", "Compile result"),
        ]:
            self.info_labels[key] = QLabel("")
            self.info_labels[key].setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.info_form.addRow(label, self.info_labels[key])
        left.addWidget(info)
        clock = QGroupBox("Clock")
        clock_layout = QFormLayout(clock)
        self.clock_logger = QLabel("")
        self.clock_offset = QLabel("")
        clock_layout.addRow("Logger", self.clock_logger)
        clock_layout.addRow("Offset from UTC", self.clock_offset)
        buttons = QHBoxLayout()
        check = QPushButton("Check")
        check.clicked.connect(lambda: self.clock(False))
        set_utc = QPushButton("Set to UTC")
        set_utc.clicked.connect(lambda: self.clock(True))
        buttons.addWidget(check)
        buttons.addWidget(set_utc)
        clock_layout.addRow(buttons)
        left.addWidget(clock)
        left.addStretch()
        layout.addLayout(left, 1)
        status = QGroupBox("Station status")
        status_layout = QVBoxLayout(status)
        self.status_table = table_widget(["Field", "Value"], stretch=1)
        refresh = QPushButton("Refresh status")
        refresh.clicked.connect(self.refresh_station)
        status_layout.addWidget(self.status_table)
        status_layout.addWidget(refresh)
        layout.addWidget(status, 2)
        return page

    def refresh_station(self) -> None:
        def job(w: Worker):
            link = w.need()
            info = cli.program_status(link)
            table = cli.find_table(link.table_definitions(), "Status")
            record = link.read_record(table)
            return info, record

        def done(result) -> None:
            info, record = result
            for key, label in self.info_labels.items():
                label.setText(str(info.get(key, "") or "").strip())
            self.setWindowTitle(f"open-loggernet: {info.get('model')} {info.get('serial_number')}")
            if record:
                fill(self.status_table, [[k, shown(v)] for k, v in record[2].items()])
            self.statusBar().showMessage("Station read")
            self.clock(False)

        self.run(job, done, "Reading the station…")

    def clock(self, set_utc: bool) -> None:
        if set_utc and not self.confirm("Set the logger's clock to this computer's UTC time?"):
            return

        def job(w: Worker):
            link = w.need()
            logger, host, _ = cli.measure_clock(link)
            if set_utc:
                adjust = (host - logger).total_seconds()
                whole = int(adjust)
                link.clock(whole, int(round((adjust - whole) * 1e9)))
                logger, host, _ = cli.measure_clock(link)
            return logger, (logger - host).total_seconds()

        def done(result) -> None:
            logger, offset = result
            self.clock_logger.setText(logger.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
            self.clock_offset.setText(f"{offset:+.3f} s")
            self.clock_label.setText(f"Logger clock {offset:+.1f} s from UTC")

        self.run(job, done)

    # -- tables and collection --

    def _tables_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.table_list = table_widget(["#", "Table", "Records", "Record size", "Interval", "Fields"])
        layout.addWidget(self.table_list)
        row = QHBoxLayout()
        self.collect_all = QCheckBox("All records")
        self.collect_all.setChecked(True)
        self.collect_count = QSpinBox()
        self.collect_count.setRange(1, 10_000_000)
        self.collect_count.setValue(1000)
        self.collect_format = QComboBox()
        self.collect_format.addItems(["toa5", "csv", "jsonl"])
        collect = QPushButton("Collect to file…")
        collect.clicked.connect(self.collect)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_tables)
        for widget in (self.collect_all, QLabel("or newest"), self.collect_count, QLabel("as"), self.collect_format):
            row.addWidget(widget)
        row.addStretch()
        row.addWidget(refresh)
        row.addWidget(collect)
        layout.addLayout(row)
        self.collect_progress = QProgressBar()
        self.collect_rate = QLabel("")
        layout.addWidget(self.collect_progress)
        layout.addWidget(self.collect_rate)
        return page

    def refresh_tables(self) -> None:
        def done(tables) -> None:
            self.tables = tables
            fill(
                self.table_list,
                [
                    [
                        t.number,
                        t.name,
                        t.size,
                        t.record_size,
                        f"{t.interval.total_seconds():g} s" if t.is_interval else "event",
                        ", ".join(f.name for f in t.fields),
                    ]
                    for t in tables
                ],
            )
            current = self.monitor_table.currentText()
            self.monitor_table.clear()
            self.monitor_table.addItems([t.name for t in tables])
            self.monitor_table.setCurrentText(current or "Public")

        self.run(lambda w: w.need().table_definitions(), done)

    def collect(self) -> None:
        row = self.table_list.currentRow()
        if row < 0:
            QMessageBox.information(self, "Collect", "Select a table first.")
            return
        table = self.tables[row]
        fmt = self.collect_format.currentText()
        suffix = {"toa5": "dat", "csv": "csv", "jsonl": "jsonl"}[fmt]
        target, _ = QFileDialog.getSaveFileName(self, f"Collect {table.name}", f"{table.name}.{suffix}")
        if not target:
            return
        count = cli.ALL_RECORDS if self.collect_all.isChecked() else self.collect_count.value()
        started = time.monotonic()

        def report(done: int, total: int) -> None:
            self.collect_progress.setMaximum(max(total, 1))
            self.collect_progress.setValue(done)
            elapsed = time.monotonic() - started
            self.collect_rate.setText(f"{done} records, {done / elapsed:.0f} records/s" if elapsed else "")

        def job(w: Worker):
            link = w.need()
            got = link.collect_recent(table, count, progress=lambda d, t: w.progress.emit(report, d, t))
            info = cli.program_info(link)
            environment = {
                "station": info.get("station_name", ""),
                "model": info.get("model") or "",
                "serial": info["serial_number"],
                "os": info["os_version"],
                "program": info["program_name"],
                "program_signature": info["program_signature"],
            }
            with open(target, "w", newline="", encoding="utf-8") as out:
                records.write_records(out, table, got, fmt, environment)
            return len(got)

        def done(n: int) -> None:
            elapsed = time.monotonic() - started
            self.collect_progress.setMaximum(max(n, 1))
            self.collect_progress.setValue(n)
            self.collect_rate.setText(f"{n} records in {elapsed:.1f} s ({n / elapsed:.0f} records/s) -> {target}")
            self.statusBar().showMessage(f"Collected {n} records of {table.name}")

        self.run(job, done, f"Collecting {table.name}…")

    # -- monitor --

    def _monitor_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        self.monitor_table = QComboBox()
        self.monitor_interval = QDoubleSpinBox()
        self.monitor_interval.setRange(0.1, 3600)
        self.monitor_interval.setValue(1.0)
        self.monitor_interval.setSuffix(" s")
        self.monitor_button = QPushButton("Start")
        self.monitor_button.clicked.connect(self.toggle_monitor)
        self.monitor_record = QLabel("")
        for widget in (
            QLabel("Table"),
            self.monitor_table,
            QLabel("every"),
            self.monitor_interval,
            self.monitor_button,
        ):
            row.addWidget(widget)
        row.addWidget(self.monitor_record, 1)
        layout.addLayout(row)
        self.monitor_values = table_widget(["Field", "Value", "Units"], stretch=1)
        self.monitor_values.cellClicked.connect(self.plot_field)
        layout.addWidget(self.monitor_values, 2)
        self.plot = Plot()
        layout.addWidget(self.plot, 1)
        self.monitor_previous: dict = {}
        self.plotted = ""
        return page

    def toggle_monitor(self) -> None:
        if self.monitor_timer.isActive():
            self.stop_monitor()
            return
        self.monitor_previous = {}
        self.monitor_values.setRowCount(0)
        self.monitor_timer.start(int(self.monitor_interval.value() * 1000))
        self.monitor_button.setText("Stop")
        self.monitor_tick()

    def stop_monitor(self) -> None:
        self.monitor_timer.stop()
        self.monitor_button.setText("Start")

    def monitor_tick(self) -> None:
        if not self.worker.idle():
            return  # a slow logger or another job: skip this beat rather than queue them up
        name = self.monitor_table.currentText()
        table = next((t for t in self.tables if t.name == name), None)
        if table is None:
            return

        def done(record) -> None:
            if not record:
                return
            number, when, values = record
            names = records.column_names(table)
            flat = dict(zip(names, records.flat_values(table, values), strict=True))
            units = {}
            for field in table.fields:
                for col in names:
                    if col == field.name or col.startswith(field.name + "("):
                        units[col] = field.units
            self.monitor_record.setText(f"record {number}  {records.timestamp(when)}")
            if self.monitor_values.rowCount() != len(flat):
                fill(self.monitor_values, [[k, "", units.get(k, "")] for k in flat])
            changed = QBrush(QColor(255, 236, 140))
            for row, (key, value) in enumerate(flat.items()):
                item = QTableWidgetItem(shown(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if key in self.monitor_previous and self.monitor_previous[key] != value:
                    item.setBackground(changed)
                self.monitor_values.setItem(row, 1, item)
                if key == self.plotted:
                    self.plot.add(value)
            self.monitor_previous = flat

        self.worker.submit(lambda w: w.need().read_record(table), done)

    def plot_field(self, row: int, _column: int) -> None:
        item = self.monitor_values.item(row, 0)
        if item:
            self.plotted = item.text()
            self.plot.reset(self.plotted)

    # -- files and program --

    def _files_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.program_label = QLabel("")
        layout.addWidget(self.program_label)
        self.file_list = table_widget(["Name", "Size", "Modified", "Run flags"])
        layout.addWidget(self.file_list)
        row = QHBoxLayout()
        actions = [
            ("Refresh", self.refresh_files),
            ("Send file…", self.send_file),
            ("Get…", self.get_file),
            ("Delete", self.delete_file),
            ("Run", self.run_program),
            (
                "Stop program",
                lambda: self.program_code("", pakbus.FILECTL_PAUSE, "Stop the program? Its data is kept."),
            ),
            ("Resume", self.resume_program),
        ]
        for label, action in actions:
            button = QPushButton(label)
            button.clicked.connect(action)
            row.addWidget(button)
        self.keep_data = QCheckBox("Run keeps data")
        self.keep_data.setChecked(True)
        row.addWidget(self.keep_data)
        row.addStretch()
        layout.addLayout(row)
        return page

    def refresh_files(self) -> None:
        def job(w: Worker):
            link = w.need()
            return link.directory(), cli.program_status(link)

        def done(result) -> None:
            entries, info = result
            rows = []
            for e in entries:
                flags = [pakbus.FILE_ATTRIBUTES.get(a, f"attribute {a}") for a in e.attributes]
                if 5 in e.attributes:
                    flags = ["stopped" if f == "running" else f for f in flags]
                size = f"{cli.human_size(e.size)} free" if e.is_drive else cli.human_size(e.size)
                rows.append([e.name, size, e.modified, ", ".join(flags)])
            fill(self.file_list, rows)
            self.program_label.setText(
                f"<b>{info['program_name'] or 'No program'}</b> ({info['state']}); "
                f"power-up {info['power_up_program'] or 'none'}; {info['compile_result'].strip()}"
            )

        self.run(job, done)

    def selected_file(self) -> str | None:
        row = self.file_list.currentRow()
        item = self.file_list.item(row, 0) if row >= 0 else None
        if item is None or item.text().endswith(":"):
            QMessageBox.information(self, "Files", "Select a file first.")
            return None
        return item.text()

    def send_file(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, "File to send to the logger")
        if not source:
            return
        data = Path(source).read_bytes()
        name = f"CPU:{Path(source).name}"

        def job(w: Worker):
            code = w.need().file_download(name, data)
            if code != 0:
                raise PakBusError(f"{name}: the logger refused the file, response code {code}")

        self.run(job, lambda _: self.refresh_files(), f"Sending {name}…")

    def get_file(self) -> None:
        name = self.selected_file()
        if not name:
            return
        target, _ = QFileDialog.getSaveFileName(self, f"Save {name}", name.split(":", 1)[1])
        if not target:
            return

        def job(w: Worker) -> int:
            data = w.need().file_upload(name, swath=983)
            Path(target).write_bytes(data)
            return len(data)

        self.run(job, lambda n: self.statusBar().showMessage(f"{name}: {n} bytes saved"), f"Reading {name}…")

    def delete_file(self) -> None:
        name = self.selected_file()
        if name and self.confirm(f"Delete {name} from the logger?"):
            self.run(lambda w: w.need().file_control(name, pakbus.FILECTL_DELETE), lambda _: self.refresh_files())

    def run_program(self) -> None:
        name = self.selected_file()
        if not name:
            return
        keep = self.keep_data.isChecked()
        note = "Tables that did not change keep their data." if keep else "Its data tables start empty."
        if self.confirm(f"Compile and run {name}? {note}"):
            code = pakbus.FILECTL_COMPILE_RUN_KEEP_DATA if keep else pakbus.FILECTL_COMPILE_RUN_POWERUP
            self.program_code(name, code)

    def resume_program(self) -> None:
        def job(w: Worker):
            current = w.need().get_program_stats().get("program_name")
            if not current:
                raise PakBusError("no program is loaded")
            return w.file_control(current, pakbus.FILECTL_RESUME)

        self.run(job, self.program_done, "Resuming…")

    def program_code(self, name: str, code: int, question: str | None = None) -> None:
        if question and not self.confirm(question):
            return
        self.stop_monitor()
        self.run(lambda w: w.file_control(name, code), self.program_done, "Waiting for the logger…")

    def program_done(self, info: dict) -> None:
        self.statusBar().showMessage(f"{info['program_name'] or 'No program'}: {info['compile_result'].strip()}")
        self.refresh_files()
        self.refresh_tables()
        self.refresh_station()

    # -- settings --

    def _settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Settings as Device Configuration Utility shows them. Edit a value and Apply: the logger "
            "saves the settings and restarts. Names come from DevConfig's device descriptions when it "
            "is installed."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.settings_table = table_widget(["Id", "Setting", "Value", "Tab"], stretch=2)
        self.settings_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        layout.addWidget(self.settings_table)
        row = QHBoxLayout()
        read = QPushButton("Read settings")
        read.clicked.connect(self.read_settings)
        apply = QPushButton("Apply changes")
        apply.clicked.connect(self.apply_settings)
        row.addWidget(read)
        row.addStretch()
        row.addWidget(apply)
        layout.addLayout(row)
        return page

    def read_settings(self) -> None:
        def done(result) -> None:
            got, defs = result
            self.settings, self.setting_defs = got["settings"], defs
            self.settings_table.setRowCount(len(self.settings))
            for row, (sid, (read_only, raw)) in enumerate(sorted(self.settings.items())):
                definition = defs.get(sid)
                value = settings_lib.decode(definition, raw)
                cells = [
                    str(sid),
                    definition.name if definition else "",
                    shown(value),
                    definition.tab if definition else "",
                ]
                for col, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    editable = col == 2 and not read_only and isinstance(value, (str, int, float, bool))
                    if not editable:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    item.setData(Qt.ItemDataRole.UserRole, text)
                    item.setData(Qt.ItemDataRole.UserRole + 1, sid)
                    self.settings_table.setItem(row, col, item)

        self.run(lambda w: cli.read_settings(w.need()), done, "Reading settings…")

    def apply_settings(self) -> None:
        changes = {}
        for row in range(self.settings_table.rowCount()):
            item = self.settings_table.item(row, 2)
            if item and item.text() != item.data(Qt.ItemDataRole.UserRole):
                sid = int(item.data(Qt.ItemDataRole.UserRole + 1))
                try:
                    changes[sid] = settings_lib.encode(self.setting_defs.get(sid), item.text())
                except ValueError as exc:
                    QMessageBox.warning(self, "Settings", f"Setting {sid}: {exc}")
                    return
        if not changes:
            QMessageBox.information(self, "Settings", "No setting was changed.")
            return
        if not self.confirm(f"Write {len(changes)} setting(s)? The logger will save them and restart."):
            return
        self.stop_monitor()

        def job(w: Worker):
            link = w.need()
            outcomes = link.set_settings(changes)
            refused = {s: c for s, c in outcomes.items() if c != 1}
            if refused:
                raise PakBusError(f"the logger refused setting(s) {refused}; nothing was saved")
            time.sleep(3)
            flash.reconnect(link, w.port, model=link.model)
            return cli.read_settings(link)

        self.run(job, lambda result: (self.read_settings(), self.refresh_station()), "Writing settings…")

    # -- terminal --

    def _terminal_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.terminal_text = QPlainTextEdit()
        self.terminal_text.setReadOnly(True)
        self.terminal_text.setFont(QFont("Consolas", 10))
        layout.addWidget(self.terminal_text)
        row = QHBoxLayout()
        self.terminal_input = QLineEdit()
        self.terminal_input.setPlaceholderText("Command, then Enter")
        self.terminal_input.returnPressed.connect(self.terminal_send)
        self.terminal_button = QPushButton("Open terminal")
        self.terminal_button.clicked.connect(self.toggle_terminal)
        row.addWidget(self.terminal_input, 1)
        row.addWidget(self.terminal_button)
        layout.addLayout(row)
        return page

    def toggle_terminal(self) -> None:
        if self.terminal_tran is not None:
            self.close_terminal()
            return
        self.stop_monitor()

        def opened(result: tuple[int, bytes]) -> None:
            self.terminal_tran, prompt = result
            self.terminal_button.setText("Close terminal")
            self.terminal_timer.start(300)
            self.terminal_input.setFocus()
            self.terminal_text.insertPlainText(prompt.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n"))

        self.run(lambda w: w.need().terminal_open(), opened, "Opening the terminal…")

    def close_terminal(self) -> None:
        self.terminal_timer.stop()
        self.terminal_tran = None
        self.terminal_button.setText("Open terminal")

    def terminal_send(self) -> None:
        tran = self.terminal_tran
        if tran is None:
            return
        text = self.terminal_input.text()
        self.terminal_input.clear()
        self.worker.submit(lambda w: w.need().terminal_write(tran, text.encode("latin-1") + b"\r"))

    def terminal_poll(self) -> None:
        tran = self.terminal_tran
        if tran is None or not self.worker.idle():
            return

        def done(data: bytes) -> None:
            if data:
                text = data.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n")
                self.terminal_text.moveCursor(self.terminal_text.textCursor().MoveOperation.End)
                self.terminal_text.insertPlainText(text)
                self.terminal_text.ensureCursorVisible()

        self.worker.submit(lambda w: w.need().terminal_read(tran, 0.2), done)

    # -- operating system --

    def _os_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel(
            "Sends a new operating system the way Device Configuration Utility's Manage OS does: the "
            "program is stopped and its data deleted, the .obj image is sent and loaded, and the "
            "program is put back. Do not disconnect the logger while this runs."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        self.os_path = QLineEdit()
        browse = QPushButton("Choose .obj…")
        browse.clicked.connect(self.choose_os)
        send = QPushButton("Send operating system")
        send.clicked.connect(self.send_os)
        row.addWidget(self.os_path, 1)
        row.addWidget(browse)
        row.addWidget(send)
        layout.addLayout(row)
        self.os_progress = QProgressBar()
        layout.addWidget(self.os_progress)
        self.os_log = QPlainTextEdit()
        self.os_log.setReadOnly(True)
        layout.addWidget(self.os_log)
        return page

    def choose_os(self) -> None:
        start = Path.home() / "Documents" / "CampbellSci" / "DevConfig" / "OperatingSystems"
        path, _ = QFileDialog.getOpenFileName(self, "Operating system image", str(start), "OS images (*.obj)")
        if path:
            self.os_path.setText(path)

    def send_os(self) -> None:
        image = Path(self.os_path.text())
        if not image.is_file():
            QMessageBox.information(self, "Operating system", "Choose an .obj image first.")
            return
        if not self.confirm(f"Send {image.name} to the logger? Its data tables are deleted."):
            return
        self.stop_monitor()
        log = self.os_log

        def say(text: str) -> None:
            self.worker.progress.emit(lambda *_: log.appendPlainText(text), 0, 0)

        def progress(done: int, total: int) -> None:
            self.worker.progress.emit(self._os_progress, done, total)

        def job(w: Worker):
            return os_update.send_os(w.need(), image, port=w.port, say=say, progress=progress)

        def done(result: dict) -> None:
            log.appendPlainText(f"done: OS {result['before']} -> {result['after']}, running {result['program']}")
            self.refresh_station()

        self.run(job, done, f"Sending {image.name}…")

    def _os_progress(self, done: int, total: int) -> None:
        self.os_progress.setMaximum(max(total, 1))
        self.os_progress.setValue(done)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("open-loggernet")
    window = Window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
