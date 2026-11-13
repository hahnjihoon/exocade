import json
import os
import sqlite3
import uuid
import zipfile
from datetime import datetime
from contextlib import closing
from pathlib import Path

from PySide6 import QtCore, QtWidgets


class CaseManager(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "EXOCADE" / "cases"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.database = self.folder.parent / "cases.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, name TEXT, patient TEXT, client TEXT, notes TEXT, path TEXT, updated TEXT)")
        layout = QtWidgets.QVBoxLayout(self)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("케이스·환자·의뢰처 검색")
        self.search.textChanged.connect(self.refresh)
        layout.addWidget(self.search)
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["케이스", "환자", "의뢰처", "수정일"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.select_case)
        layout.addWidget(self.table)
        form = QtWidgets.QFormLayout()
        self.patient = QtWidgets.QLineEdit()
        self.client = QtWidgets.QLineEdit()
        self.notes = QtWidgets.QPlainTextEdit()
        self.notes.setMaximumHeight(100)
        form.addRow("환자", self.patient)
        form.addRow("의뢰처", self.client)
        form.addRow("작업 메모", self.notes)
        layout.addLayout(form)
        self.selected_id = None
        for title, operation in [("현재 작업을 새 케이스로 등록", "create"), ("선택 케이스 정보 수정", "update"),
                                 ("선택 케이스 열기", "open"), ("선택 케이스 ZIP 내보내기", "export"),
                                 ("케이스 ZIP 가져오기", "import")]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, value=operation: self.run(value))
            layout.addWidget(button)
        self.info = QtWidgets.QLabel("케이스는 이 PC의 사용자 데이터 폴더에 저장됩니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

    def refresh(self):
        query = "%" + self.search.text().strip() + "%"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            rows = connection.execute("SELECT id, name, patient, client, updated FROM cases WHERE name LIKE ? OR patient LIKE ? OR client LIKE ? ORDER BY updated DESC", (query, query, query)).fetchall()
        self.table.setRowCount(0)
        self.selected_id = None
        for row, record in enumerate(rows):
            self.table.insertRow(row)
            for column, value in enumerate(record[1:]):
                item = QtWidgets.QTableWidgetItem(value or "")
                item.setData(QtCore.Qt.ItemDataRole.UserRole, record[0])
                self.table.setItem(row, column, item)

    def select_case(self):
        row = self.table.currentRow()
        if row < 0 or self.table.item(row, 0) is None:
            return
        self.selected_id = self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            record = connection.execute("SELECT patient, client, notes FROM cases WHERE id = ?", (self.selected_id,)).fetchone()
        if record:
            self.patient.setText(record[0] or "")
            self.client.setText(record[1] or "")
            self.notes.setPlainText(record[2] or "")

    def run(self, operation):
        try:
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            if operation == "create":
                identity = uuid.uuid4().hex
                path = self.folder / (identity + ".exo")
                previous = self.window.project_path
                self.window.project_path = path
                if not self.window.save_project():
                    self.window.project_path = previous
                    return
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?)", (identity, self.window.case_name.text(), self.patient.text(), self.client.text(), self.notes.toPlainText(), str(path), now))
                self.refresh()
                self.info.setText("현재 작업을 케이스 목록에 등록했습니다.")
                return
            if operation == "import":
                source, _ = QtWidgets.QFileDialog.getOpenFileName(self, "케이스 ZIP 가져오기", "", "케이스 묶음 (*.zip)")
                if not source:
                    return
                with zipfile.ZipFile(source) as archive:
                    if sum(item.file_size for item in archive.infolist()) > 2_000_000_000:
                        raise ValueError("케이스 묶음이 2 GB를 초과합니다.")
                    metadata = json.loads(archive.read("case.json").decode("utf-8"))
                    content = archive.read("project.exo")
                identity = uuid.uuid4().hex
                path = self.folder / (identity + ".exo")
                import io
                with zipfile.ZipFile(io.BytesIO(content)) as project:
                    manifest = json.loads(project.read("manifest.json").decode("utf-8"))
                    if manifest.get("format") != "EXOCADE" or manifest.get("version") != 1:
                        raise ValueError("지원하지 않는 EXOCADE 프로젝트입니다.")
                path.write_bytes(content)
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?)", (identity, str(metadata.get("name", path.stem)), str(metadata.get("patient", "")), str(metadata.get("client", "")), str(metadata.get("notes", "")), str(path), now))
                self.refresh()
                self.info.setText("케이스 묶음을 가져왔습니다.")
                return
            if not self.selected_id:
                raise ValueError("목록에서 케이스를 선택하세요.")
            with closing(sqlite3.connect(self.database)) as connection, connection:
                record = connection.execute("SELECT name, patient, client, notes, path FROM cases WHERE id = ?", (self.selected_id,)).fetchone()
            if record is None:
                raise ValueError("케이스를 찾을 수 없습니다.")
            if operation == "update":
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("UPDATE cases SET patient = ?, client = ?, notes = ?, updated = ? WHERE id = ?", (self.patient.text(), self.client.text(), self.notes.toPlainText(), now, self.selected_id))
                self.refresh()
            elif operation == "open":
                self.window.open_project(path_override=record[4])
            elif operation == "export":
                destination, _ = QtWidgets.QFileDialog.getSaveFileName(self, "케이스 ZIP 내보내기", "case.zip", "케이스 묶음 (*.zip)")
                if not destination:
                    return
                if Path(destination).resolve() == Path(record[4]).resolve():
                    raise ValueError("프로젝트 원본과 다른 ZIP 경로를 지정하세요.")
                if self.window.project_path and Path(self.window.project_path).resolve() == Path(record[4]).resolve():
                    if not self.window.save_project():
                        return
                metadata = dict(zip(["name", "patient", "client", "notes"], record[:4]))
                with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.write(record[4], "project.exo")
                    archive.writestr("case.json", json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
                self.info.setText("케이스 정보와 프로젝트를 ZIP으로 내보냈습니다.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "케이스 관리", str(error))
