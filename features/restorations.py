import copy

import manifold3d
import numpy as np
import vtk
from PySide6 import QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy


class RestorationTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.source = QtWidgets.QComboBox()
        self.kind = QtWidgets.QComboBox()
        self.kind.addItems(["오버레이", "베니어", "에그쉘 임시치아", "해부학적 컷백", "텔레스코픽 1차관", "텔레스코픽 2차관", "왁스업 형태 등록"])
        self.wall = QtWidgets.QDoubleSpinBox()
        self.wall.setRange(.2, 4)
        self.wall.setValue(.8)
        self.wall.setSuffix(" mm")
        self.gap = QtWidgets.QDoubleSpinBox()
        self.gap.setRange(0, 1)
        self.gap.setValue(.06)
        self.gap.setSuffix(" mm")
        self.reduction = QtWidgets.QDoubleSpinBox()
        self.reduction.setRange(.05, 2)
        self.reduction.setValue(.4)
        self.reduction.setSuffix(" mm")
        self.trim = QtWidgets.QDoubleSpinBox()
        self.trim.setRange(.05, .95)
        self.trim.setValue(.55)
        self.direction = QtWidgets.QComboBox()
        self.direction.addItems(["+X", "−X", "+Y", "−Y", "+Z", "−Z"])
        self.direction.setCurrentIndex(4)
        self.taper = QtWidgets.QDoubleSpinBox()
        self.taper.setRange(0, 15)
        self.taper.setValue(4)
        self.taper.setSuffix("°")
        self.height = QtWidgets.QDoubleSpinBox()
        self.height.setRange(1, 20)
        self.height.setValue(5)
        self.height.setSuffix(" mm")
        for title, widget in [("원본 치아·보철", self.source), ("설계 종류", self.kind), ("벽두께", self.wall),
                              ("내면 여유", self.gap), ("컷백 깊이", self.reduction), ("절단 위치 비율", self.trim),
                              ("유지할 표면 방향", self.direction), ("텔레스코픽 경사", self.taper), ("텔레스코픽 높이", self.height)]:
            form.addRow(title, widget)
        layout.addLayout(form)
        button = QtWidgets.QPushButton("보철 형상 생성")
        button.clicked.connect(self.run)
        layout.addWidget(button)
        self.info = QtWidgets.QLabel("오버레이·베니어·임시치아는 닫힌 개별 치아형을 사용합니다. 컷백은 기존 크라운의 내면과 마진을 보호합니다. 텔레스코픽은 선택 치아의 닫힌 마진과 삽입축을 사용합니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def refresh(self):
        previous = self.source.currentData()
        self.source.clear()
        for key, obj in self.window.objects.items():
            if obj["role"] not in {"print", "connector"}:
                self.source.addItem(obj["name"], key)
        self.source.setCurrentIndex(max(0, self.source.findData(previous or self.window.active_id)))

    def run(self):
        try:
            source = self.window.objects.get(self.source.currentData())
            if source is None:
                raise ValueError("원본 치아·보철을 선택하세요.")
            kind = self.kind.currentIndex()
            name = self.kind.currentText()
            wall, gap = self.wall.value(), self.gap.value()
            if kind == 6:
                mesh = vtk.vtkPolyData()
                mesh.DeepCopy(source["mesh"])
                self.window.push_undo()
                key = self.window.add_object(mesh, source["name"] + " · 왁스업", "library", tooth=self.window.current_tooth, visible=False, library_origin="사용자 왁스업")
                self.window.library_combo.setCurrentIndex(self.window.library_combo.findData(key))
                self.info.setText("왁스업을 치아 형태로 등록했습니다. 보철 설계에서 내면과 마진을 생성할 수 있습니다.")
                return
            if kind == 3:
                count = source.get("inner_count", 0)
                if not count:
                    raise ValueError("내면 정보가 있는 기존 크라운을 선택하세요.")
                mesh = vtk.vtkPolyData()
                mesh.DeepCopy(source["mesh"])
                points = vtk_to_numpy(mesh.GetPoints().GetData()).copy()
                triangles = vtk_to_numpy(mesh.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
                inner = vtk.vtkPolyData()
                inner.SetPoints(mesh.GetPoints())
                faces = triangles[np.all(triangles < count, axis=1)]
                cells = vtk.vtkCellArray()
                cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).astype(np.int64).ravel(), deep=True))
                inner.SetPolys(cells)
                distance = vtk.vtkImplicitPolyDataDistance()
                distance.SetInput(inner)
                normals = vtk.vtkPolyDataNormals()
                normals.SetInputData(mesh)
                normals.SplittingOff()
                normals.Update()
                vectors = vtk_to_numpy(normals.GetOutput().GetPointData().GetNormals())
                editable = np.asarray(source.get("editable", [False] * len(points)))
                for index in np.flatnonzero(editable):
                    thickness = -distance.EvaluateFunction(points[index])
                    delta = max(0., min(self.reduction.value(), thickness - wall))
                    points[index] -= vectors[index] * delta
                mesh.GetPoints().SetData(numpy_to_vtk(points, deep=True))
                metadata = {key: copy.deepcopy(value) for key, value in source.items() if key not in {"mesh", "actor", "name", "role"}}
                metadata["params"]["wall"] = wall
                metadata["cutback_mm"] = self.reduction.value()
                self.window.analyze_print_mesh(mesh, name)
                self.window.push_undo()
                metadata.pop("export_enabled", None)
                metadata.pop("superseded_by", None)
                key = self.window.add_object(mesh, source["name"] + " · 컷백", "restoration", **metadata)
                source.update(export_enabled=False, superseded_by=key)
                source["actor"].SetVisibility(False)
                self.window.refresh_scene()
                self.info.setText("내면과 마진을 유지한 컷백 보철을 생성했습니다.")
                return
            if kind in {4, 5}:
                state = self.window.teeth[str(self.window.current_tooth)]
                margin = np.asarray(state.get("margin", []), dtype=float)
                if not state.get("closed") or len(margin) < 6:
                    raise ValueError("선택 치아의 마진을 지정하고 닫으세요.")
                axis = np.asarray(state["axis"], dtype=float)
                axis /= np.linalg.norm(axis)
                x = np.array([1., 0, 0]) if abs(axis[0]) < .85 else np.array([0., 1, 0])
                x -= axis * np.dot(x, axis)
                x /= np.linalg.norm(x)
                basis = np.column_stack([x, np.cross(axis, x), axis])
                center = margin.mean(axis=0)
                polygon = (margin - center) @ basis[:, :2]
                outline = manifold3d.CrossSection([polygon])
                if outline.is_empty() or outline.area() < .1:
                    raise ValueError("마진의 평면 윤곽이 올바르지 않습니다.")
                height = self.height.value()
                radius = np.sqrt(outline.area() / np.pi)
                scale = 1 - np.tan(np.radians(self.taper.value())) * height / max(radius, .1)
                if scale <= .25:
                    raise ValueError("경사와 높이를 줄이세요. 상단이 너무 좁습니다.")
                if kind == 4:
                    outer = outline.offset(wall + gap).extrude(height, scale_top=(scale, scale))
                    cavity = outline.offset(gap).extrude(height - wall, scale_top=(scale, scale)).translate((0, 0, -.01))
                    solid = outer - cavity
                else:
                    cavity = outline.offset(wall + 2 * gap).extrude(height + .02, scale_top=(scale, scale)).translate((0, 0, -.01))
                    solid = outline.offset(2 * wall + 2 * gap).extrude(height + wall, scale_top=(scale, scale)) - cavity
                solid = solid.transform(np.column_stack([basis, center]))
                if solid.status() != manifold3d.Error.NoError or solid.is_empty():
                    raise ValueError("텔레스코픽 형상 생성에 실패했습니다.")
                result = solid.to_mesh64()
                mesh = vtk.vtkPolyData()
                points = vtk.vtkPoints()
                points.SetData(numpy_to_vtk(np.asarray(result.vert_properties[:, :3]), deep=True))
                mesh.SetPoints(points)
                cells = vtk.vtkCellArray()
                faces = np.asarray(result.tri_verts, dtype=np.int64)
                cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).ravel(), deep=True))
                mesh.SetPolys(cells)
            else:
                mesh, report, solid = self.window.analyze_print_mesh(source["mesh"], source["name"])
                bounds = np.asarray(mesh.GetBounds()).reshape(3, 2)
                spacing = max(.10, float(np.max(bounds[:, 1] - bounds[:, 0]) / 120))
                expanded = bounds + np.array([-1, 1]) * (wall + gap + 3 * spacing)
                dimensions = np.ceil((expanded[:, 1] - expanded[:, 0]) / spacing).astype(int) + 1
                if np.prod(dimensions) > 5_000_000:
                    raise ValueError("모델이 너무 큽니다. 개별 치아를 선택하세요.")
                distance = vtk.vtkImplicitPolyDataDistance()
                distance.SetInput(mesh)
                sample = vtk.vtkSampleFunction()
                sample.SetImplicitFunction(distance)
                sample.SetModelBounds(*expanded.ravel())
                sample.SetSampleDimensions(*map(int, dimensions))
                sample.ComputeNormalsOff()
                sample.Update()
                image = sample.GetOutput()
                values = vtk_to_numpy(image.GetPointData().GetScalars()).reshape(tuple(dimensions[::-1]))
                field = np.maximum(values, -wall - values) if kind == 2 else np.maximum(gap - values, values - gap - wall)
                dimension = self.direction.currentIndex() // 2 if kind == 1 else 2
                sign = 1 if kind != 1 or self.direction.currentIndex() % 2 == 0 else -1
                shape = [1, 1, 1]
                shape[2 - dimension] = int(dimensions[dimension])
                coordinates = np.linspace(expanded[dimension, 0], expanded[dimension, 1], dimensions[dimension]).reshape(shape)
                plane = bounds[dimension, 0] + np.ptp(bounds[dimension]) * self.trim.value()
                field = np.maximum(field, (plane - coordinates) * sign)
                image.GetPointData().SetScalars(numpy_to_vtk(np.ascontiguousarray(-field).ravel(), deep=True))
                contour = vtk.vtkFlyingEdges3D()
                contour.SetInputData(image)
                contour.SetValue(0, spacing * 1e-4)
                contour.Update()
                mesh = vtk.vtkPolyData()
                mesh.DeepCopy(contour.GetOutput())
            self.window.analyze_print_mesh(mesh, name)
            self.window.push_undo()
            self.window.add_object(mesh, f"{self.window.current_tooth} · {name}", "part", tooth=self.window.current_tooth, design_kind=name, nominal_wall=wall, gap=gap)
            self.window.focus_selection()
            self.info.setText(name + " 형상을 생성했습니다. 절단 위치·내면·두께를 검사하세요.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "보철 확장 설계", str(error))
