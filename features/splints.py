import manifold3d
import numpy as np
import vtk
from PySide6 import QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray


class SplintTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.source = QtWidgets.QComboBox()
        self.kind = QtWidgets.QComboBox()
        self.kind.addItems(["경로 윤곽으로 장치 자르기", "상부 교합 테이블 형성", "트레이 천공"])
        self.height = QtWidgets.QDoubleSpinBox()
        self.height.setRange(-1000, 1000)
        self.height.setValue(8)
        self.radius = QtWidgets.QDoubleSpinBox()
        self.radius.setRange(.3, 3)
        self.radius.setValue(1)
        self.spacing = QtWidgets.QDoubleSpinBox()
        self.spacing.setRange(2, 20)
        self.spacing.setValue(6)
        for title, widget in [("스플린트·개인 트레이", self.source), ("작업", self.kind), ("교합면 Z 높이 (mm)", self.height),
                              ("천공 반경 (mm)", self.radius), ("천공 간격 (mm)", self.spacing)]:
            form.addRow(title, widget)
        layout.addLayout(form)
        button = QtWidgets.QPushButton("장치 형상 편집")
        button.clicked.connect(self.run)
        layout.addWidget(button)
        self.info = QtWidgets.QLabel("기본 쉘은 모델·장치 단계에서 생성합니다. 윤곽 절단은 형태 편집 단계의 배치 경로를 닫힌 XY 윤곽으로 사용합니다. 대합치 적합과 동적 교합은 접촉·교합 도구에서 실행할 수 있습니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def refresh(self):
        previous = self.source.currentData()
        self.source.clear()
        for key, obj in self.window.objects.items():
            if obj["role"] in {"appliance", "part", "denture"} and obj.get("export_enabled", True):
                self.source.addItem(obj["name"], key)
        self.source.setCurrentIndex(max(0, self.source.findData(previous or self.window.active_id)))
        obj = self.window.objects.get(self.source.currentData())
        if obj is not None:
            self.height.setValue(obj["mesh"].GetBounds()[5] + .5)

    def run(self):
        try:
            source = self.window.objects.get(self.source.currentData())
            if source is None:
                raise ValueError("스플린트·트레이를 생성하거나 선택하세요.")
            mesh, report, solid = self.window.analyze_print_mesh(source["mesh"], source["name"])
            kind = self.kind.currentIndex()
            bounds = np.asarray(mesh.GetBounds()).reshape(3, 2)
            if kind == 0:
                path = np.asarray(self.window.layout_points, dtype=float)
                if len(path) < 3:
                    raise ValueError("윤곽 경로를 최소 3점 선택하세요.")
                section = manifold3d.CrossSection([path[:, :2]])
                if section.is_empty() or section.area() < .5:
                    raise ValueError("닫힌 XY 윤곽을 만들 수 없습니다.")
                cutter = section.extrude(float(np.ptp(bounds[2]) + 2)).translate((0, 0, bounds[2, 0] - 1))
                solid = solid ^ cutter
            elif kind == 1:
                wall = float(source.get("nominal_wall", source.get("construction", {}).get("wall", .8)))
                start = bounds[2, 1] - max(.1, min(wall * .25, .5))
                height = self.height.value()
                if height <= start + .02 or abs(height - bounds[2, 1]) > 3:
                    raise ValueError(f"교합면 높이는 {start + .02:.2f} mm보다 높고 기존 상단의 3 mm 이내여야 합니다.")
                section = solid.slice(float(start))
                if section.is_empty() or section.area() < .01:
                    raise ValueError("상부 교합면의 단면을 찾지 못했습니다.")
                table = section.extrude(float(height - start + .02)).translate((0, 0, float(start - .01)))
                solid = (solid + table).trim_by_plane((0, 0, -1), -height)
            else:
                step, radius = self.spacing.value(), self.radius.value()
                if step <= 2 * radius + .5:
                    raise ValueError("천공 간격은 지름보다 0.5 mm 이상 커야 합니다.")
                cutters = []
                for x in np.arange(bounds[0, 0] + step, bounds[0, 1] - step / 2, step):
                    for y in np.arange(bounds[1, 0] + step, bounds[1, 1] - step / 2, step):
                        if len(cutters) >= 1000:
                            raise ValueError("천공이 너무 많습니다. 간격을 늘리세요.")
                        cutters.append(manifold3d.Manifold.cylinder(float(np.ptp(bounds[2]) + 2), radius, circular_segments=32).translate((float(x), float(y), bounds[2, 0] - 1)))
                if not cutters:
                    raise ValueError("천공할 공간이 없습니다. 간격을 줄이세요.")
                solid = solid - manifold3d.Manifold.batch_boolean(cutters, manifold3d.OpType.Add)
            solid = solid.as_original().simplify(1e-5)
            if solid.status() != manifold3d.Error.NoError or solid.is_empty():
                raise ValueError("장치 형상 연산에 실패했습니다.")
            result = solid.to_mesh64()
            mesh = vtk.vtkPolyData()
            points = vtk.vtkPoints()
            points.SetData(numpy_to_vtk(np.asarray(result.vert_properties[:, :3]), deep=True))
            mesh.SetPoints(points)
            faces = np.asarray(result.tri_verts, dtype=np.int64)
            cells = vtk.vtkCellArray()
            cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).ravel(), deep=True))
            mesh.SetPolys(cells)
            self.window.analyze_print_mesh(mesh, "장치 편집")
            self.window.push_undo()
            key = self.window.add_object(mesh, source["name"] + " · " + self.kind.currentText(), "appliance", tooth=self.window.current_tooth, appliance_type=source.get("appliance_type", "custom"))
            source.update(export_enabled=False, superseded_by=key)
            source["actor"].SetVisibility(False)
            self.window.refresh_scene()
            self.window.focus_selection()
            self.info.setText("장치 편집을 완료했습니다. 접촉·교합 도구에서 대합치와의 간격을 조정할 수 있습니다.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "스플린트·트레이 편집", str(error))
