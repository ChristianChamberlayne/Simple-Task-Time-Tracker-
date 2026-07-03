#%% Imports & constants
import csv
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QFileDialog,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
STATE_FILE = APP_DIR / "task_tracker_state.json"
DEFAULT_LOG_FILE = APP_DIR / "task_time_log.csv"
TIMER_INTERVAL_MS = 1000
CSV_HEADER = ["timestamp", "task", "segment_seconds", "cumulative_seconds"]


#%% Data model
@dataclass
class Task:
    id: str
    name: str
    cumulative_seconds: float = 0.0


@dataclass
class AppState:
    log_file_path: str = str(DEFAULT_LOG_FILE)
    tasks: list[Task] = field(default_factory=list)
    active_task_id: str | None = None
    active_since: datetime | None = None


#%% Helpers
def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def segment_seconds(active_since: datetime | None) -> float:
    if active_since is None:
        return 0.0
    return max(0.0, (datetime.now() - active_since).total_seconds())


#%% TaskRowWidget
class TaskRowWidget(QWidget):
    toggle_clicked = pyqtSignal(str)
    delete_requested = pyqtSignal(str)
    name_changed = pyqtSignal(str, str)

    def __init__(self, task_id: str, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.task_id = task_id

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(8)

        self.toggle_btn = QPushButton("OFF")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setMinimumHeight(48)
        self.toggle_btn.setMinimumWidth(80)
        self.toggle_btn.setStyleSheet(
            "QPushButton { font-size: 14px; font-weight: bold; }"
            "QPushButton:checked { background-color: #4caf50; color: white; }"
        )
        self.toggle_btn.clicked.connect(lambda: self.toggle_clicked.emit(self.task_id))
        layout.addWidget(self.toggle_btn)

        self.name_input = QLineEdit(name)
        self.name_input.setFont(QFont("", 12))
        self.name_input.setPlaceholderText("Task name…")
        self.name_input.editingFinished.connect(self._on_name_edited)
        layout.addWidget(self.name_input, stretch=1)

        self.time_label = QLabel("00:00:00")
        self.time_label.setFont(QFont("Consolas", 12))
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.time_label)

        self.delete_btn = QPushButton("×")
        self.delete_btn.setFixedSize(28, 28)
        self.delete_btn.setToolTip("Delete task")
        self.delete_btn.clicked.connect(lambda: self.delete_requested.emit(self.task_id))
        layout.addWidget(self.delete_btn)

    def _on_name_edited(self) -> None:
        self.name_changed.emit(self.task_id, self.name_input.text().strip())

    def set_name(self, name: str) -> None:
        self.name_input.blockSignals(True)
        self.name_input.setText(name)
        self.name_input.blockSignals(False)

    def set_checked(self, checked: bool) -> None:
        self.toggle_btn.blockSignals(True)
        self.toggle_btn.setChecked(checked)
        self.toggle_btn.setText("ON" if checked else "OFF")
        self.toggle_btn.blockSignals(False)

    def set_total_seconds(self, seconds: float) -> None:
        self.time_label.setText(format_duration(seconds))


#%% TaskTimeTrackerWindow
class TaskTimeTrackerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Experiment 158")
        self.resize(520, 640)

        self.state = AppState()
        self._row_widgets: dict[str, TaskRowWidget] = {}
        self._suppress_toggle = False

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(TIMER_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh_displays)
        self._timer.start()

        self._load_state()
        self._rebuild_task_list()
        self._refresh_displays()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        top_bar = QHBoxLayout()
        self.menu_btn = QToolButton()
        self.menu_btn.setText("Settings ▾")
        self.menu_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self)
        menu.addAction("Set log file location…", self._set_log_file_path)
        menu.addSeparator()
        menu.addAction("Clear current task segment", self._clear_current_segment)
        menu.addAction("Clear current task cumulative time", self._clear_current_cumulative)
        menu.addSeparator()
        menu.addAction("Clear all task times", self._clear_times)
        menu.addAction("Clear all tasks", self._clear_all_tasks)
        self.menu_btn.setMenu(menu)
        top_bar.addWidget(self.menu_btn)

        self.log_path_label = QLabel()
        self.log_path_label.setStyleSheet("color: #666;")
        self.log_path_label.setWordWrap(True)
        top_bar.addWidget(self.log_path_label, stretch=1)
        root.addLayout(top_bar)

        banner = QWidget()
        banner.setStyleSheet(
            "background-color: #f0f4f8; border: 1px solid #ccd6e0; border-radius: 6px;"
        )
        banner_layout = QVBoxLayout(banner)
        banner_layout.setContentsMargins(16, 12, 16, 12)

        self.active_name_label = QLabel("No task selected")
        self.active_name_label.setFont(QFont("", 13))
        self.active_name_label.setAlignment(Qt.AlignCenter)
        banner_layout.addWidget(self.active_name_label)

        self.session_clock_label = QLabel("—")
        self.session_clock_label.setFont(QFont("Consolas", 28, QFont.Bold))
        self.session_clock_label.setAlignment(Qt.AlignCenter)
        self.session_clock_label.setStyleSheet("color: #2c3e50;")
        banner_layout.addWidget(self.session_clock_label)
        root.addWidget(banner)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        self.task_container = QWidget()
        self.task_list_layout = QVBoxLayout(self.task_container)
        self.task_list_layout.setSpacing(6)
        self.task_list_layout.setContentsMargins(0, 0, 0, 0)
        self.task_list_layout.addStretch()
        scroll.setWidget(self.task_container)
        root.addWidget(scroll, stretch=1)

        add_row = QHBoxLayout()
        self.add_input = QLineEdit()
        self.add_input.setPlaceholderText("New task name…")
        self.add_input.returnPressed.connect(self._add_task_from_input)
        add_row.addWidget(self.add_input, stretch=1)

        add_btn = QPushButton("Add Task")
        add_btn.clicked.connect(self._add_task_from_input)
        add_row.addWidget(add_btn)
        root.addLayout(add_row)

    def _set_log_path_label(self) -> None:
        self.log_path_label.setText(f"Log: {self.state.log_file_path}")

    def _get_task(self, task_id: str) -> Task | None:
        for task in self.state.tasks:
            if task.id == task_id:
                return task
        return None

    def _rebuild_task_list(self) -> None:
        while self.task_list_layout.count() > 1:
            item = self.task_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._row_widgets.clear()

        for task in self.state.tasks:
            row = TaskRowWidget(task.id, task.name)
            row.toggle_clicked.connect(self._on_row_toggle_clicked)
            row.delete_requested.connect(self._delete_task)
            row.name_changed.connect(self._on_task_name_changed)
            self._row_widgets[task.id] = row
            self.task_list_layout.insertWidget(self.task_list_layout.count() - 1, row)

        self._sync_toggle_buttons()

    def _sync_toggle_buttons(self) -> None:
        self._suppress_toggle = True
        for task_id, row in self._row_widgets.items():
            row.set_checked(task_id == self.state.active_task_id)
        self._suppress_toggle = False

    def _on_row_toggle_clicked(self, task_id: str) -> None:
        if self._suppress_toggle:
            return

        if self.state.active_task_id == task_id:
            self._activate_task(None)
        else:
            self._activate_task(task_id)

    def _on_task_name_changed(self, task_id: str, new_name: str) -> None:
        task = self._get_task(task_id)
        row = self._row_widgets.get(task_id)
        if task is None or row is None:
            return

        if not new_name:
            row.set_name(task.name)
            return

        if new_name == task.name:
            row.set_name(task.name)
            return

        task.name = new_name
        self._refresh_displays()
        self._save_state()

    def _activate_task(self, task_id: str | None) -> None:
        now = datetime.now()

        if self.state.active_task_id is not None:
            prev_task = self._get_task(self.state.active_task_id)
            if prev_task is not None and self.state.active_since is not None:
                seg = segment_seconds(self.state.active_since)
                prev_task.cumulative_seconds += seg
                self._append_log_row(prev_task.name, seg, prev_task.cumulative_seconds)

        if task_id is None:
            self.state.active_task_id = None
            self.state.active_since = None
        else:
            self.state.active_task_id = task_id
            self.state.active_since = now

        self._sync_toggle_buttons()
        self._refresh_displays()
        self._save_state()

    def _append_log_row(
        self, task_name: str, segment_seconds_val: float, cumulative_seconds_val: float
    ) -> None:
        log_path = Path(self.state.log_file_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not log_path.exists() or log_path.stat().st_size == 0

        with open(log_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(CSV_HEADER)
            writer.writerow(
                [
                    datetime.now().isoformat(timespec="seconds"),
                    task_name,
                    round(segment_seconds_val, 2),
                    round(cumulative_seconds_val, 2),
                ]
            )

    def _append_log_message(
        self,
        message: str,
        segment_seconds_val: float = 0.0,
        cumulative_seconds_val: float = 0.0,
    ) -> None:
        self._append_log_row(message, segment_seconds_val, cumulative_seconds_val)

    def _get_active_task(self) -> Task | None:
        if self.state.active_task_id is None:
            return None
        return self._get_task(self.state.active_task_id)

    def _refresh_displays(self) -> None:
        active_task = (
            self._get_task(self.state.active_task_id)
            if self.state.active_task_id
            else None
        )

        if active_task is None:
            self.active_name_label.setText("No task selected")
            self.session_clock_label.setText("—")
        else:
            self.active_name_label.setText(active_task.name)
            self.session_clock_label.setText(
                format_duration(segment_seconds(self.state.active_since))
            )

        for task in self.state.tasks:
            row = self._row_widgets.get(task.id)
            if row is None:
                continue
            total = task.cumulative_seconds
            if task.id == self.state.active_task_id:
                total += segment_seconds(self.state.active_since)
            row.set_total_seconds(total)

        self._set_log_path_label()

    def _add_task_from_input(self) -> None:
        name = self.add_input.text().strip()
        if not name:
            return
        self._add_task(name)
        self.add_input.clear()

    def _add_task(self, name: str) -> None:
        task = Task(id=str(uuid.uuid4()), name=name)
        self.state.tasks.append(task)
        self._rebuild_task_list()
        self._refresh_displays()
        self._save_state()

    def _delete_task(self, task_id: str) -> None:
        if self.state.active_task_id == task_id:
            self._activate_task(None)

        self.state.tasks = [t for t in self.state.tasks if t.id != task_id]
        self._rebuild_task_list()
        self._refresh_displays()
        self._save_state()

    def _set_log_file_path(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Select log file",
            self.state.log_file_path,
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        self.state.log_file_path = path
        self._set_log_path_label()
        self._save_state()

    def _clear_current_segment(self) -> None:
        task = self._get_active_task()
        if task is None or self.state.active_since is None:
            QMessageBox.information(
                self,
                "No active task",
                "Select a task before clearing its current segment.",
            )
            return

        discarded = segment_seconds(self.state.active_since)
        reply = QMessageBox.question(
            self,
            "Clear current task segment",
            (
                f"Discard the current session segment for “{task.name}” "
                f"({format_duration(discarded)}) without adding it to cumulative time?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.state.active_since = datetime.now()
        self._append_log_message(
            f"[Clear segment] {task.name} — segment discarded, not added to cumulative",
            discarded,
            task.cumulative_seconds,
        )
        self._refresh_displays()
        self._save_state()

    def _clear_current_cumulative(self) -> None:
        task = self._get_active_task()
        if task is None:
            QMessageBox.information(
                self,
                "No active task",
                "Select a task before clearing its cumulative time.",
            )
            return

        reply = QMessageBox.question(
            self,
            "Clear current task cumulative time",
            f"Reset cumulative time for “{task.name}” to zero?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        previous_cumulative = task.cumulative_seconds
        task.cumulative_seconds = 0.0
        self._append_log_message(
            f"[Clear cumulative] {task.name} — cumulative time reset to 0",
            previous_cumulative,
            0.0,
        )
        self._refresh_displays()
        self._save_state()

    def _clear_times(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear all task times",
            "Reset cumulative time for every task to zero?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        for task in self.state.tasks:
            task.cumulative_seconds = 0.0
        self._append_log_message("[Clear all task times] All cumulative times reset to 0")
        self._refresh_displays()
        self._save_state()

    def _clear_all_tasks(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear all tasks",
            "Remove all tasks? Active session will be logged first.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        if self.state.active_task_id is not None:
            self._activate_task(None)

        self.state.tasks.clear()
        self._rebuild_task_list()
        self._append_log_message("[Clear all tasks] All tasks removed")
        self._refresh_displays()
        self._save_state()

    def _save_state(self) -> None:
        data = {
            "log_file_path": self.state.log_file_path,
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "cumulative_seconds": t.cumulative_seconds,
                }
                for t in self.state.tasks
            ],
            "active_task_id": self.state.active_task_id,
            "active_since": (
                self.state.active_since.isoformat(timespec="seconds")
                if self.state.active_since
                else None
            ),
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            self._set_log_path_label()
            return

        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)

        self.state.log_file_path = data.get("log_file_path", str(DEFAULT_LOG_FILE))
        self.state.tasks = [
            Task(
                id=t["id"],
                name=t["name"],
                cumulative_seconds=float(t.get("cumulative_seconds", 0.0)),
            )
            for t in data.get("tasks", [])
        ]
        self.state.active_task_id = data.get("active_task_id")
        active_since = data.get("active_since")
        self.state.active_since = (
            datetime.fromisoformat(active_since) if active_since else None
        )
        self._set_log_path_label()

    def closeEvent(self, event) -> None:
        self._save_state()
        super().closeEvent(event)


#%% main
def main() -> None:
    app = QApplication([])
    window = TaskTimeTrackerWindow()
    window.show()
    app.exec_()


if __name__ == "__main__":
    main()
