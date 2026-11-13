import manifold3d
import numpy as np
import vtk
from PySide6 import QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy


class BarTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.profile = QtWidgets.QComboBox()
        self.profile.addItems(["원형", "타원형", "사각형", "반원형"])
        self.controls = {}
        for key, title, low, high, value in [("width", "단면 폭 (mm)", .6, 10, 3), ("height", "단면 높이 (mm)", .6, 10, 4),
                                          ("offset", "삽입축 방향 이동 (mm)", -20, 20, 0), ("radius", "유지공 반경 (mm)", .2, 2, .5),
                                          ("pitch", "유지공 간격 (mm)", 2, 20, 6)]:
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(low, high)
            widget.setValue(value)
            self.controls[key] = widget
            form.addRow(title, widget)
        form.insertRow(0, "바 단면", self.profile)
        layout.addLayout(form)
        self.holes = QtWidgets.QCheckBox("유지공 추가")
        layout.addWidget(self.holes)
        button = QtWidgets.QPushButton("배치 경로를 따라 바 생성")
        button.clicked.connect(self.run)
        layout.addWidget(button)
        self.info = QtWidgets.QLabel("형태 편집 단계에서 선택한 배치 경로를 사용합니다. 단면 높이 방향은 선택 치아의 삽입축을 기준으로 합니다. 제조사 체결부는 포함하지 않습니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def refresh(self):
        self.info.setText(f"현재 배치 경로 {len(self.window.layout_points)}점 · 단면 방향은 선택 치아의 삽입축입니다.")

    def run(self):
        try:
            values = {key: widget.value() for key, widget in self.controls.items()}
            path = np.asarray(self.window.layout_points, dtype=float)
            if len(path) < 2 or np.sum(np.linalg.norm(np.diff(path, axis=0), axis=1)) < 1:
                raise ValueError("형태 편집에서 배치 경로를 2점 이상 선택하세요.")
            if np.any(np.linalg.norm(np.diff(path, axis=0), axis=1) < .01):
                raise ValueError("배치 경로에 겹치는 점이 있습니다.")
            axis = np.asarray(self.window.teeth[str(self.window.current_tooth)]["axis"], dtype=float)
            axis /= np.linalg.norm(axis)
            points = vtk.vtkPoints()
            points.SetData(numpy_to_vtk(path, deep=True))
            spline = vtk.vtkParametricSpline()
            spline.SetPoints(points)
            sampler = vtk.vtkParametricFunctionSource()
            sampler.SetParametricFunction(spline)
            sampler.SetUResolution(min(240, max(32, len(path) * 16)))
            sampler.Update()
            centers = vtk_to_numpy(sampler.GetOutput().GetPoints().GetData()).astype(float) + axis * values["offset"]
            kind = self.profile.currentIndex()
            width, height = values["width"] / 2, values["height"] / 2
            if kind < 2:
                angles = np.linspace(0, 2 * np.pi, 48, endpoint=False)
                section = np.column_stack([width * np.cos(angles), (width if kind == 0 else height) * np.sin(angles)])
            elif kind == 2:
                section = np.array([[width, height], [-width, height], [-width, -height], [width, -height]])
            else:
                angles = np.linspace(0, np.pi, 33)
                section = np.column_stack([width * np.cos(angles), height * np.sin(angles)])
            vertices = []
            for index, center in enumerate(centers):
                tangent = centers[min(index + 1, len(centers) - 1)] - centers[max(0, index - 1)]
                tangent /= np.linalg.norm(tangent)
                up = axis - tangent * np.dot(axis, tangent)
                if np.linalg.norm(up) < .05:
                    raise ValueError("경로가 삽입축과 평행합니다. 단면 기준 삽입축을 변경하세요.")
                up /= np.linalg.norm(up)
                across = np.cross(up, tangent)
                vertices.extend(center + section[:, :1] * across + section[:, 1:] * up)
            vertices = np.asarray(vertices)
            count = len(section)
            faces = []
            for ring in range(len(centers) - 1):
                for j in range(count):
                    k = (j + 1) % count
                    a, b, c, d = ring * count + j, ring * count + k, (ring + 1) * count + j, (ring + 1) * count + k
                    faces.extend([[a, b, d], [a, d, c]])
            vertices = np.vstack([vertices, centers[0], centers[-1]])
            for j in range(count):
                k = (j + 1) % count
                faces.extend([[len(vertices) - 2, k, j], [len(vertices) - 1, (len(centers) - 1) * count + j, (len(centers) - 1) * count + k]])
            solid = manifold3d.Manifold(manifold3d.Mesh64(np.ascontiguousarray(vertices), np.ascontiguousarray(faces, dtype=np.uint64)))
            if self.holes.isChecked():
                if values["radius"] * 2 + .4 >= min(values["width"], values["height"]) or values["pitch"] <= 2 * values["radius"] + .5:
                    raise ValueError("유지공 지름과 간격을 단면 크기에 맞게 줄이세요.")
                distances = np.r_[0., np.cumsum(np.linalg.norm(np.diff(centers, axis=0), axis=1))]
                across = np.array([1., 0, 0]) if abs(axis[0]) < .85 else np.array([0., 1, 0])
                across -= axis * np.dot(across, axis)
                across /= np.linalg.norm(across)
                basis = np.column_stack([across, np.cross(axis, across), axis])
                length = max(values["width"], values["height"]) * 2 + 2
                cutters = []
                for position in np.arange(values["pitch"] / 2, distances[-1], values["pitch"]):
                    center = np.array([np.interp(position, distances, centers[:, index]) for index in range(3)])
                    cutters.append(manifold3d.Manifold.cylinder(length, values["radius"], circular_segments=32).transform(np.column_stack([basis, center - axis * length / 2])))
                if cutters:
                    solid = solid - manifold3d.Manifold.batch_boolean(cutters, manifold3d.OpType.Add)
            if solid.status() != manifold3d.Error.NoError or solid.is_empty() or len(solid.decompose()) != 1:
                raise ValueError("단일 바를 만들 수 없습니다. 단면 크기와 경로를 조정하세요.")
            solid = solid.as_original().simplify(1e-5)
            result = solid.to_mesh64()
            mesh = vtk.vtkPolyData()
            points = vtk.vtkPoints()
            points.SetData(numpy_to_vtk(np.asarray(result.vert_properties[:, :3]), deep=True))
            mesh.SetPoints(points)
            faces = np.asarray(result.tri_verts, dtype=np.int64)
            cells = vtk.vtkCellArray()
            cells.SetCells(len(faces), numpy_to_vtkIdTypeArray(np.column_stack([np.full(len(faces), 3), faces]).ravel(), deep=True))
            mesh.SetPolys(cells)
            self.window.analyze_print_mesh(mesh, "단면 바")
            self.window.push_undo()
            self.window.add_object(mesh, self.profile.currentText() + " 단면 바", "bar", tooth=self.window.current_tooth, profile=self.profile.currentText(), construction=values, path=path.tolist())
            self.window.focus_selection()
            self.info.setText("바의 단면·폐합·체적 검사를 완료했습니다.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "바 단면 설계", str(error))
