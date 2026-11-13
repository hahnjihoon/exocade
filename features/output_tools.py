import csv

from PySide6 import QtCore, QtWidgets


class OutputTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        self.list = QtWidgets.QListWidget()
        layout.addWidget(self.list)
        for title, callback in [("전체 출력 포함 여부 적용", self.apply), ("선택 오브젝트 검사", self.inspect),
                                ("선택 오브젝트 STL / 3MF 저장", self.export), ("전체 출력 검사 결과 CSV 저장", self.report)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.info = QtWidgets.QLabel("체크한 제작물만 전체 출력에 포함합니다. 대체된 원본도 선택 출력으로 따로 저장할 수 있습니다. 출력 배치가 있다면 포함 여부 변경 후 다시 배치하세요.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

    def refresh(self):
        self.list.clear()
        for key, obj in self.window.objects.items():
            if obj["role"] in {"restoration", "connector", "model", "appliance", "die", "pontic", "implant", "bar", "denture", "gingiva", "part", "segmentation"}:
                item = QtWidgets.QListWidgetItem(obj["name"])
                item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(QtCore.Qt.CheckState.Checked if obj.get("export_enabled", True) else QtCore.Qt.CheckState.Unchecked)
                self.list.addItem(item)

    def apply(self):
        self.window.push_undo()
        for index in range(self.list.count()):
            item = self.list.item(index)
            obj = self.window.objects.get(item.data(QtCore.Qt.ItemDataRole.UserRole))
            if obj:
                obj["export_enabled"] = item.checkState() == QtCore.Qt.CheckState.Checked
        self.window.dirty = True
        self.info.setText("전체 출력 포함 여부를 적용했습니다. 기존 출력 배치는 다시 생성하세요.")

    def inspect(self):
        try:
            item = self.list.currentItem()
            obj = self.window.objects.get(item.data(QtCore.Qt.ItemDataRole.UserRole)) if item else None
            if obj is None:
                raise ValueError("오브젝트를 선택하세요.")
            mesh, report, solid = self.window.analyze_print_mesh(obj["mesh"], obj["name"])
            self.info.setText(f"{obj['name']} · 닫힌 메시 · {report['triangles']:,}면 · 체적 {report['volume_mm3']:.3f} mm³")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "출력 검사", str(error))

    def export(self):
        item = self.list.currentItem()
        if item and item.data(QtCore.Qt.ItemDataRole.UserRole) in self.window.objects:
            self.window.active_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
            self.window.export_mesh(selected=True)

    def report(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "출력 검사 결과", "mesh-report.csv", "CSV (*.csv)")
        if not path:
            return
        rows = []
        try:
            for index in range(self.list.count()):
                item = self.list.item(index)
                if item.checkState() != QtCore.Qt.CheckState.Checked:
                    continue
                obj = self.window.objects.get(item.data(QtCore.Qt.ItemDataRole.UserRole))
                if obj is None:
                    continue
                try:
                    mesh, report, solid = self.window.analyze_print_mesh(obj["mesh"], obj["name"])
                    rows.append([obj["name"], obj["role"], report["triangles"], report["volume_mm3"], "정상", ""])
                except Exception as error:
                    rows.append([obj["name"], obj["role"], "", "", "오류", str(error)])
            with open(path, "w", encoding="utf-8-sig", newline="") as output:
                writer = csv.writer(output)
                writer.writerow(["오브젝트", "종류", "삼각형 수", "체적 mm³", "검사", "상세"])
                writer.writerows(rows)
            self.info.setText(f"{len(rows)}개 오브젝트의 검사 결과를 저장했습니다.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "검사 결과 저장", str(error))
