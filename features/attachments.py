import manifold3d
import numpy as np
import vtk
from PySide6 import QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray


class AttachmentTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.target = QtWidgets.QComboBox()
        self.kind = QtWidgets.QComboBox()
        self.kind.addItems(["구형 어태치먼트", "원통형 어태치먼트", "도브테일 어태치먼트", "포스트·코어", "일반 스크루 채널"])
        self.operation = QtWidgets.QComboBox()
        self.operation.addItems(["별도 부품 생성", "선택 모델에 합치기", "선택 모델에서 빼기"])
        self.parameters = {}
        for key, title, lo, hi, initial in [("x", "위치 X", -1000, 1000, 0), ("y", "위치 Y", -1000, 1000, 0), ("z", "위치 Z", -1000, 1000, 0),
                                          ("rx", "회전 X", -180, 180, 0), ("ry", "회전 Y", -180, 180, 0), ("rz", "회전 Z", -180, 180, 0),
                                          ("diameter", "지름 / 폭", .4, 20, 2), ("height", "높이 / 길이", .5, 40, 5), ("clearance", "암형 여유", 0, 1, .1),
                                          ("core_width", "코어 폭", 1, 20, 5), ("core_height", "코어 높이", 1, 20, 4)]:
            control = QtWidgets.QDoubleSpinBox()
            control.setRange(lo, hi)
            control.setValue(initial)
            control.setDecimals(3)
            self.parameters[key] = control
            form.addRow(title, control)
        form.insertRow(0, "형상", self.kind)
        form.insertRow(1, "대상", self.target)
        form.insertRow(2, "연산", self.operation)
        layout.addLayout(form)
        position = QtWidgets.QPushButton("선택 치아 위치·삽입축 사용")
        position.clicked.connect(self.position)
        layout.addWidget(position)
        button = QtWidgets.QPushButton("형상 생성 / 연산")
        button.clicked.connect(self.run)
        layout.addWidget(button)
        self.info = QtWidgets.QLabel("일반 기하 형상으로 설계합니다. 제조사 임플란트 접속부·체결 규격은 포함하지 않습니다. 위치는 모델 좌표의 mm입니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def refresh(self):
        previous = self.target.currentData()
        self.target.clear()
        for key, obj in self.window.objects.items():
            if obj["role"] not in {"print", "library"}:
                self.target.addItem(obj["name"], key)
        self.target.setCurrentIndex(max(0, self.target.findData(previous or self.window.active_id)))

    def position(self):
        state = self.window.teeth[str(self.window.current_tooth)]
        margin = state.get("margin", [])
        obj = self.window.objects.get(self.target.currentData())
        center = np.mean(margin, axis=0) if margin else np.asarray(obj["mesh"].GetCenter()) if obj else np.zeros(3)
        for key, value in zip(["x", "y", "z"], center):
            self.parameters[key].setValue(float(value))
        axis = np.asarray(state["axis"], dtype=float)
        axis /= np.linalg.norm(axis)
        self.parameters["rx"].setValue(float(-np.degrees(np.arcsin(np.clip(axis[1], -1, 1)))))
        self.parameters["ry"].setValue(float(np.degrees(np.arctan2(axis[0], axis[2]))))

    def run(self):
        try:
            values = {key: control.value() for key, control in self.parameters.items()}
            kind, operation = self.kind.currentIndex(), self.operation.currentIndex()
            radius = values["diameter"] / 2 + (values["clearance"] if operation == 2 else 0)
            height = values["height"]
            if kind == 0:
                solid = manifold3d.Manifold.sphere(radius, 48)
            elif kind in {1, 4}:
                solid = manifold3d.Manifold.cylinder(height, radius, circular_segments=64)
            elif kind == 2:
                solid = manifold3d.CrossSection([[[-radius, -radius], [radius, -radius], [radius * .65, radius], [-radius * .65, radius]]]).extrude(height)
            else:
                post = manifold3d.Manifold.cylinder(height, radius * .65, radius, circular_segments=64).translate((0, 0, -height))
                core = manifold3d.Manifold.cylinder(values["core_height"], values["core_width"] / 2, values["core_width"] * .40, circular_segments=64).translate((0, 0, -.05))
                solid = post + core
            solid = solid.rotate((values["rx"], values["ry"], values["rz"])).translate((values["x"], values["y"], values["z"]))
            target = self.window.objects.get(self.target.currentData())
            if operation:
                if target is None:
                    raise ValueError("연산할 닫힌 모델을 선택하세요.")
                mesh, report, base = self.window.analyze_print_mesh(target["mesh"], target["name"])
                if (base ^ solid).volume() <= 1e-8:
                    raise ValueError("부품과 대상이 겹치지 않습니다. 위치와 회전을 조정하세요.")
                solid = base + solid if operation == 1 else base - solid
            if solid.status() != manifold3d.Error.NoError or solid.is_empty():
                raise ValueError("형상 연산 결과가 비어 있거나 올바르지 않습니다.")
            result = solid.to_mesh64()
            mesh = vtk.vtkPolyData()
            points = vtk.vtkPoints()
            points.SetData(numpy_to_vtk(np.asarray(result.vert_properties[:, :3]), deep=True))
            mesh.SetPoints(points)
            faces = np.asarray(result.tri_verts, dtype=np.int64)
            cells = vtk.vtkCellArray()
            cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).ravel(), deep=True))
            mesh.SetPolys(cells)
            self.window.analyze_print_mesh(mesh, self.kind.currentText())
            self.window.push_undo()
            name = self.kind.currentText() if operation == 0 else target["name"] + " · " + self.kind.currentText()
            key = self.window.add_object(mesh, name, "part", tooth=self.window.current_tooth, design_kind=self.kind.currentText(), construction=values, source_id=self.target.currentData() if operation else None)
            if operation:
                target.update(export_enabled=False, superseded_by=key)
                target["actor"].SetVisibility(False)
                self.window.refresh_scene()
            self.window.focus_selection()
            self.info.setText("형상 생성과 폐합 검사를 완료했습니다. 대상 원본은 보존됩니다.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "어태치먼트·포스트", str(error))
