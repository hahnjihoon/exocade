import manifold3d
import numpy as np
import vtk
from PySide6 import QtCore, QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy


class DentureTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.source = QtWidgets.QComboBox()
        self.kind = QtWidgets.QComboBox()
        self.kind.addItems(["무치악 의치상", "선택 인공치 소켓", "경로 기반 부분의치 프레임", "선택 치아 클라스프·레스트"])
        self.controls = {}
        for key, title, lo, hi, value in [("wall", "의치상 두께", .5, 6, 2), ("gap", "조직·소켓 여유", 0, 1, .15),
                                        ("trim", "하단 절단 Z", -1000, 1000, 0), ("radius", "프레임·클라스프 반경", .2, 4, .7),
                                        ("arc", "클라스프 각도", 90, 320, 230)]:
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(lo, hi)
            widget.setValue(value)
            self.controls[key] = widget
            form.addRow(title, widget)
        form.insertRow(0, "스캔·의치상·지대치", self.source)
        form.insertRow(1, "설계", self.kind)
        layout.addLayout(form)
        layout.addWidget(QtWidgets.QLabel("소켓을 만들 인공치 (여러 개 선택 가능)"))
        self.teeth = QtWidgets.QListWidget()
        self.teeth.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.MultiSelection)
        layout.addWidget(self.teeth)
        button = QtWidgets.QPushButton("의치 형상 생성")
        button.clicked.connect(self.run)
        layout.addWidget(button)
        self.info = QtWidgets.QLabel("의치상은 닫힌 무치악 스캔을 사용합니다. 프레임은 형태 편집 단계의 배치 경로를 사용합니다. 클라스프는 선택 모델 주변에 생성하며 경로 점이 있으면 첫 점에 레스트를 만듭니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

    def refresh(self):
        previous = self.source.currentData()
        self.source.clear()
        selected = {item.data(QtCore.Qt.ItemDataRole.UserRole) for item in self.teeth.selectedItems()}
        self.teeth.clear()
        for key, obj in self.window.objects.items():
            if obj["role"] not in {"print", "library"} and obj.get("export_enabled", True):
                self.source.addItem(obj["name"], key)
            if obj["role"] in {"pontic", "restoration", "part"} and obj.get("export_enabled", True):
                item = QtWidgets.QListWidgetItem(obj["name"])
                item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
                self.teeth.addItem(item)
                item.setSelected(key in selected)
        self.source.setCurrentIndex(max(0, self.source.findData(previous or self.window.active_id)))

    def run(self):
        try:
            source_id = self.source.currentData()
            source = self.window.objects.get(source_id)
            if source is None:
                raise ValueError("스캔·의치상·지대치를 선택하세요.")
            values = {key: widget.value() for key, widget in self.controls.items()}
            kind = self.kind.currentIndex()
            if kind == 0:
                mesh, report, base = self.window.analyze_print_mesh(source["mesh"], source["name"])
                bounds = np.asarray(mesh.GetBounds()).reshape(3, 2)
                step = max(min(.25, values["wall"] / 3), float(np.max(np.ptp(bounds, axis=1)) / 120))
                bounds += np.array([-1, 1]) * (values["gap"] + values["wall"] + 3 * step)
                dimensions = np.ceil(np.ptp(bounds, axis=1) / step).astype(int) + 1
                if np.prod(dimensions) > 6_000_000:
                    raise ValueError("계산량이 큽니다. 스캔을 정리하거나 범위를 줄이세요.")
                distance = vtk.vtkImplicitPolyDataDistance()
                distance.SetInput(mesh)
                sample = vtk.vtkSampleFunction()
                sample.SetImplicitFunction(distance)
                sample.SetModelBounds(*bounds.ravel())
                sample.SetSampleDimensions(*map(int, dimensions))
                sample.ComputeNormalsOff()
                sample.Update()
                image = sample.GetOutput()
                field = vtk_to_numpy(image.GetPointData().GetScalars()).reshape(tuple(dimensions[::-1]))
                field = np.maximum(values["gap"] - field, field - values["gap"] - values["wall"])
                z = np.linspace(bounds[2, 0], bounds[2, 1], dimensions[2])[:, None, None]
                field = np.maximum(field, values["trim"] - z)
                image.GetPointData().SetScalars(numpy_to_vtk(np.ascontiguousarray(-field).ravel(), deep=True))
                contour = vtk.vtkFlyingEdges3D()
                contour.SetInputData(image)
                contour.SetValue(0, step * 1e-4)
                contour.Update()
                mesh, report, solid = self.window.analyze_print_mesh(contour.GetOutput(), "무치악 의치상")
            elif kind == 1:
                mesh, report, solid = self.window.analyze_print_mesh(source["mesh"], source["name"])
                selected = [self.window.objects.get(item.data(QtCore.Qt.ItemDataRole.UserRole)) for item in self.teeth.selectedItems()]
                if not selected:
                    raise ValueError("소켓을 만들 인공치를 선택하세요.")
                intersections = 0
                for tooth in selected:
                    if tooth is source:
                        continue
                    tooth_mesh, tooth_report, tooth_solid = self.window.analyze_print_mesh(tooth["mesh"], tooth["name"])
                    center = np.asarray(tooth_mesh.GetCenter())
                    spans = np.maximum(np.ptp(np.asarray(tooth_mesh.GetBounds()).reshape(3, 2), axis=1), .1)
                    scale = 1 + 2 * values["gap"] / spans
                    cavity = tooth_solid.translate(-center).scale(scale).translate(center)
                    if (solid ^ cavity).volume() > 1e-8:
                        solid = solid - cavity
                        intersections += 1
                if not intersections:
                    raise ValueError("선택 인공치가 의치상과 겹치지 않습니다. 치아 배치를 조정하세요.")
            else:
                if kind == 2:
                    path = np.asarray(self.window.layout_points, dtype=float)
                    if len(path) < 2:
                        raise ValueError("형태 편집 단계에서 프레임 경로를 2점 이상 선택하세요.")
                else:
                    bounds = np.asarray(source["mesh"].GetBounds()).reshape(3, 2)
                    center = np.asarray(source["mesh"].GetCenter())
                    radii = np.ptp(bounds[:2], axis=1) / 2 + values["gap"] + values["radius"]
                    angles = np.linspace(0, np.radians(values["arc"]), 80)
                    path = np.column_stack([center[0] + radii[0] * np.cos(angles), center[1] + radii[1] * np.sin(angles), np.full(len(angles), center[2])])
                vtk_points = vtk.vtkPoints()
                vtk_points.SetData(numpy_to_vtk(path, deep=True))
                data = vtk.vtkPolyData()
                data.SetPoints(vtk_points)
                lines = vtk.vtkCellArray()
                lines.InsertNextCell(len(path))
                for index in range(len(path)):
                    lines.InsertCellPoint(index)
                data.SetLines(lines)
                tube = vtk.vtkTubeFilter()
                tube.SetInputData(data)
                tube.SetRadius(values["radius"])
                tube.SetNumberOfSides(32)
                tube.CappingOn()
                tube.Update()
                mesh, report, solid = self.window.analyze_print_mesh(tube.GetOutput(), "부분의치 프레임")
                if kind == 3:
                    if self.window.layout_points:
                        point = np.asarray(self.window.layout_points[0])
                        solid = solid + manifold3d.Manifold.cube((2.5, 2.5, 1), center=True).translate(point)
            solid = solid.as_original().simplify(1e-5)
            if solid.status() != manifold3d.Error.NoError or solid.is_empty():
                raise ValueError("의치 형상이 비어 있거나 올바르지 않습니다.")
            for tolerance in [1e-5, 3e-5, 1e-4, 3e-4, .001]:
                simplified = solid.as_original().simplify(tolerance)
                data = simplified.to_mesh64()
                vertices = np.asarray(data.vert_properties[:, :3])
                faces = np.asarray(data.tri_verts, dtype=np.int64)
                coordinates = vertices[faces]
                areas = np.linalg.norm(np.cross(coordinates[:, 1] - coordinates[:, 0], coordinates[:, 2] - coordinates[:, 0]), axis=1)
                if len(areas) and np.all(areas > 1e-12):
                    break
            else:
                raise ValueError("의치 형상의 미세 면을 정리하지 못했습니다. 프레임 반경이나 배치를 조정하세요.")
            mesh = vtk.vtkPolyData()
            points = vtk.vtkPoints()
            points.SetData(numpy_to_vtk(np.asarray(data.vert_properties[:, :3]), deep=True))
            mesh.SetPoints(points)
            faces = np.asarray(data.tri_verts, dtype=np.int64)
            cells = vtk.vtkCellArray()
            cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).ravel(), deep=True))
            mesh.SetPolys(cells)
            self.window.analyze_print_mesh(mesh, self.kind.currentText())
            self.window.push_undo()
            key = self.window.add_object(mesh, self.kind.currentText(), "denture" if kind < 2 else "part", tooth=self.window.current_tooth, design_kind=self.kind.currentText(), construction=values, color=(.84, .46, .48) if kind < 2 else (.7, .73, .78))
            if kind == 1:
                source.update(export_enabled=False, superseded_by=key)
                source["actor"].SetVisibility(False)
            self.window.refresh_scene()
            self.window.focus_selection()
            self.info.setText("의치 형상을 생성했습니다. 경계·유지부·인공치 배치를 확인하세요.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "의치 설계", str(error))
