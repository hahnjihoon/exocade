import csv
import copy

import numpy as np
import vtk
from PySide6 import QtCore, QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy


class ArticulatorTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.frames = []
        self.previewed_key = None
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.advance)
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.moving = QtWidgets.QComboBox()
        self.target = QtWidgets.QComboBox()
        self.controls = {}
        for key, title, lo, hi, value in [("px", "회전 중심 X", -1000, 1000, 0), ("py", "회전 중심 Y", -1000, 1000, 0),
                                        ("pz", "회전 중심 Z", -1000, 1000, 0), ("opening", "최대 개구 회전 (도)", -40, 40, 12),
                                        ("protrusion", "최대 전방 이동 (mm)", -15, 15, 3), ("lateral", "최대 측방 이동 (mm)", -15, 15, 2),
                                        ("gap", "동적 여유 (mm)", 0, 1, .05), ("wall", "보호 두께 (mm)", .2, 3, .6)]:
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(lo, hi)
            widget.setValue(value)
            self.controls[key] = widget
            form.addRow(title, widget)
        form.insertRow(0, "움직일 대합치·악궁", self.moving)
        form.insertRow(1, "검사·조정할 보철 / 스플린트", self.target)
        layout.addLayout(form)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.valueChanged.connect(self.preview)
        layout.addWidget(self.slider)
        for title, callback in [("설정값으로 턱 운동 생성", self.generate), ("턱 운동 CSV 가져오기", self.load_csv),
                                ("운동 재생 / 정지", self.play), ("현재 위치를 모델에 적용", self.apply),
                                ("동적 접촉 거리맵", lambda: self.analyze(False)), ("전체 운동 범위에 교합 조정", lambda: self.analyze(True))]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.info = QtWidgets.QLabel("CSV 열: tx, ty, tz (mm), rx, ry, rz (도), 선택 열 time (초). 회전 중심과 축은 스캔 좌표에 맞게 지정합니다. 생성 운동은 단순 강체 운동이며 실제 계측값을 대신하지 않습니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def refresh(self):
        self.deactivate()
        stored = self.window.extension_state.get("jaw_motion", {})
        if not isinstance(stored, dict):
            stored = {}
        self.frames = stored.get("frames", [])
        if not isinstance(self.frames, list) or len(self.frames) > 6000:
            self.frames = []
        try:
            for frame in self.frames:
                values = [float(frame[key]) for key in ["tx", "ty", "tz", "rx", "ry", "rz", "time"]]
                if not np.isfinite(values).all() or any(abs(value) > 180 for value in values[:6]):
                    raise ValueError("저장된 턱 운동 범위를 확인하세요.")
        except (TypeError, KeyError, ValueError):
            self.frames = []
        self.slider.setRange(0, max(0, len(self.frames) - 1))
        pivot = stored.get("pivot", {})
        if isinstance(pivot, dict):
            for key, value in pivot.items():
                if key in {"px", "py", "pz"} and isinstance(value, (int, float)) and np.isfinite(value):
                    self.controls[key].setValue(float(value))
        for combo in [self.moving, self.target]:
            previous = combo.currentData()
            combo.clear()
            for key, obj in self.window.objects.items():
                if obj["role"] not in {"print", "library"} and obj.get("export_enabled", True):
                    combo.addItem(obj["name"], key)
            combo.setCurrentIndex(max(0, combo.findData(previous or self.window.active_id)))
        if self.target.currentData() == self.moving.currentData() and self.moving.count() > 1:
            antagonist = next((i for i in range(self.moving.count()) if self.window.objects[self.moving.itemData(i)]["role"] == "antagonist"), -1)
            self.moving.setCurrentIndex(antagonist if antagonist >= 0 else (self.target.currentIndex() + 1) % self.moving.count())

    def generate(self):
        self.deactivate()
        self.frames = []
        for index, phase in enumerate(np.linspace(0, 2 * np.pi, 61)):
            self.frames.append({"tx": float(self.controls["lateral"].value() * np.sin(phase)),
                                "ty": float(self.controls["protrusion"].value() * (1 - np.cos(phase)) / 2), "tz": 0.,
                                "rx": float(self.controls["opening"].value() * (1 - np.cos(phase)) / 2), "ry": 0., "rz": 0., "time": index * .08})
        self.slider.setRange(0, len(self.frames) - 1)
        self.window.extension_state["jaw_motion"] = {"frames": self.frames, "pivot": {key: self.controls[key].value() for key in ["px", "py", "pz"]}}
        self.window.dirty = True
        self.slider.setValue(0)
        self.preview()

    def load_csv(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "턱 운동 CSV", "", "턱 운동 (*.csv)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source)
                required = {"tx", "ty", "tz", "rx", "ry", "rz"}
                if not required.issubset(reader.fieldnames or []):
                    raise ValueError("CSV에는 tx, ty, tz, rx, ry, rz 열이 필요합니다.")
                frames = []
                for index, row in enumerate(reader):
                    if index >= 6000:
                        raise ValueError("CSV는 6000행 이하로 입력하세요.")
                    frame = {key: float(row[key]) for key in required}
                    frame["time"] = float(row.get("time") or index * .1)
                    if not np.isfinite(list(frame.values())).all() or any(abs(frame[key]) > 180 for key in required):
                        raise ValueError("유효하지 않거나 범위를 벗어난 운동값이 있습니다.")
                    if frames and frame["time"] <= frames[-1]["time"]:
                        raise ValueError("time 값은 시간 순서대로 증가해야 합니다.")
                    frames.append(frame)
            if not frames:
                raise ValueError("운동 데이터가 비어 있습니다.")
            self.deactivate()
            self.frames = frames
            self.window.extension_state["jaw_motion"] = {"frames": frames, "pivot": {key: self.controls[key].value() for key in ["px", "py", "pz"]}}
            self.window.dirty = True
            self.slider.setRange(0, len(frames) - 1)
            self.slider.setValue(0)
            self.preview()
            self.info.setText(f"운동 CSV {len(frames)}개 자세를 읽었습니다. 회전 중심은 현재 설정값을 사용합니다.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "턱 운동 CSV", str(error))

    def preview(self):
        if not self.frames:
            return
        key = self.moving.currentData()
        obj = self.window.objects.get(key)
        if obj is None:
            return
        if self.previewed_key and self.previewed_key != key:
            previous = self.window.objects.get(self.previewed_key)
            if previous:
                previous["actor"].SetUserMatrix(None)
        frame = self.frames[self.slider.value()]
        pivot = np.array([self.controls[name].value() for name in ["px", "py", "pz"]])
        transform = vtk.vtkTransform()
        transform.PostMultiply()
        transform.Translate(-pivot)
        transform.RotateX(frame["rx"])
        transform.RotateY(frame["ry"])
        transform.RotateZ(frame["rz"])
        transform.Translate(pivot + [frame["tx"], frame["ty"], frame["tz"]])
        obj["actor"].SetUserMatrix(transform.GetMatrix())
        self.previewed_key = key
        self.window.renderer.ResetCameraClippingRange()
        self.window.vtk_widget.GetRenderWindow().Render()

    def play(self):
        if self.timer.isActive():
            self.timer.stop()
        else:
            if not self.frames:
                self.generate()
            self.timer.start(80)

    def advance(self):
        if self.slider.value() >= self.slider.maximum():
            self.timer.stop()
            return
        current = self.slider.value()
        if current + 1 < len(self.frames):
            interval = (self.frames[current + 1]["time"] - self.frames[current]["time"]) * 1000
            self.timer.setInterval(int(np.clip(interval, 15, 2000)))
        self.slider.setValue(current + 1)

    def apply(self):
        try:
            if not self.frames:
                raise ValueError("턱 운동을 생성하거나 CSV를 가져오세요.")
            self.preview()
            key = self.moving.currentData()
            obj = self.window.objects.get(key)
            if obj is None:
                raise ValueError("움직일 모델을 선택하세요.")
            if obj["role"] in {"restoration", "connector"}:
                raise ValueError("위치 적용에는 스캔·대합치·참고 모델을 선택하세요.")
            transform = vtk.vtkTransform()
            transform.SetMatrix(obj["actor"].GetUserMatrix())
            process = vtk.vtkTransformPolyDataFilter()
            process.SetInputData(obj["mesh"])
            process.SetTransform(transform)
            process.Update()
            self.deactivate()
            self.window.push_undo()
            obj["mesh"].DeepCopy(process.GetOutput())
            for state in self.window.teeth.values():
                if state.get("scan_id") == key:
                    state["margin"] = [list(transform.TransformPoint(point)) for point in state["margin"]]
                    if "seed" in state:
                        state["seed"] = list(transform.TransformPoint(state["seed"]))
                    axis = np.asarray(transform.TransformVector(state["axis"]))
                    state["axis"] = (axis / np.linalg.norm(axis)).tolist()
            self.window.update_margin()
            self.window.refresh_scene()
            self.window.dirty = True
            self.frames = []
            self.window.extension_state.pop("jaw_motion", None)
            self.slider.setRange(0, 0)
            self.info.setText("현재 자세를 모델 좌표에 적용했습니다.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "턱 운동 위치 적용", str(error))

    def analyze(self, adapt):
        self.deactivate()
        try:
            moving = self.window.objects.get(self.moving.currentData())
            target = self.window.objects.get(self.target.currentData())
            if moving is None or target is None or moving is target:
                raise ValueError("서로 다른 운동 모델과 보철·스플린트를 선택하세요.")
            if not self.frames:
                raise ValueError("턱 운동을 먼저 생성하거나 가져오세요.")
            if len(self.frames) * target["mesh"].GetNumberOfPoints() > 4_000_000:
                raise ValueError("계산량이 큽니다. 운동 CSV 행 수 또는 보철 정점 수를 줄이세요.")
            mesh = vtk.vtkPolyData()
            mesh.DeepCopy(target["mesh"])
            points = vtk_to_numpy(mesh.GetPoints().GetData()).astype(float).copy()
            minimum = np.full(len(points), np.inf)
            gradients = np.zeros_like(points)
            pivot = np.array([self.controls[name].value() for name in ["px", "py", "pz"]])
            for frame in self.frames:
                transform = vtk.vtkTransform()
                transform.PostMultiply()
                transform.Translate(-pivot)
                transform.RotateX(frame["rx"])
                transform.RotateY(frame["ry"])
                transform.RotateZ(frame["rz"])
                transform.Translate(pivot + [frame["tx"], frame["ty"], frame["tz"]])
                process = vtk.vtkTransformPolyDataFilter()
                process.SetInputData(moving["mesh"])
                process.SetTransform(transform)
                process.Update()
                distance = vtk.vtkImplicitPolyDataDistance()
                distance.SetInput(process.GetOutput())
                values = np.array([distance.EvaluateFunction(point) for point in points])
                changed = values < minimum
                for index in np.flatnonzero(changed):
                    gradient = [0., 0., 0.]
                    distance.EvaluateGradient(points[index], gradient)
                    gradients[index] = gradient
                minimum = np.minimum(minimum, values)
            if not adapt:
                self.window.push_undo()
                array = numpy_to_vtk(minimum, deep=True)
                array.SetName("DynamicContact_mm")
                target["mesh"].GetPointData().SetScalars(array)
                mapper = target["actor"].GetMapper()
                lut = vtk.vtkLookupTable()
                lut.SetHueRange(0, .66)
                lut.SetRange(-.3, 2)
                lut.Build()
                mapper.SetLookupTable(lut)
                mapper.SetScalarRange(-.3, 2)
                mapper.ScalarVisibilityOn()
                self.window.dirty = True
            else:
                editable = np.asarray(target.get("editable", [True] * len(points)), dtype=bool)
                inner_distance = None
                if target.get("inner_count"):
                    faces = vtk_to_numpy(mesh.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
                    faces = faces[np.all(faces < target["inner_count"], axis=1)]
                    inner = vtk.vtkPolyData()
                    inner.SetPoints(mesh.GetPoints())
                    cells = vtk.vtkCellArray()
                    cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).astype(np.int64).ravel(), deep=True))
                    inner.SetPolys(cells)
                    inner_distance = vtk.vtkImplicitPolyDataDistance()
                    inner_distance.SetInput(inner)
                for index in np.flatnonzero(editable & (minimum < self.controls["gap"].value()) & (minimum > -2)):
                    length = np.linalg.norm(gradients[index])
                    if length < 1e-8:
                        continue
                    proposed = points[index] + gradients[index] / length * (self.controls["gap"].value() - minimum[index])
                    if inner_distance and -inner_distance.EvaluateFunction(proposed) < self.controls["wall"].value():
                        continue
                    points[index] = proposed
                mesh.GetPoints().SetData(numpy_to_vtk(points, deep=True))
                self.window.analyze_print_mesh(mesh, "동적 교합 조정")
                metadata = {name: copy.deepcopy(value) for name, value in target.items() if name not in {"mesh", "actor", "name", "role", "export_enabled", "superseded_by"}}
                self.window.push_undo()
                result = self.window.add_object(mesh, target["name"] + " · 동적 교합", target["role"], **metadata)
                target.update(export_enabled=False, superseded_by=result)
                target["actor"].SetVisibility(False)
                self.window.refresh_scene()
            self.window.vtk_widget.GetRenderWindow().Render()
            self.info.setText(f"{len(self.frames)}개 자세의 최소 거리 {minimum.min():.3f} mm를 계산했습니다. 열린 대합치 표면에서는 거리 부호를 확인하세요.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "동적 교합", str(error))

    def deactivate(self):
        self.timer.stop()
        if self.previewed_key:
            obj = self.window.objects.get(self.previewed_key)
            if obj:
                obj["actor"].SetUserMatrix(None)
            self.previewed_key = None
            self.window.vtk_widget.GetRenderWindow().Render()
