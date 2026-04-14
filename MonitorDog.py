import sys
import os
import json
import time
import psutil
import sqlite3
import winreg
import pyqtgraph as pg
from PyQt5.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QWidget,
                             QSystemTrayIcon, QMenu, QAction, QActionGroup, qApp, QStyle, QTabWidget,
                             QPushButton, QDateTimeEdit, QLabel, QDialog)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QDateTime
from PyQt5.QtGui import QIcon, QPixmap

try:
    import pynvml

    HAS_PYNVML = True
except ImportError:
    HAS_PYNVML = False


def get_resource_path(relative_path):
    """获取资源绝对路径，兼容 Nuitka 与源码运行"""
    base_path = os.path.dirname(os.path.abspath(sys.argv[0]))
    if not getattr(sys, 'frozen', False):
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def get_data_dir():
    """获取 Local AppData 存储目录"""
    appdata = os.getenv('LOCALAPPDATA')
    if not appdata:
        appdata = os.path.expanduser('~')

    target_dir = os.path.join(appdata, "LightMonitor")
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)

    return target_dir


class TimeAxisItem(pg.DateAxisItem):
    """自定义时间坐标轴"""

    def tickStrings(self, values, scale, spacing):
        strings = []
        for v in values:
            dt = QDateTime.fromSecsSinceEpoch(int(v))
            if spacing > 86400:
                strings.append(dt.toString("MM-dd"))
            elif spacing > 3600:
                strings.append(dt.toString("MM-dd HH:mm"))
            else:
                strings.append(dt.toString("HH:mm:ss"))
        return strings


class AboutDialog(QDialog):
    """关于窗口"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于 LightMonitor")
        self.setFixedSize(320, 200)
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)

        self.setStyleSheet("""
            QDialog { background-color: #1e1e1e; }
            QLabel { color: #d4d4d4; font-family: 'Segoe UI', Arial, sans-serif; }
            QPushButton {
                background-color: #007acc; color: white;
                padding: 6px 20px; font-weight: bold; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #0098ff; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        logo_label = QLabel()
        pixmap = QPixmap(get_resource_path('icon.ico'))
        if not pixmap.isNull():
            logo_label.setPixmap(pixmap.scaled(56, 56, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        logo_label.setAlignment(Qt.AlignCenter)

        title_label = QLabel("<b>LightMonitor</b><br>Version 1.0")
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet("font-size: 13px; line-height: 1.5;")

        link_label = QLabel(
            '<a href="https://thenounproject.com/" style="color: #4ec9b0; text-decoration: none;">Icon by Hamstring from The Noun Project</a>')
        link_label.setOpenExternalLinks(True)
        link_label.setAlignment(Qt.AlignCenter)
        link_label.setStyleSheet("font-size: 12px;")

        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("确定")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        btn_layout.addStretch()

        layout.addWidget(logo_label)
        layout.addWidget(title_label)
        layout.addWidget(link_label)
        layout.addSpacing(10)
        layout.addLayout(btn_layout)


class MonitorWorker(QThread):
    """数据采集线程"""
    data_updated = pyqtSignal(float, float, float, int, float, float)
    self_data_updated = pyqtSignal(float, float)
    error_updated = pyqtSignal(str)

    def __init__(self, db_path, retention_days=7):
        super().__init__()
        self.db_path = db_path
        self.running = True
        self.has_gpu = False
        self.gpu_handle = None
        self.retention_days = retention_days

        self.my_process = psutil.Process(os.getpid())
        self.my_process.cpu_percent(interval=None)

    def set_retention_days(self, days):
        self.retention_days = days

    def run(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute('''CREATE TABLE IF NOT EXISTS monitor_data
                          (
                              id
                              INTEGER
                              PRIMARY
                              KEY
                              AUTOINCREMENT,
                              timestamp
                              REAL,
                              cpu_usage
                              REAL,
                              mem_usage
                              REAL,
                              gpu_usage
                              INTEGER,
                              vram_usage
                              REAL,
                              gpu_temp
                              REAL
                          )''')

        cutoff_time = time.time() - (self.retention_days * 24 * 60 * 60)
        cursor.execute("DELETE FROM monitor_data WHERE timestamp < ?", (cutoff_time,))
        conn.commit()
        last_clean_time = time.time()

        psutil.cpu_percent()
        if HAS_PYNVML:
            try:
                pynvml.nvmlInit()
                self.gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                self.has_gpu = True
            except Exception:
                pass

        while self.running:
            current_time = time.time()

            if current_time - last_clean_time >= 3600:
                cutoff = current_time - (self.retention_days * 24 * 60 * 60)
                try:
                    cursor.execute("DELETE FROM monitor_data WHERE timestamp < ?", (cutoff,))
                    conn.commit()
                except Exception:
                    pass
                last_clean_time = current_time

            try:
                self_cpu = self.my_process.cpu_percent(interval=None)
                self_mem = self.my_process.memory_info().rss / 1048576.0

                cpu_usage = psutil.cpu_percent(interval=None)
                mem_usage = psutil.virtual_memory().percent
                gpu_usage, vram_usage, gpu_temp = 0, 0, 0

                if self.has_gpu:
                    util = pynvml.nvmlDeviceGetUtilizationRates(self.gpu_handle)
                    gpu_usage = util.gpu
                    mem_info = pynvml.nvmlDeviceGetMemoryInfo(self.gpu_handle)
                    vram_usage = (mem_info.used / mem_info.total) * 100
                    gpu_temp = pynvml.nvmlDeviceGetTemperature(self.gpu_handle, pynvml.NVML_TEMPERATURE_GPU)

                cursor.execute('''INSERT INTO monitor_data
                                      (timestamp, cpu_usage, mem_usage, gpu_usage, vram_usage, gpu_temp)
                                  VALUES (?, ?, ?, ?, ?, ?)''',
                               (current_time, cpu_usage, mem_usage, gpu_usage, vram_usage, gpu_temp))
                conn.commit()

                self.data_updated.emit(current_time, cpu_usage, mem_usage, gpu_usage, vram_usage, gpu_temp)
                self.self_data_updated.emit(self_cpu, self_mem)

            except Exception as e:
                self.error_updated.emit(f"数据读取/写入异常: {str(e)}")

            time.sleep(1)

        conn.close()
        if self.has_gpu:
            pynvml.nvmlShutdown()

    def stop(self):
        self.running = False
        self.wait()


class LightMonitorApp(QMainWindow):
    """主界面"""

    def __init__(self):
        super().__init__()

        self.base_dir = get_data_dir()
        self.db_path = os.path.join(self.base_dir, "hw_data.db")
        self.config_path = os.path.join(self.base_dir, "config.json")
        self.config = self.load_config()

        self.time_window_seconds = 3600
        self.time_data = []
        self.cpu_data, self.gpu_data = [], []
        self.mem_data, self.temp_data = [], []

        self.initUI()
        self.initTray()

        self.worker = MonitorWorker(self.db_path, self.config.get("retention_days", 7))
        self.worker.data_updated.connect(self.update_chart)
        self.worker.self_data_updated.connect(self.update_self_status)
        self.worker.error_updated.connect(self.show_error_status)
        self.worker.start()

        self.load_history_data()

    def load_config(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if "retention_days" not in data:
                        data["retention_days"] = 7
                    return data
            except:
                pass
        return {"last_tab": 0, "retention_days": 7}

    def save_config(self):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.config, f)

    def create_graph_widget(self, title):
        date_axis = TimeAxisItem(orientation='bottom')
        graph = pg.PlotWidget(axisItems={'bottom': date_axis})
        graph.setTitle(title, color="#d4d4d4", size="12pt")
        graph.showGrid(x=True, y=True, alpha=0.3)
        graph.setYRange(0, 100)
        graph.addLegend(offset=(-10, 10))
        return graph

    def initUI(self):
        self.setWindowIcon(QIcon(get_resource_path('icon.ico')))
        self.setWindowTitle('LightMonitor')
        self.resize(850, 500)

        pg.setConfigOption('background', '#1e1e1e')
        pg.setConfigOption('foreground', '#d4d4d4')

        self.tabs = QTabWidget()
        # 【修改核心】：增加 min-width: 80px; 强制撑开 Tab 宽度防止削边
        self.tabs.setStyleSheet("""
            QTabBar::tab { background: #2d2d30; color: #d4d4d4; padding: 8px 20px; font-weight: bold; min-width: 80px; }
            QTabBar::tab:selected { background: #007acc; color: white; }
            QTabWidget::pane { border: 1px solid #3e3e42; }
            QLabel { color: #d4d4d4; }
        """)

        self.tab_all = self.create_graph_widget("总览")
        self.line_all_cpu = self.tab_all.plot(name="CPU (%)", pen=pg.mkPen(color='#569cd6', width=2))
        self.line_all_gpu = self.tab_all.plot(name="GPU (%)", pen=pg.mkPen(color='#4ec9b0', width=2))
        self.line_all_mem = self.tab_all.plot(name="内存 (%)", pen=pg.mkPen(color='#ce9178', width=2))
        self.line_all_temp = self.tab_all.plot(name="温度 (°C)", pen=pg.mkPen(color='#d16969', width=2))

        self.tab_cpu = self.create_graph_widget("CPU")
        self.line_single_cpu = self.tab_cpu.plot(name="CPU (%)", pen=pg.mkPen(color='#569cd6', width=2), fillLevel=0,
                                                 brush=(86, 156, 214, 50))

        self.tab_gpu = self.create_graph_widget("GPU")
        self.line_single_gpu = self.tab_gpu.plot(name="占用率 (%)", pen=pg.mkPen(color='#4ec9b0', width=2))
        self.line_single_temp = self.tab_gpu.plot(name="温度 (°C)", pen=pg.mkPen(color='#d16969', width=2))

        self.has_nvidia = False
        if HAS_PYNVML:
            try:
                pynvml.nvmlInit()
                pynvml.nvmlDeviceGetHandleByIndex(0)
                self.has_nvidia = True
                pynvml.nvmlShutdown()
            except Exception:
                pass

        if not self.has_nvidia:
            self.line_all_gpu.hide()
            self.line_all_temp.hide()
            self.line_single_gpu.hide()
            self.line_single_temp.hide()

            self.gpu_warning_text = pg.TextItem("NVIDIA GPU not detected", color='#555555', anchor=(0.5, 0.5))
            font = self.gpu_warning_text.textItem.font()
            font.setPointSize(14)
            self.gpu_warning_text.setFont(font)
            self.tab_gpu.addItem(self.gpu_warning_text)

        self.tab_mem = self.create_graph_widget("内存")
        self.line_single_mem = self.tab_mem.plot(name="内存 (%)", pen=pg.mkPen(color='#ce9178', width=2), fillLevel=0,
                                                 brush=(206, 145, 120, 50))

        self.tab_history = QWidget()
        hist_layout = QVBoxLayout()
        ctrl_layout = QHBoxLayout()

        btn_style = "padding: 6px 12px; background-color: #007acc; color: white; font-weight: bold; border-radius: 4px;"
        self.btn_1h = QPushButton("过去 1 小时")
        self.btn_5h = QPushButton("过去 5 小时")
        self.btn_24h = QPushButton("过去 24 小时")

        dt_style = "background-color: #333; color: white; padding: 4px; border: 1px solid #555;"
        self.dt_start = QDateTimeEdit(QDateTime.currentDateTime().addSecs(-3600))
        self.dt_start.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.dt_end = QDateTimeEdit(QDateTime.currentDateTime())
        self.dt_end.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.dt_start.setStyleSheet(dt_style)
        self.dt_end.setStyleSheet(dt_style)

        self.btn_search = QPushButton("查询")

        for btn in [self.btn_1h, self.btn_5h, self.btn_24h, self.btn_search]:
            btn.setStyleSheet(btn_style)

        self.btn_1h.clicked.connect(lambda: self.do_quick_query(1))
        self.btn_5h.clicked.connect(lambda: self.do_quick_query(5))
        self.btn_24h.clicked.connect(lambda: self.do_quick_query(24))
        self.btn_search.clicked.connect(self.do_custom_query)

        ctrl_layout.addWidget(self.btn_1h)
        ctrl_layout.addWidget(self.btn_5h)
        ctrl_layout.addWidget(self.btn_24h)
        ctrl_layout.addSpacing(20)
        ctrl_layout.addWidget(QLabel("从:"))
        ctrl_layout.addWidget(self.dt_start)
        ctrl_layout.addWidget(QLabel("至:"))
        ctrl_layout.addWidget(self.dt_end)
        ctrl_layout.addWidget(self.btn_search)
        ctrl_layout.addStretch()

        self.graph_history = self.create_graph_widget("历史峰值")
        self.line_hist_cpu = self.graph_history.plot(name="CPU 峰值(%)", pen=pg.mkPen(color='#569cd6', width=2))
        self.line_hist_gpu = self.graph_history.plot(name="GPU 峰值(%)", pen=pg.mkPen(color='#4ec9b0', width=2))
        self.line_hist_temp = self.graph_history.plot(name="温度 峰值(°C)", pen=pg.mkPen(color='#d16969', width=2))

        if not self.has_nvidia:
            self.line_hist_gpu.hide()
            self.line_hist_temp.hide()

        hist_layout.addLayout(ctrl_layout)
        hist_layout.addWidget(self.graph_history)
        self.tab_history.setLayout(hist_layout)

        self.tabs.addTab(self.tab_all, "总览")
        self.tabs.addTab(self.tab_cpu, "CPU")
        self.tabs.addTab(self.tab_gpu, "GPU")
        self.tabs.addTab(self.tab_mem, "内存")
        self.tabs.addTab(self.tab_history, "历史复盘")

        central_widget = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self.tabs)
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)

        self.statusBar().setStyleSheet("color: #4ec9b0; font-weight: bold; background-color: #1e1e1e;")
        self.statusBar().showMessage("初始化监控...")

        self.tabs.setCurrentIndex(self.config.get("last_tab", 0))
        self.tabs.currentChanged.connect(self.on_tab_changed)

    def on_tab_changed(self, index):
        self.config["last_tab"] = index
        self.save_config()
        self.redraw_active_lines()

    def update_self_status(self, self_cpu, self_mem):
        if not self.isVisible(): return
        self.statusBar().setStyleSheet("color: #4ec9b0; font-weight: bold; background-color: #1e1e1e;")
        self.statusBar().showMessage(f"当前进程 | CPU: {self_cpu:.2f}% | 内存: {self_mem:.1f} MB")

    def show_error_status(self, error_msg):
        if not self.isVisible(): return
        self.statusBar().setStyleSheet("color: #ff4d4d; font-weight: bold; background-color: #1e1e1e;")
        self.statusBar().showMessage(error_msg)

    def get_autostart_status(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                 winreg.KEY_READ)
            winreg.QueryValueEx(key, "LightMonitor")
            winreg.CloseKey(key)
            return True
        except:
            return False

    def toggle_autostart(self, checked):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE)
            if checked:
                if getattr(sys, 'frozen', False):
                    path = sys.executable
                else:
                    path = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
                winreg.SetValueEx(key, "LightMonitor", 0, winreg.REG_SZ, path)
            else:
                winreg.DeleteValue(key, "LightMonitor")
            winreg.CloseKey(key)
        except Exception as e:
            pass

    def change_retention_days(self, days):
        self.config["retention_days"] = days
        self.save_config()
        self.worker.set_retention_days(days)

    def initTray(self):
        self.tray_icon = QSystemTrayIcon(self)
        icon = QIcon(get_resource_path('icon.ico'))
        self.tray_icon.setIcon(icon)

        tray_menu = QMenu()
        show_action = QAction("显示主界面", self)
        show_action.triggered.connect(self.showNormal)

        retention_menu = QMenu("数据保留", self)
        self.retention_group = QActionGroup(self)
        self.retention_group.setExclusive(True)

        action_7 = QAction("7 天", self, checkable=True)
        action_14 = QAction("14 天", self, checkable=True)
        action_30 = QAction("30 天", self, checkable=True)

        self.retention_group.addAction(action_7)
        self.retention_group.addAction(action_14)
        self.retention_group.addAction(action_30)

        current_days = self.config.get("retention_days", 7)
        if current_days == 30:
            action_30.setChecked(True)
        elif current_days == 14:
            action_14.setChecked(True)
        else:
            action_7.setChecked(True)

        action_7.triggered.connect(lambda: self.change_retention_days(7))
        action_14.triggered.connect(lambda: self.change_retention_days(14))
        action_30.triggered.connect(lambda: self.change_retention_days(30))

        retention_menu.addAction(action_7)
        retention_menu.addAction(action_14)
        retention_menu.addAction(action_30)

        autostart_action = QAction("开机自启", self)
        autostart_action.setCheckable(True)
        autostart_action.setChecked(self.get_autostart_status())
        autostart_action.triggered.connect(self.toggle_autostart)

        about_action = QAction("关于", self)
        about_action.triggered.connect(self.show_about_dialog)

        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.safe_quit)

        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addMenu(retention_menu)
        tray_menu.addAction(autostart_action)
        tray_menu.addSeparator()
        tray_menu.addAction(about_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()
        self.tray_icon.activated.connect(self.on_tray_icon_activated)

    def show_about_dialog(self):
        dialog = AboutDialog(self)
        dialog.exec_()

    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.load_history_data()
            self.showNormal()
            self.activateWindow()

    def load_history_data(self):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            current_time = time.time()
            cutoff_time = current_time - self.time_window_seconds
            cursor.execute('''SELECT timestamp, cpu_usage, mem_usage, gpu_usage, gpu_temp
                              FROM monitor_data
                              WHERE timestamp >= ?
                              ORDER BY timestamp DESC''', (cutoff_time,))
            records = cursor.fetchall()
            conn.close()

            if not records: return
            records.reverse()
            self.time_data.clear()
            self.cpu_data.clear()
            self.mem_data.clear()
            self.gpu_data.clear()
            self.temp_data.clear()

            for row in records:
                self.time_data.append(row[0])
                self.cpu_data.append(row[1])
                self.mem_data.append(row[2])
                self.gpu_data.append(row[3])
                self.temp_data.append(row[4])

            self.redraw_active_lines()
        except Exception:
            pass

    def update_chart(self, timestamp, cpu, mem, gpu, vram, temp):
        self.time_data.append(timestamp)
        self.cpu_data.append(cpu)
        self.gpu_data.append(gpu)
        self.mem_data.append(mem)
        self.temp_data.append(temp)

        cutoff_time = timestamp - self.time_window_seconds
        while self.time_data and self.time_data[0] < cutoff_time:
            self.time_data.pop(0)
            self.cpu_data.pop(0)
            self.gpu_data.pop(0)
            self.mem_data.pop(0)
            self.temp_data.pop(0)

        if not self.isVisible(): return
        self.redraw_active_lines()

    def redraw_active_lines(self):
        if not self.time_data: return
        x_data = self.time_data

        latest_cpu = f"{self.cpu_data[-1]:.1f}"
        latest_gpu = f"{self.gpu_data[-1]}"
        latest_mem = f"{self.mem_data[-1]:.1f}"
        latest_temp = f"{self.temp_data[-1]:.1f}"

        current_index = self.tabs.currentIndex()

        if current_index == 0:
            self.line_all_cpu.setData(x_data, self.cpu_data)
            self.line_all_mem.setData(x_data, self.mem_data)

            if self.has_nvidia:
                self.line_all_gpu.setData(x_data, self.gpu_data)
                self.line_all_temp.setData(x_data, self.temp_data)
                self.tab_all.setTitle(
                    f"总览 | CPU: {latest_cpu}% | GPU: {latest_gpu}% | 内存: {latest_mem}% | 温度: {latest_temp}°C",
                    color="#d4d4d4", size="11pt")
            else:
                self.tab_all.setTitle(
                    f"总览 | CPU: {latest_cpu}% | 内存: {latest_mem}%",
                    color="#d4d4d4", size="11pt")

        elif current_index == 1:
            self.line_single_cpu.setData(x_data, self.cpu_data)
            self.tab_cpu.setTitle(f"CPU | 占用: {latest_cpu}%", color="#569cd6", size="12pt")

        elif current_index == 2:
            if self.has_nvidia:
                self.line_single_gpu.setData(x_data, self.gpu_data)
                self.line_single_temp.setData(x_data, self.temp_data)
                self.tab_gpu.setTitle(f"GPU | 占用: {latest_gpu}% | 温度: {latest_temp}°C",
                                      color="#4ec9b0", size="12pt")
            else:
                self.tab_gpu.setTitle("GPU | NVIDIA not detected", color="#555555", size="12pt")
                if hasattr(self, 'gpu_warning_text'):
                    mid_index = len(x_data) // 2
                    self.gpu_warning_text.setPos(x_data[mid_index], 50)

        elif current_index == 3:
            self.line_single_mem.setData(x_data, self.mem_data)
            self.tab_mem.setTitle(f"内存 | 占用: {latest_mem}%", color="#ce9178", size="12pt")

    def do_quick_query(self, hours):
        end_ts = time.time()
        start_ts = end_ts - (hours * 3600)
        self.dt_start.setDateTime(QDateTime.fromSecsSinceEpoch(int(start_ts)))
        self.dt_end.setDateTime(QDateTime.fromSecsSinceEpoch(int(end_ts)))
        self.execute_history_query(start_ts, end_ts)

    def do_custom_query(self):
        start_ts = self.dt_start.dateTime().toSecsSinceEpoch()
        end_ts = self.dt_end.dateTime().toSecsSinceEpoch()
        if start_ts >= end_ts:
            self.graph_history.setTitle("无效时间区间", color="#ff4d4d")
            return
        self.execute_history_query(start_ts, end_ts)

    def execute_history_query(self, start_ts, end_ts):
        delta = end_ts - start_ts
        if delta <= 3600:
            bucket = 1
        elif delta <= 18000:
            bucket = 5
        else:
            bucket = 60
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            if bucket == 1:
                cursor.execute('''SELECT timestamp, cpu_usage, gpu_usage, gpu_temp
                                  FROM monitor_data
                                  WHERE timestamp >= ? AND timestamp <= ?
                                  ORDER BY timestamp ASC''', (start_ts, end_ts))
            else:
                cursor.execute(
                    f'''SELECT CAST(timestamp / {bucket} AS INTEGER) * {bucket} AS bucket_time, MAX(cpu_usage), MAX(gpu_usage), MAX(gpu_temp) FROM monitor_data WHERE timestamp >= ? AND timestamp <= ? GROUP BY bucket_time ORDER BY bucket_time ASC''',
                    (start_ts, end_ts))
            records = cursor.fetchall()
            conn.close()
            if not records:
                self.graph_history.setTitle("无数据", color="#555555")
                return
            t_data = [r[0] for r in records]
            c_data = [r[1] for r in records]
            g_data = [r[2] for r in records]
            tp_data = [r[3] for r in records]

            self.line_hist_cpu.setData(t_data, c_data)
            if self.has_nvidia:
                self.line_hist_gpu.setData(t_data, g_data)
                self.line_hist_temp.setData(t_data, tp_data)

            self.graph_history.setTitle(f"数据加载完成 (精度: {bucket}s)", color="#4ec9b0", size="11pt")
        except:
            self.graph_history.setTitle("查询错误", color="#ff4d4d")

    def closeEvent(self, event):
        event.ignore()
        self.save_config()
        self.hide()

    def safe_quit(self):
        self.save_config()
        self.worker.stop()
        qApp.quit()


if __name__ == '__main__':
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    main_win = LightMonitorApp()
    main_win.show()
    sys.exit(app.exec_())
