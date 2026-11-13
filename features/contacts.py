import copy

import manifold3d
import numpy as np
import vtk
from PySide6 import QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy


class ContactTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.source = QtWidgets.QComboBox()
        self.target = QtWidgets.QComboBox()
        self.kind = QtWidgets.QComboBox()
        self.kind.addItems(["인접면 여유 조정", "대합면 여유 조정", "폰틱 기저면 적합", "타원형 브릿지 연결"])
        self.controls = {}
        for key, title, lo, hi, value in [("gap", "접촉 여유", 0, 2, .05), ("limit", "최대 이동", .05, 3, .5),
                                        ("wall", "보호할 최소 두께", .2, 3, .6), ("rx", "연결부 가로 반경", .5, 4, 1.5),
                                        ("ry", "연결부 세로 반경", .5, 4, 2)]:
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(lo, hi)
            widget.setValue(value)
            widget.setSuffix(" mm")
            self.controls[key] = widget
            form.addRow(title, widget)
        form.insertRow(0, "편집할 보철", self.source)
        form.insertRow(1, "인접치·대합치·치은·연결 보철", self.target)
        form.insertRow(2, "작업", self.kind)
        layout.addLayout(form)
        button = QtWidgets.QPushButton("접촉·연결 계산")
        button.clicked.connect(self.run)
        layout.addWidget(button)
        self.info = QtWidgets.QLabel("대상은 이미 정렬된 모델을 사용합니다. 원본 내면 정보가 있으면 내면과 마진을 보호합니다. 결과는 별도 오브젝트로 생성됩니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def refresh(self):
        for combo in [self.source, self.target]:
            previous = combo.currentData()
            combo.clear()
            for key, obj in self.window.objects.items():
                if obj["role"] not in {"print", "library"}:
                    combo.addItem(obj["name"], key)
            combo.setCurrentIndex(max(0, combo.findData(previous or self.window.active_id)))
        if self.source.currentData() == self.target.currentData() and self.target.count() > 1:
            self.target.setCurrentIndex((self.source.currentIndex() + 1) % self.target.count())

    def run(self):
        try:
            source_id, target_id = self.source.currentData(), self.target.currentData()
            source, target = self.window.objects.get(source_id), self.window.objects.get(target_id)
            if source is None or target is None or source is target:
                raise ValueError("서로 다른 원본과 대상을 선택하세요.")
            values = {key: widget.value() for key, widget in self.controls.items()}
            kind = self.kind.currentIndex()
            metadata = {key: copy.deepcopy(value) for key, value in source.items() if key not in {"mesh", "actor", "name", "role", "superseded_by", "export_enabled"}}
            if kind == 3:
                first_mesh, first_report, first = self.window.analyze_print_mesh(source["mesh"], source["name"])
                second_mesh, second_report, second = self.window.analyze_print_mesh(target["mesh"], target["name"])
                centers = np.asarray([source["mesh"].GetCenter(), target["mesh"].GetCenter()])
                direction = centers[1] - centers[0]
                length = np.linalg.norm(direction)
                if length < .1 or length > 40:
                    raise ValueError("연결 거리는 0.1~40 mm 범위여야 합니다.")
                z = direction / length
                x = np.array([1., 0, 0]) if abs(z[0]) < .85 else np.array([0., 1, 0])
                x -= z * np.dot(x, z)
                x /= np.linalg.norm(x)
                basis = np.column_stack([x, np.cross(z, x), z])
                connector = manifold3d.Manifold.cylinder(length, 1, circular_segments=64).scale((values["rx"], values["ry"], 1)).transform(np.column_stack([basis, centers[0]]))
                if (first ^ connector).volume() < 1e-8 or (second ^ connector).volume() < 1e-8:
                    raise ValueError("연결부가 양쪽 보철에 닿지 않습니다. 반경이나 배치를 조정하세요.")
                result = first + connector + second
                for original in [source, target]:
                    count = int(original.get("inner_count", 0))
                    if not count:
                        continue
                    ring_count = int(original.get("ring_points", 96))
                    vertices = np.asarray(vtk_to_numpy(original["mesh"].GetPoints().GetData()), dtype=np.float64)
                    if ring_count < 3 or count <= ring_count or count > len(vertices):
                        raise ValueError("보철 내면 정보를 확인하세요.")
                    axis = np.asarray(original.get("axis", [0, 0, 1]), dtype=float)
                    axis /= np.linalg.norm(axis)
                    boundary = vertices[count - ring_count:count]
                    lower = boundary - axis * (length + np.linalg.norm(np.ptp(vertices, axis=0)) + 5)
                    cavity_vertices = np.vstack([vertices[:count], lower, lower.mean(axis=0)])
                    faces = vtk_to_numpy(original["mesh"].GetPolys().GetData()).reshape(-1, 4)[:, 1:]
                    cavity_faces = faces[np.all(faces < count, axis=1)][:, [0, 2, 1]].tolist()
                    for j in range(ring_count):
                        k = (j + 1) % ring_count
                        a, b, c, d = count - ring_count + j, count - ring_count + k, count + j, count + k
                        cavity_faces.extend([[a, c, d], [a, d, b], [len(cavity_vertices) - 1, d, c]])
                    cavity = manifold3d.Manifold(manifold3d.Mesh64(np.ascontiguousarray(cavity_vertices), np.ascontiguousarray(cavity_faces, dtype=np.uint64)))
                    if cavity.status() != manifold3d.Error.NoError or cavity.is_empty():
                        raise ValueError("브릿지의 내면 공간을 보존하지 못했습니다.")
                    result = result - cavity
                if result.status() != manifold3d.Error.NoError or len(result.decompose()) != 1:
                    raise ValueError("브릿지를 단일 입체로 결합하지 못했습니다.")
                data = result.to_mesh64()
                mesh = vtk.vtkPolyData()
                points = vtk.vtkPoints()
                points.SetData(numpy_to_vtk(np.asarray(data.vert_properties[:, :3]), deep=True))
                mesh.SetPoints(points)
                faces = np.asarray(data.tri_verts, dtype=np.int64)
                cells = vtk.vtkCellArray()
                cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).ravel(), deep=True))
                mesh.SetPolys(cells)
                metadata = {"tooth": self.window.current_tooth, "connector_radii": [values["rx"], values["ry"]], "source_pair": [source_id, target_id]}
                role = "part"
                changed = len(faces)
            else:
                mesh = vtk.vtkPolyData()
                mesh.DeepCopy(source["mesh"])
                points = vtk_to_numpy(mesh.GetPoints().GetData()).astype(float).copy()
                triangles = vtk_to_numpy(mesh.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
                editable = np.asarray(source.get("editable", [True] * len(points)), dtype=bool)
                distance = vtk.vtkImplicitPolyDataDistance()
                distance.SetInput(target["mesh"])
                inner_distance = None
                if source.get("inner_count"):
                    faces = triangles[np.all(triangles < source["inner_count"], axis=1)]
                    inner = vtk.vtkPolyData()
                    inner.SetPoints(mesh.GetPoints())
                    cells = vtk.vtkCellArray()
                    cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).astype(np.int64).ravel(), deep=True))
                    inner.SetPolys(cells)
                    inner_distance = vtk.vtkImplicitPolyDataDistance()
                    inner_distance.SetInput(inner)
                axis = np.asarray(source.get("axis", self.window.teeth[str(self.window.current_tooth)]["axis"]), dtype=float)
                axis /= np.linalg.norm(axis)
                if kind == 2:
                    heights = points @ axis
                    editable &= heights < np.quantile(heights, .35)
                changed = 0
                for index in np.flatnonzero(editable):
                    value = distance.EvaluateFunction(points[index])
                    if kind == 2:
                        if abs(value - values["gap"]) > values["limit"]:
                            continue
                    elif value >= values["gap"] or value < -values["limit"]:
                        continue
                    gradient = [0., 0., 0.]
                    distance.EvaluateGradient(points[index], gradient)
                    gradient = np.asarray(gradient)
                    norm = np.linalg.norm(gradient)
                    if norm < 1e-8:
                        continue
                    gradient /= norm
                    if kind in {1, 2}:
                        projection = float(np.dot(gradient, axis))
                        if abs(projection) < .25:
                            continue
                        delta = axis * np.clip((values["gap"] - value) / projection, -values["limit"], values["limit"])
                    else:
                        delta = gradient * min(values["limit"], values["gap"] - value)
                    proposed = points[index] + delta
                    if inner_distance and -inner_distance.EvaluateFunction(proposed) < values["wall"]:
                        continue
                    points[index] = proposed
                    changed += 1
                if not changed:
                    raise ValueError("조정할 정점이 없습니다. 대상·여유·최대 이동을 확인하세요.")
                mesh.GetPoints().SetData(numpy_to_vtk(points, deep=True))
                role = source["role"]
            self.window.analyze_print_mesh(mesh, self.kind.currentText())
            self.window.push_undo()
            result_id = self.window.add_object(mesh, source["name"] + " · " + self.kind.currentText(), role, **metadata)
            for original in [source, target] if kind == 3 else [source]:
                original["export_enabled"] = False
                original["superseded_by"] = result_id
                original["actor"].SetVisibility(False)
            self.window.refresh_scene()
            self.window.focus_selection()
            self.info.setText(f"{changed:,}개 요소를 계산했습니다. 원본은 보존하고 전체 출력에서 제외했습니다.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "접촉·브릿지 도구", str(error))
