import numpy as np
import vtk
from PySide6 import QtCore, QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy


class BlockoutTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.source = QtWidgets.QComboBox()
        self.spacing = QtWidgets.QDoubleSpinBox()
        self.spacing.setRange(.1, 1)
        self.spacing.setValue(.3)
        self.spacing.setSuffix(" mm")
        self.gap = QtWidgets.QDoubleSpinBox()
        self.gap.setRange(0, 2)
        self.gap.setValue(0)
        self.gap.setSuffix(" mm")
        self.direction = QtWidgets.QComboBox()
        self.direction.addItems(["선택 치아 삽입축", "선택 치아 삽입축 반대"])
        form.addRow("스캔·모델", self.source)
        form.addRow("투영 방향", self.direction)
        form.addRow("계산 간격", self.spacing)
        form.addRow("상부 표면 여유", self.gap)
        layout.addLayout(form)
        button = QtWidgets.QPushButton("삽입 방향 투영 블록아웃 생성")
        button.clicked.connect(self.run)
        layout.addWidget(button)
        self.info = QtWidgets.QLabel("삽입축에서 보이는 첫 표면 아래를 채운 모델을 생성합니다. 원본은 보존됩니다. 계산 간격에 따라 세부 형태가 달라집니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def refresh(self):
        previous = self.source.currentData()
        self.source.clear()
        for key, obj in self.window.objects.items():
            if obj["role"] in {"scan", "model", "reference", "die", "part"}:
                self.source.addItem(obj["name"], key)
        self.source.setCurrentIndex(max(0, self.source.findData(previous or self.window.active_id)))

    def run(self):
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            source = self.window.objects.get(self.source.currentData())
            if source is None:
                raise ValueError("스캔·모델을 선택하세요.")
            state = self.window.teeth[str(self.window.current_tooth)]
            axis = np.asarray(state["axis"], dtype=float) * (1 if self.direction.currentIndex() == 0 else -1)
            axis /= np.linalg.norm(axis)
            x = np.array([1., 0, 0]) if abs(axis[0]) < .85 else np.array([0., 1, 0])
            x -= axis * np.dot(x, axis)
            x /= np.linalg.norm(x)
            basis = np.column_stack([x, np.cross(axis, x), axis])
            origin = np.asarray(source["mesh"].GetCenter())
            local = vtk.vtkPolyData()
            local.DeepCopy(source["mesh"])
            points = vtk.vtkPoints()
            coordinates = (vtk_to_numpy(local.GetPoints().GetData()) - origin) @ basis
            points.SetData(numpy_to_vtk(coordinates, deep=True))
            local.SetPoints(points)
            lo, hi = coordinates.min(axis=0), coordinates.max(axis=0)
            step = self.spacing.value()
            gap = self.gap.value()
            sizes = np.ceil((hi - lo + gap + 4 * step) / step).astype(int) + 1
            if np.prod(sizes) > 4_000_000 or sizes[0] * sizes[1] > 35000:
                raise ValueError("계산량이 큽니다. 계산 간격을 늘리거나 개별 치아를 선택하세요.")
            start = lo - 2 * step
            tree = vtk.vtkOBBTree()
            tree.SetDataSet(local)
            tree.BuildLocator()
            columns = np.full((sizes[1], sizes[0]), start[2] - step)
            hits = vtk.vtkPoints()
            for row in range(1, sizes[1] - 1):
                y = start[1] + row * step
                for column in range(1, sizes[0] - 1):
                    x_value = start[0] + column * step
                    hits.Reset()
                    tree.IntersectWithLine([x_value, y, hi[2] + step], [x_value, y, lo[2] - step], hits, None)
                    if hits.GetNumberOfPoints():
                        columns[row, column] = max(hits.GetPoint(i)[2] for i in range(hits.GetNumberOfPoints())) + gap
            z = start[2] + np.arange(sizes[2]) * step
            inside = (z[:, None, None] <= columns) & (z[:, None, None] >= lo[2])
            if not inside.any():
                raise ValueError("투영 표면을 찾을 수 없습니다.")
            image = vtk.vtkImageData()
            image.SetDimensions(*map(int, sizes))
            image.SetSpacing(step, step, step)
            image.SetOrigin(*start)
            image.GetPointData().SetScalars(numpy_to_vtk(inside.astype(np.float32).ravel(), deep=True))
            contour = vtk.vtkFlyingEdges3D()
            contour.SetInputData(image)
            contour.SetValue(0, .5)
            contour.Update()
            mesh = vtk.vtkPolyData()
            mesh.DeepCopy(contour.GetOutput())
            transformed = vtk_to_numpy(mesh.GetPoints().GetData()).astype(float) @ basis.T + origin
            mesh.GetPoints().SetData(numpy_to_vtk(transformed, deep=True))
            self.window.analyze_print_mesh(mesh, "블록아웃")
            self.window.push_undo()
            self.window.add_object(mesh, source["name"] + " · 투영 블록아웃", "model", tooth=self.window.current_tooth, blockout_axis=axis.tolist(), voxel_size=step, source_id=self.source.currentData())
            self.window.focus_selection()
            self.info.setText(f"투영 블록아웃을 생성했습니다. 계산 간격 {step:g} mm · 상부 여유 {gap:g} mm")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "언더컷 블록아웃", str(error))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
