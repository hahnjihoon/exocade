import copy

import numpy as np
import vtk
from PySide6 import QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy


class ScanTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.source = QtWidgets.QComboBox()
        self.target = QtWidgets.QComboBox()
        self.axis = QtWidgets.QComboBox()
        self.axis.addItems(["X", "Y", "Z"])
        self.iterations = QtWidgets.QSpinBox()
        self.iterations.setRange(5, 300)
        self.iterations.setValue(60)
        self.radius = QtWidgets.QDoubleSpinBox()
        self.radius.setRange(1, 20)
        self.radius.setValue(5)
        self.radius.setSuffix(" mm")
        form.addRow("원본", self.source)
        form.addRow("정합 대상", self.target)
        form.addRow("미러 기준축", self.axis)
        form.addRow("정합 반복", self.iterations)
        form.addRow("자동 마진 검색 반경", self.radius)
        layout.addLayout(form)
        for title, operation in [("선택 모델 복사", "copy"), ("선택 모델 미러 복사", "mirror"),
                                 ("대상에 강체 자동 정합", "align"), ("기준점 주변 마진 자동 추적", "margin")]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, value=operation: self.run(value))
            layout.addWidget(button)
        self.info = QtWidgets.QLabel("자동 마진은 기존 마진 또는 마진 단계에서 선택한 기준점 주변을 검색합니다. 정합 전에는 두 모델의 위치를 가깝게 배치하세요.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def refresh(self):
        for combo in [self.source, self.target]:
            previous = combo.currentData()
            combo.clear()
            for key, obj in self.window.objects.items():
                if obj["role"] != "print":
                    combo.addItem(obj["name"], key)
            combo.setCurrentIndex(max(0, combo.findData(previous or self.window.active_id)))
        if self.source.currentData() == self.target.currentData() and self.target.count() > 1:
            self.target.setCurrentIndex((self.source.currentIndex() + 1) % self.target.count())

    def run(self, operation):
        try:
            key = self.source.currentData()
            source = self.window.objects.get(key)
            if source is None:
                raise ValueError("원본 모델을 선택하세요.")
            if operation in {"copy", "mirror"}:
                mesh = vtk.vtkPolyData()
                mesh.DeepCopy(source["mesh"])
                if operation == "mirror":
                    coordinates = vtk_to_numpy(mesh.GetPoints().GetData()).copy()
                    dimension = self.axis.currentIndex()
                    coordinates[:, dimension] = 2 * source["mesh"].GetCenter()[dimension] - coordinates[:, dimension]
                    points = vtk.vtkPoints()
                    points.SetData(numpy_to_vtk(coordinates, deep=True))
                    mesh.SetPoints(points)
                    reverse = vtk.vtkReverseSense()
                    reverse.SetInputData(mesh)
                    reverse.ReverseCellsOn()
                    reverse.ReverseNormalsOn()
                    reverse.Update()
                    mesh.DeepCopy(reverse.GetOutput())
                metadata = {name: copy.deepcopy(value) for name, value in source.items() if name not in {"mesh", "actor", "name", "role", "source_ids", "source_signatures", "endpoints"}}
                metadata["tooth"] = self.window.current_tooth
                metadata.pop("scan_id", None)
                metadata.pop("inner_count", None)
                metadata.pop("editable", None)
                metadata.pop("superseded_by", None)
                metadata["export_enabled"] = True
                role = "library" if source["role"] in {"restoration", "library"} else source["role"]
                self.window.push_undo()
                self.window.add_object(mesh, source["name"] + (" · 미러" if operation == "mirror" else " · 복사"), role, **metadata)
            elif operation == "align":
                target = self.window.objects.get(self.target.currentData())
                if target is None or source is target:
                    raise ValueError("서로 다른 원본과 정합 대상을 선택하세요.")
                if source["role"] in {"restoration", "connector"}:
                    raise ValueError("정합에는 스캔·참고 모델·라이브러리를 사용하세요.")
                registration = vtk.vtkIterativeClosestPointTransform()
                registration.SetSource(source["mesh"])
                registration.SetTarget(target["mesh"])
                registration.GetLandmarkTransform().SetModeToRigidBody()
                registration.SetMaximumNumberOfIterations(self.iterations.value())
                registration.SetMaximumNumberOfLandmarks(4000)
                registration.StartByMatchingCentroidsOn()
                registration.CheckMeanDistanceOn()
                registration.SetMaximumMeanDistance(.001)
                registration.Update()
                transformed = vtk.vtkTransformPolyDataFilter()
                transformed.SetInputData(source["mesh"])
                transformed.SetTransform(registration)
                transformed.Update()
                self.window.push_undo()
                source["mesh"].DeepCopy(transformed.GetOutput())
                for state in self.window.teeth.values():
                    if state.get("scan_id") == key:
                        state["margin"] = [list(registration.TransformPoint(point)) for point in state["margin"]]
                        if "seed" in state:
                            state["seed"] = list(registration.TransformPoint(state["seed"]))
                        axis = np.asarray(registration.TransformVector(state["axis"]))
                        state["axis"] = (axis / np.linalg.norm(axis)).tolist()
                self.window.update_margin()
                self.info.setText(f"강체 정합 완료 · 반복 {registration.GetNumberOfIterations()}회 · 반복 간 평균 이동 {registration.GetMeanDistance():.4f} mm")
            elif operation == "margin":
                if source["role"] != "scan":
                    raise ValueError("자동 마진에는 작업악 스캔을 선택하세요.")
                state = self.window.teeth[str(self.window.current_tooth)]
                previous = np.asarray(state.get("margin", []))
                if len(previous) < 3 and "seed" not in state:
                    raise ValueError("마진 단계에서 단면 기준점을 선택하거나 마진 점을 3개 이상 지정하세요.")
                center = previous.mean(axis=0) if len(previous) >= 3 else np.asarray(state["seed"])
                axis = np.asarray(state["axis"], dtype=float)
                axis /= np.linalg.norm(axis)
                x = np.array([1., 0, 0]) if abs(axis[0]) < .85 else np.array([0., 1, 0])
                x -= axis * np.dot(x, axis)
                x /= np.linalg.norm(x)
                basis = np.column_stack([x, np.cross(axis, x), axis])
                curvature = vtk.vtkCurvatures()
                curvature.SetInputData(source["mesh"])
                curvature.SetCurvatureTypeToMean()
                curvature.Update()
                vertices = vtk_to_numpy(curvature.GetOutput().GetPoints().GetData())
                local = (vertices - center) @ basis
                values = np.nan_to_num(np.abs(vtk_to_numpy(curvature.GetOutput().GetPointData().GetScalars())))
                radial = np.linalg.norm(local[:, :2], axis=1)
                angles = np.arctan2(local[:, 1], local[:, 0])
                radius = self.radius.value()
                selected = []
                for angle in np.linspace(-np.pi, np.pi, 96, endpoint=False):
                    angular = np.abs(np.arctan2(np.sin(angles - angle), np.cos(angles - angle)))
                    mask = (angular < np.pi / 36) & (radial > .25) & (radial < radius) & (np.abs(local[:, 2]) < radius * .6)
                    indices = np.flatnonzero(mask)
                    if not len(indices):
                        raise ValueError("마진 후보가 끊겼습니다. 기준점과 검색 반경을 조정하세요.")
                    expected = np.mean(np.linalg.norm((previous - center) @ basis[:, :2], axis=1)) if len(previous) >= 3 else radius * .65
                    score = np.minimum(values[indices], np.percentile(values[indices], 95) + 1e-6)
                    score *= np.exp(-np.square(local[indices, 2] / max(.4, radius * .2)))
                    score *= np.exp(-np.square((radial[indices] - expected) / max(.5, radius * .35)))
                    selected.append(vertices[indices[int(np.argmax(score))]])
                points = np.asarray(selected)
                points = (np.roll(points, 1, axis=0) + 2 * points + np.roll(points, -1, axis=0)) / 4
                locator = vtk.vtkStaticCellLocator()
                locator.SetDataSet(source["mesh"])
                locator.BuildLocator()
                result = []
                for point in points:
                    nearest = [0., 0., 0.]
                    locator.FindClosestPoint(point, nearest, vtk.reference(0), vtk.reference(0), vtk.reference(0.))
                    result.append(nearest)
                self.window.push_undo()
                state.update(margin=result, scan_id=key, closed=False)
                self.window.update_margin()
                self.window.set_step(1)
                self.info.setText("마진 후보 96점을 추적했습니다. 경계를 확인하고 마진 닫기를 실행하세요.")
            self.window.dirty = True
            self.window.refresh_scene()
            self.window.camera_view("fit")
            self.refresh()
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "스캔·복사 도구", str(error))
