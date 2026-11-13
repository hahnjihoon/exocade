import json
import os
from pathlib import Path

from PySide6 import QtWidgets


class MaterialLibrary(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "EXOCADE" / "materials.json"
        self.records = []
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.records = [record for record in data if isinstance(record, dict) and isinstance(record.get("name"), str)]
            except (ValueError, OSError):
                pass
        layout = QtWidgets.QVBoxLayout(self)
        self.list = QtWidgets.QListWidget()
        self.list.currentRowChanged.connect(self.select)
        layout.addWidget(self.list)
        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit()
        self.kind = QtWidgets.QComboBox()
        self.kind.addItems(["지르코니아", "리튬디실리케이트", "레진", "금속"])
        self.wall = QtWidgets.QDoubleSpinBox()
        self.wall.setRange(.3, 3)
        self.wall.setValue(.8)
        self.gap = QtWidgets.QDoubleSpinBox()
        self.gap.setRange(0, .3)
        self.gap.setValue(.06)
        self.notes = QtWidgets.QPlainTextEdit()
        self.notes.setMaximumHeight(100)
        for title, widget in [("재료 이름", self.name), ("계열", self.kind), ("설계 벽두께 (mm)", self.wall),
                              ("시멘트 공간 (mm)", self.gap), ("사용자 메모", self.notes)]:
            form.addRow(title, widget)
        layout.addLayout(form)
        for title, operation in [("새 재료 등록", "create"), ("선택 재료 수정", "update"), ("선택 재료 삭제", "delete"),
                                 ("현재 보철 설계에 적용", "apply"), ("라이브러리 JSON 내보내기", "export"), ("라이브러리 JSON 가져오기", "import")]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, value=operation: self.run(value))
            layout.addWidget(button)
        self.info = QtWidgets.QLabel("사용자가 입력한 설계 설정을 저장합니다. 제조사 보증값이나 자동 임상 처방은 포함하지 않습니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

    def refresh(self):
        previous = self.list.currentRow()
        self.list.clear()
        self.list.addItems([record["name"] for record in self.records])
        if self.records:
            self.list.setCurrentRow(min(max(0, previous), len(self.records) - 1))

    def select(self, index):
        if index < 0 or index >= len(self.records):
            return
        record = self.records[index]
        self.name.setText(record["name"])
        self.kind.setCurrentIndex(int(record.get("kind", 0)))
        self.wall.setValue(float(record.get("wall", .8)))
        self.gap.setValue(float(record.get("gap", .06)))
        self.notes.setPlainText(str(record.get("notes", "")))

    def run(self, operation):
        try:
            index = self.list.currentRow()
            record = {"name": self.name.text().strip(), "kind": self.kind.currentIndex(), "wall": self.wall.value(), "gap": self.gap.value(), "notes": self.notes.toPlainText()}
            if operation in {"create", "update", "apply"} and not record["name"]:
                raise ValueError("재료 이름을 입력하세요.")
            if operation == "create":
                self.records.append(record)
            elif operation == "update":
                if index < 0:
                    raise ValueError("수정할 재료를 선택하세요.")
                self.records[index] = record
            elif operation == "delete":
                if index < 0:
                    raise ValueError("삭제할 재료를 선택하세요.")
                self.records.pop(index)
            elif operation == "apply":
                self.window.material.setCurrentIndex(record["kind"])
                self.window.design_controls["wall"].setValue(record["wall"])
                self.window.design_controls["gap"].setValue(record["gap"])
                self.window.extension_state["material"] = record.copy()
                self.window.dirty = True
                self.info.setText(record["name"] + "의 설계 벽두께와 시멘트 공간을 적용했습니다.")
                return
            elif operation == "export":
                path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "재료 라이브러리 저장", "materials.json", "JSON (*.json)")
                if path:
                    Path(path).write_text(json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8")
                return
            elif operation == "import":
                path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "재료 라이브러리 가져오기", "", "JSON (*.json)")
                if not path:
                    return
                records = json.loads(Path(path).read_text(encoding="utf-8-sig"))
                if not isinstance(records, list) or len(records) > 1000:
                    raise ValueError("1000개 이하의 재료 목록이 필요합니다.")
                staged = []
                for value in records:
                    if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"].strip():
                        raise ValueError("재료 이름이 올바르지 않습니다.")
                    wall, gap, kind = float(value["wall"]), float(value["gap"]), int(value["kind"])
                    if not .3 <= wall <= 3 or not 0 <= gap <= .3 or kind not in range(4):
                        raise ValueError("재료 설정 범위를 벗어났습니다.")
                    staged.append({"name": value["name"], "wall": wall, "gap": gap, "kind": kind, "notes": str(value.get("notes", ""))})
                self.records.extend(staged)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
            self.refresh()
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "재료 라이브러리", str(error))
