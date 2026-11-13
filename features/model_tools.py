import manifold3d
import numpy as np
import vtk
from PySide6 import QtGui, QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy


class ModelTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.model = QtWidgets.QComboBox()
        self.die = QtWidgets.QComboBox()
        self.radius = QtWidgets.QDoubleSpinBox()
        self.radius.setRange(.5, 4)
        self.radius.setValue(1.5)
        self.depth = QtWidgets.QDoubleSpinBox()
        self.depth.setRange(1, 15)
        self.depth.setValue(4)
        self.clearance = QtWidgets.QDoubleSpinBox()
        self.clearance.setRange(.03, .5)
        self.clearance.setValue(.12)
        self.text = QtWidgets.QLineEdit("EXOCADE")
        self.text_height = QtWidgets.QDoubleSpinBox()
        self.text_height.setRange(1, 10)
        self.text_height.setValue(3)
        self.text_depth = QtWidgets.QDoubleSpinBox()
        self.text_depth.setRange(.1, 2)
        self.text_depth.setValue(.5)
        for title, widget in [("베이스 모델", self.model), ("분리 다이", self.die), ("핀 반경 (mm)", self.radius),
                              ("핀 길이 (mm)", self.depth), ("결합 여유 (mm)", self.clearance), ("모델 표기", self.text),
                              ("문자 높이 (mm)", self.text_height), ("문자 돌출 (mm)", self.text_depth)]:
            form.addRow(title, widget)
        layout.addLayout(form)
        for title, mode in [("키형 핀과 대응 소켓 생성", "socket"), ("베이스 바닥에 문자 추가", "label")]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, value=mode: self.run(value))
            layout.addWidget(button)
        self.info = QtWidgets.QLabel("핀·소켓은 같은 위치의 모델과 분리 다이를 선택합니다. 문자는 베이스 바닥 중앙에 배치합니다. 실제 출력 공차에 맞게 결합 여유를 조정하세요.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def refresh(self):
        for combo in [self.model, self.die]:
            previous = combo.currentData()
            combo.clear()
            for key, obj in self.window.objects.items():
                if obj["role"] in {"model", "die", "part", "scan", "reference"} and obj.get("export_enabled", True):
                    combo.addItem(obj["name"], key)
            combo.setCurrentIndex(max(0, combo.findData(previous or self.window.active_id)))
        die_index = next((i for i in range(self.die.count()) if self.window.objects[self.die.itemData(i)]["role"] == "die"), -1)
        if die_index >= 0:
            self.die.setCurrentIndex(die_index)

    def run(self, mode):
        try:
            model_id = self.model.currentData()
            model = self.window.objects.get(model_id)
            if model is None:
                raise ValueError("베이스 모델을 선택하세요.")
            model_mesh, report, model_solid = self.window.analyze_print_mesh(model["mesh"], model["name"])
            outputs = []
            originals = [model]
            if mode == "label":
                text = self.text.text().strip()
                if not text or len(text) > 50:
                    raise ValueError("1~50자의 모델 표기를 입력하세요.")
                path = QtGui.QPainterPath()
                path.addText(0, 0, QtGui.QFont("Malgun Gothic", 100), text)
                bounds = path.boundingRect()
                if bounds.height() <= 0:
                    raise ValueError("문자 윤곽을 생성할 수 없습니다.")
                scale = self.text_height.value() / bounds.height()
                contours = []
                for polygon in path.toSubpathPolygons():
                    contour = [[(point.x() - bounds.center().x()) * scale, -(point.y() - bounds.center().y()) * scale] for point in polygon]
                    if len(contour) >= 4:
                        contours.append(contour[:-1] if contour[0] == contour[-1] else contour)
                section = manifold3d.CrossSection(contours, manifold3d.FillRule.NonZero)
                if section.is_empty():
                    raise ValueError("문자 면적이 비어 있습니다.")
                center = model_mesh.GetCenter()
                label = section.extrude(self.text_depth.value() + .15).translate((center[0], center[1], model_mesh.GetBounds()[4] - self.text_depth.value()))
                if (model_solid ^ label).volume() < 1e-8:
                    raise ValueError("문자가 바닥에 닿지 않습니다. 평면 베이스 모델을 선택하세요.")
                combined = model_solid + label
                outputs.append((combined, model["name"] + " · 표기", "model", {"model_label": text}))
            else:
                die_id = self.die.currentData()
                die = self.window.objects.get(die_id)
                if die is None or die_id == model_id:
                    raise ValueError("베이스와 다른 분리 다이를 선택하세요.")
                die_mesh, die_report, die_solid = self.window.analyze_print_mesh(die["mesh"], die["name"])
                axis = np.asarray(die.get("axis", self.window.teeth[str(self.window.current_tooth)]["axis"]), dtype=float)
                axis /= np.linalg.norm(axis)
                x = np.array([1., 0, 0]) if abs(axis[0]) < .85 else np.array([0., 1, 0])
                x -= axis * np.dot(x, axis)
                x /= np.linalg.norm(x)
                basis = np.column_stack([x, np.cross(axis, x), axis])
                center = np.asarray(die_mesh.GetCenter())
                local = (vtk_to_numpy(die_mesh.GetPoints().GetData()) - center) @ basis
                bottom = float(local[:, 2].min())
                radius, depth = self.radius.value(), self.depth.value()
                pin = manifold3d.Manifold.cylinder(depth + .3, radius, circular_segments=64)
                flat = manifold3d.Manifold.cube((radius * 2, radius * 3, depth + 1)).translate((radius * .65, -radius * 1.5, -.1))
                pin = (pin - flat).translate((0, 0, bottom - depth)).transform(np.column_stack([basis, center]))
                keyed = die_solid + pin
                if keyed.status() != manifold3d.Error.NoError or len(keyed.decompose()) != 1:
                    raise ValueError("핀을 다이에 연결하지 못했습니다. 크기를 줄이세요.")
                raw = keyed.to_mesh64()
                surface = vtk.vtkPolyData()
                points = vtk.vtkPoints()
                points.SetData(numpy_to_vtk(np.asarray(raw.vert_properties[:, :3]), deep=True))
                surface.SetPoints(points)
                faces = np.asarray(raw.tri_verts, dtype=np.int64)
                cells = vtk.vtkCellArray()
                cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).ravel(), deep=True))
                surface.SetPolys(cells)
                distance = vtk.vtkImplicitPolyDataDistance()
                distance.SetInput(surface)
                gap = self.clearance.value()
                bounds = np.asarray(surface.GetBounds()).reshape(3, 2)
                step = max(.08, gap / 2, np.max(np.ptp(bounds, axis=1)) / 140)
                bounds += np.array([-1, 1]) * (gap + 3 * step)
                dimensions = np.ceil(np.ptp(bounds, axis=1) / step).astype(int) + 1
                if np.prod(dimensions) > 5_000_000:
                    raise ValueError("다이가 너무 큽니다. 개별 다이를 선택하세요.")
                sample = vtk.vtkSampleFunction()
                sample.SetImplicitFunction(distance)
                sample.SetModelBounds(*bounds.ravel())
                sample.SetSampleDimensions(*map(int, dimensions))
                sample.ComputeNormalsOff()
                sample.Update()
                contour = vtk.vtkFlyingEdges3D()
                contour.SetInputConnection(sample.GetOutputPort())
                contour.SetValue(0, gap)
                contour.Update()
                reverse = vtk.vtkReverseSense()
                reverse.SetInputConnection(contour.GetOutputPort())
                reverse.ReverseCellsOn()
                reverse.ReverseNormalsOn()
                reverse.Update()
                expanded, expanded_report, cavity = self.window.analyze_print_mesh(reverse.GetOutput(), "다이 결합 여유")
                if (model_solid ^ cavity).volume() < 1e-8:
                    raise ValueError("다이·핀이 베이스와 겹치지 않습니다.")
                socket = model_solid - cavity
                outputs.append((keyed, die["name"] + " · 키형 핀", "die", {"axis": axis.tolist(), "keyed_socket": True, "fit_gap": gap}))
                outputs.append((socket, model["name"] + " · 다이 소켓", "model", {"socket_for": die_id, "fit_gap": gap}))
                originals.append(die)
            staged = []
            for solid, name, role, metadata in outputs:
                if solid.status() != manifold3d.Error.NoError or solid.is_empty():
                    raise ValueError("모델 연산 결과가 비어 있습니다.")
                data = solid.to_mesh64()
                mesh = vtk.vtkPolyData()
                points = vtk.vtkPoints()
                points.SetData(numpy_to_vtk(np.asarray(data.vert_properties[:, :3]), deep=True))
                mesh.SetPoints(points)
                faces = np.asarray(data.tri_verts, dtype=np.int64)
                cells = vtk.vtkCellArray()
                cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).ravel(), deep=True))
                mesh.SetPolys(cells)
                self.window.analyze_print_mesh(mesh, name)
                staged.append((mesh, name, role, metadata))
            self.window.push_undo()
            keys = [self.window.add_object(mesh, name, role, tooth=self.window.current_tooth, **metadata) for mesh, name, role, metadata in staged]
            for original in originals:
                original.update(export_enabled=False, superseded_by=keys[-1])
                original["actor"].SetVisibility(False)
            self.window.refresh_scene()
            self.window.focus_selection()
            self.info.setText("모델 생성과 폐합 검사를 완료했습니다. 원본은 보존하고 전체 출력에서 제외했습니다.")
            self.refresh()
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "모델·다이 도구", str(error))
