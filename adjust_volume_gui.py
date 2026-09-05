#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import glob
import subprocess
import time
import tempfile
import threading
import shutil
from pathlib import Path

# 尝试导入PyQt5
try:
    from PyQt5.QtWidgets import *
    from PyQt5.QtCore import *
    from PyQt5.QtGui import *
except ImportError:
    print("❌ 未安装PyQt5，请运行: pip install PyQt5")
    sys.exit(1)


class WorkerThread(QThread):
    """工作线程，处理视频"""
    progress_signal = pyqtSignal(str)
    progress_bar_signal = pyqtSignal(int, int)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, file_paths, target_db=-14, method='dual'):
        super().__init__()
        self.file_paths = file_paths
        self.target_db = target_db
        self.method = method
        self.is_cancelled = False

    def cancel(self):
        self.is_cancelled = True

    def get_video_files(self, paths):
        """递归获取所有视频文件"""
        video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.flv', '.wmv', '.m4v', '.mpg', '.mpeg', '.3gp'}
        video_files = []

        for path in paths:
            path = Path(path)
            if path.is_file() and path.suffix.lower() in video_extensions:
                video_files.append(str(path))
            elif path.is_dir():
                for ext in video_extensions:
                    video_files.extend([str(f) for f in path.rglob(f'*{ext}')])
                    video_files.extend([str(f) for f in path.rglob(f'*{ext.upper()}')])

        # 去重
        return list(set(video_files))

    def get_video_duration(self, video_path):
        """获取视频时长"""
        cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
               '-of', 'default=noprint_wrappers=1:nokey=1', video_path]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return float(result.stdout.strip())
        except:
            return 60

    def get_volume_at_time(self, video_path, time_point, duration=0.5):
        """获取视频在指定时间点的音量"""
        cmd = [
            'ffmpeg',
            '-ss', str(time_point),
            '-t', str(duration),
            '-i', video_path,
            '-af', 'volumedetect',
            '-f', 'null',
            '-'
        ]

        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, timeout=5)
            output = result.stderr

            for line in output.split('\n'):
                if 'mean_volume' in line:
                    parts = line.split(':')
                    if len(parts) >= 2:
                        db_str = parts[1].strip().split(' ')[0]
                        if db_str != '-inf' and db_str != 'nan':
                            return float(db_str)
            return None
        except:
            return None

    def sample_volume_profile(self, video_path, num_samples=20):
        """采样音量轮廓"""
        duration = self.get_video_duration(video_path)

        volumes = []
        for i in range(num_samples + 1):
            if self.is_cancelled:
                return []
            time_point = (i / num_samples) * duration
            if time_point >= duration - 0.3:
                time_point = max(0, duration - 0.3)

            volume = self.get_volume_at_time(video_path, time_point)
            if volume is not None:
                volumes.append(volume)

        return volumes

    def normalize_audio_dual(self, input_video, output_video, target_db=-14):
        """双压缩器处理"""
        try:
            cmd = [
                'ffmpeg',
                '-i', input_video,
                '-af',
                'compand=attacks=0.1:decays=0.3:points=-80/-80|-40/-30|-25/-20|-15/-15|-8/-13|0/-13:soft-knee=8,'
                'compand=attacks=0.02:decays=0.1:points=-80/-80|-35/-25|-25/-18|-18/-14|-12/-12|-5/-12|0/-12:soft-knee=10:gain=6,'
                f'volume={10 ** ((target_db - (-14)) / 20)}',
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-movflags', '+faststart',
                output_video,
                '-y'
            ]

            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return result.returncode == 0
        except Exception as e:
            print(f"双压缩器处理失败: {e}")
            return False

    def normalize_audio_aggressive(self, input_video, output_video, target_db=-14):
        """两段式处理"""
        temp_file = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
        temp_path = temp_file.name
        temp_file.close()

        try:
            # 第一步：强压缩
            cmd1 = [
                'ffmpeg',
                '-i', input_video,
                '-af',
                'compand=attacks=0.02:decays=0.1:'
                'points=-80/-80|-50/-35|-40/-25|-30/-18|-20/-14|-10/-12|0/-12:'
                'soft-knee=12:gain=6',
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-b:a', '192k',
                temp_path,
                '-y'
            ]
            result = subprocess.run(cmd1, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            if result.returncode != 0:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                return False

            # 第二步：响度标准化
            cmd2 = [
                'ffmpeg',
                '-i', temp_path,
                '-af', f'loudnorm=I={target_db}:LRA=7:TP=-1.5',
                '-c:v', 'copy',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-movflags', '+faststart',
                output_video,
                '-y'
            ]
            result = subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            if os.path.exists(temp_path):
                os.unlink(temp_path)
            return result.returncode == 0

        except Exception as e:
            print(f"两段式处理失败: {e}")
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            return False

    def process_single_video(self, video_path, output_folder):
        """处理单个视频"""
        filename = os.path.basename(video_path)
        name, ext = os.path.splitext(filename)
        output_path = os.path.join(output_folder, f'{name}_音量统一{ext}')

        # 检查是否已处理
        if os.path.exists(output_path):
            return {'status': 'skipped', 'file': filename, 'output': output_path}

        # 采样处理前音量
        before_volumes = self.sample_volume_profile(video_path)
        before_mean = sum(before_volumes) / len(before_volumes) if before_volumes else 0
        before_std = (sum((v - before_mean) ** 2 for v in before_volumes) / len(
            before_volumes)) ** 0.5 if before_volumes else 0

        # 处理音频
        if self.method == 'dual':
            success = self.normalize_audio_dual(video_path, output_path, self.target_db)
        else:
            success = self.normalize_audio_aggressive(video_path, output_path, self.target_db)

        if not success:
            return {'status': 'failed', 'file': filename, 'error': '处理失败'}

        # 采样处理后音量
        after_volumes = self.sample_volume_profile(output_path)
        after_mean = sum(after_volumes) / len(after_volumes) if after_volumes else 0
        after_std = (sum((v - after_mean) ** 2 for v in after_volumes) / len(
            after_volumes)) ** 0.5 if after_volumes else 0

        improvement = ((before_std - after_std) / before_std * 100) if before_std > 0 else 0

        return {
            'status': 'success',
            'file': filename,
            'output': output_path,
            'before_mean': before_mean,
            'after_mean': after_mean,
            'before_std': before_std,
            'after_std': after_std,
            'improvement': improvement
        }

    def run(self):
        """主处理逻辑"""
        self.progress_signal.emit("🔍 扫描视频文件...")

        # 获取所有视频文件
        video_files = self.get_video_files(self.file_paths)

        if not video_files:
            self.finished_signal.emit(False, "未找到任何视频文件")
            return

        self.progress_signal.emit(f"📁 找到 {len(video_files)} 个视频文件")

        # 创建输出文件夹
        if len(self.file_paths) == 1 and os.path.isfile(self.file_paths[0]):
            # 单个文件：在文件所在目录创建输出文件夹
            output_folder = os.path.join(os.path.dirname(self.file_paths[0]), '音量统一后')
        else:
            # 多个文件或文件夹：使用第一个路径的父目录
            first_path = self.file_paths[0]
            if os.path.isfile(first_path):
                output_folder = os.path.join(os.path.dirname(first_path), '音量统一后')
            else:
                output_folder = os.path.join(first_path, '音量统一后')

        os.makedirs(output_folder, exist_ok=True)

        self.progress_signal.emit(f"📁 输出文件夹: {output_folder}")

        results = []
        total = len(video_files)

        for i, video_path in enumerate(video_files):
            if self.is_cancelled:
                self.finished_signal.emit(False, "处理已取消")
                return

            self.progress_signal.emit(f"📹 处理 [{i + 1}/{total}]: {os.path.basename(video_path)}")
            self.progress_bar_signal.emit(i, total)

            result = self.process_single_video(video_path, output_folder)
            results.append(result)

            if result['status'] == 'success':
                self.progress_signal.emit(f"  ✅ {result['file']} - 改善 {result['improvement']:.1f}%")
            elif result['status'] == 'skipped':
                self.progress_signal.emit(f"  ⏭️ {result['file']} - 已存在，跳过")
            else:
                self.progress_signal.emit(f"  ❌ {result['file']} - {result.get('error', '失败')}")

        # 总结
        success_count = sum(1 for r in results if r['status'] == 'success')
        skipped_count = sum(1 for r in results if r['status'] == 'skipped')
        failed_count = sum(1 for r in results if r['status'] == 'failed')

        summary = f"""
处理完成！
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ 成功: {success_count} 个
⏭️  跳过: {skipped_count} 个
❌ 失败: {failed_count} 个
📁 输出位置: {output_folder}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        """

        self.finished_signal.emit(True, summary)


class DropArea(QLabel):
    """拖拽区域"""
    files_dropped = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("""
            QLabel {
                border: 3px dashed #aaa;
                border-radius: 10px;
                padding: 40px;
                background-color: #f8f9fa;
                font-size: 16px;
                color: #666;
                min-height: 150px;
            }
            QLabel:hover {
                border-color: #007bff;
                background-color: #e8f0fe;
            }
        """)
        self.setText("📁 拖拽视频文件或文件夹到这里\n\n或点击按钮选择文件")
        self.setWordWrap(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setStyleSheet("""
                QLabel {
                    border: 3px solid #007bff;
                    border-radius: 10px;
                    padding: 40px;
                    background-color: #e8f0fe;
                    font-size: 16px;
                    color: #007bff;
                    min-height: 150px;
                }
            """)
            self.setText("📥 释放鼠标添加文件")

    def dragLeaveEvent(self, event):
        self.setStyleSheet("""
            QLabel {
                border: 3px dashed #aaa;
                border-radius: 10px;
                padding: 40px;
                background-color: #f8f9fa;
                font-size: 16px;
                color: #666;
                min-height: 150px;
            }
        """)
        self.setText("📁 拖拽视频文件或文件夹到这里\n\n或点击按钮选择文件")

    def dropEvent(self, event):
        self.setStyleSheet("""
            QLabel {
                border: 3px dashed #aaa;
                border-radius: 10px;
                padding: 40px;
                background-color: #f8f9fa;
                font-size: 16px;
                color: #666;
                min-height: 150px;
            }
        """)
        self.setText("📁 拖拽视频文件或文件夹到这里\n\n或点击按钮选择文件")

        urls = event.mimeData().urls()
        paths = [url.toLocalFile() for url in urls if url.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.file_paths = []
        self.worker = None
        self.init_ui()
        self.check_ffmpeg()

    def init_ui(self):
        self.setWindowTitle("🎵 视频音量统一工具")
        self.setMinimumSize(700, 600)

        # 中央部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)

        # 标题
        title_label = QLabel("🎵 视频音量统一工具")
        title_label.setStyleSheet("font-size: 24px; font-weight: bold; color: #333;")
        layout.addWidget(title_label)

        subtitle_label = QLabel("自动统一视频音量到 -14dB（削高填低）")
        subtitle_label.setStyleSheet("font-size: 14px; color: #666; margin-bottom: 10px;")
        layout.addWidget(subtitle_label)

        # 拖拽区域
        self.drop_area = DropArea()
        self.drop_area.files_dropped.connect(self.add_files)
        layout.addWidget(self.drop_area)

        # 按钮行
        btn_layout = QHBoxLayout()

        select_btn = QPushButton("📂 选择文件")
        select_btn.clicked.connect(self.select_files)
        select_btn.setStyleSheet("padding: 8px 20px; font-size: 14px;")
        btn_layout.addWidget(select_btn)

        select_folder_btn = QPushButton("📁 选择文件夹")
        select_folder_btn.clicked.connect(self.select_folder)
        select_folder_btn.setStyleSheet("padding: 8px 20px; font-size: 14px;")
        btn_layout.addWidget(select_folder_btn)

        clear_btn = QPushButton("🗑️ 清空列表")
        clear_btn.clicked.connect(self.clear_files)
        clear_btn.setStyleSheet("padding: 8px 20px; font-size: 14px;")
        btn_layout.addWidget(clear_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # 文件列表
        self.file_list = QListWidget()
        self.file_list.setStyleSheet("""
            QListWidget {
                border: 1px solid #ddd;
                border-radius: 5px;
                padding: 5px;
                min-height: 100px;
                max-height: 150px;
            }
        """)
        layout.addWidget(QLabel("📋 文件列表:"))
        layout.addWidget(self.file_list)

        # 设置行
        settings_layout = QHBoxLayout()
        settings_layout.addWidget(QLabel("🎚️ 目标音量:"))

        self.target_db_spin = QSpinBox()
        self.target_db_spin.setRange(-30, 0)
        self.target_db_spin.setValue(-14)
        self.target_db_spin.setSuffix(" dB")
        settings_layout.addWidget(self.target_db_spin)

        settings_layout.addSpacing(20)
        settings_layout.addWidget(QLabel("🔧 处理模式:"))

        self.method_combo = QComboBox()
        self.method_combo.addItems(["双压缩器（推荐）", "两段式处理"])
        self.method_combo.setCurrentIndex(0)
        settings_layout.addWidget(self.method_combo)

        settings_layout.addStretch()
        layout.addLayout(settings_layout)

        # 日志区域
        layout.addWidget(QLabel("📝 处理日志:"))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit {
                border: 1px solid #ddd;
                border-radius: 5px;
                padding: 10px;
                font-family: 'Courier New', monospace;
                font-size: 12px;
                min-height: 200px;
                max-height: 250px;
                background-color: #fafafa;
            }
        """)
        layout.addWidget(self.log_text)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #ddd;
                border-radius: 5px;
                text-align: center;
                height: 25px;
            }
            QProgressBar::chunk {
                background-color: #007bff;
                border-radius: 5px;
            }
        """)
        layout.addWidget(self.progress_bar)

        # 操作按钮
        action_layout = QHBoxLayout()

        self.start_btn = QPushButton("🚀 开始处理")
        self.start_btn.clicked.connect(self.start_processing)
        self.start_btn.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                padding: 10px 30px;
                font-size: 16px;
                font-weight: bold;
                border: none;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #218838;
            }
            QPushButton:disabled {
                background-color: #6c757d;
            }
        """)
        action_layout.addWidget(self.start_btn)

        self.cancel_btn = QPushButton("⏹️ 取消")
        self.cancel_btn.clicked.connect(self.cancel_processing)
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #dc3545;
                color: white;
                padding: 10px 30px;
                font-size: 16px;
                font-weight: bold;
                border: none;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #c82333;
            }
            QPushButton:disabled {
                background-color: #6c757d;
            }
        """)
        self.cancel_btn.setEnabled(False)
        action_layout.addWidget(self.cancel_btn)

        action_layout.addStretch()
        layout.addLayout(action_layout)

    def check_ffmpeg(self):
        """检查ffmpeg是否安装"""
        try:
            subprocess.run(['ffmpeg', '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            self.log_text.append("✅ ffmpeg 已安装")
        except:
            self.log_text.append("❌ 错误: 未找到ffmpeg")
            self.log_text.append("请安装ffmpeg:")
            self.log_text.append("  macOS: brew install ffmpeg")
            self.log_text.append("  Windows: 下载 ffmpeg.org 并添加到PATH")

    def add_files(self, paths):
        """添加文件到列表"""
        for path in paths:
            if path not in self.file_paths:
                # 检查是文件还是文件夹
                if os.path.isfile(path):
                    self.file_paths.append(path)
                    self.file_list.addItem(f"📄 {os.path.basename(path)}")
                elif os.path.isdir(path):
                    self.file_paths.append(path)
                    self.file_list.addItem(f"📁 {os.path.basename(path)}")

        self.log_text.append(f"📥 添加了 {len(paths)} 个项目")

    def select_files(self):
        """选择文件"""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择视频文件",
            "",
            "视频文件 (*.mp4 *.mov *.avi *.mkv *.flv *.wmv *.m4v *.mpg);;所有文件 (*.*)"
        )
        if files:
            self.add_files(files)

    def select_folder(self):
        """选择文件夹"""
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder:
            self.add_files([folder])

    def clear_files(self):
        """清空文件列表"""
        self.file_paths.clear()
        self.file_list.clear()
        self.log_text.append("🗑️ 已清空列表")

    def start_processing(self):
        """开始处理"""
        if not self.file_paths:
            QMessageBox.warning(self, "提示", "请先添加视频文件或文件夹")
            return

        # 禁用按钮
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)

        # 清空日志（保留ffmpeg检查信息）
        lines = self.log_text.toPlainText().split('\n')
        keep_lines = [l for l in lines if 'ffmpeg' in l]
        self.log_text.clear()
        for line in keep_lines:
            self.log_text.append(line)
        self.log_text.append("=" * 50)
        self.log_text.append("🚀 开始处理...")

        # 获取参数
        target_db = self.target_db_spin.value()
        method = 'dual' if self.method_combo.currentIndex() == 0 else 'aggressive'

        # 创建并启动工作线程
        self.worker = WorkerThread(self.file_paths.copy(), target_db, method)
        self.worker.progress_signal.connect(self.update_log)
        self.worker.progress_bar_signal.connect(self.update_progress)
        self.worker.finished_signal.connect(self.on_finished)
        self.worker.start()

    def cancel_processing(self):
        """取消处理"""
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.log_text.append("⏹️ 正在取消...")

    def update_log(self, message):
        """更新日志"""
        self.log_text.append(message)
        # 自动滚动到底部
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def update_progress(self, current, total):
        """更新进度条"""
        if total > 0:
            progress = int((current + 1) / total * 100)
            self.progress_bar.setValue(progress)

    def on_finished(self, success, message):
        """处理完成"""
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setValue(100)

        self.log_text.append("=" * 50)
        self.log_text.append(message)

        if success:
            self.log_text.append("✨ 处理完成！")
            QMessageBox.information(self, "完成", message)
        else:
            self.log_text.append("⚠️ " + message)
            QMessageBox.warning(self, "提示", message)

        self.worker = None


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # 设置应用图标
    app.setWindowIcon(QIcon())

    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()