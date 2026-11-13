import numpy as np
import vtk
from PySide6 import QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy


class ToothLibrary(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.source = QtWidgets.QComboBox()
        self.style = QtWidgets.QComboBox()
        self.style.addItems(["둥근 형태", "각진 형태", "길쭉한 형태"])
        self.controls = {}
        for key, title, lo, hi, value in [("width", "근원심 폭 배율", .6, 1.6, 1), ("depth", "협설 폭 배율", .6, 1.6, 1),
                                        ("height", "높이 배율", .6, 1.6, 1), ("cusp", "교두 강조 (mm)", 0, 1.5, .3),
                                        ("groove", "중앙 열구 깊이 (mm)", 0, 1, .15)]:
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(lo, hi)
            widget.setValue(value)
            self.controls[key] = widget
            form.addRow(title, widget)
        form.insertRow(0, "기존 치아형", self.source)
        form.insertRow(1, "기본 형태", self.style)
        layout.addLayout(form)
        for title, callback in [("선택 FDI 치아의 변형 형태 등록", self.generate), ("기존 치아형에 배율·열구 적용", self.vary),
                                ("폴더의 외부 치아형 일괄 등록", self.import_folder)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.info = QtWidgets.QLabel("자체 매개변수 형태와 사용자가 보유한 STL·PLY·OBJ·VTP를 등록합니다. 등록한 형태는 보철 설계의 치아 형태 목록에서 선택할 수 있습니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def refresh(self):
        previous = self.source.currentData()
        self.source.clear()
        for key, obj in self.window.objects.items():
            if obj["role"] == "library":
                self.source.addItem(obj["name"], key)
        self.source.setCurrentIndex(max(0, self.source.findData(previous)))

    def generate(self):
        try:
            self.window.push_undo()
            key = self.window.create_library_tooth(select_result=False)
            self.refresh()
            self.source.setCurrentIndex(self.source.findData(key))
            self.vary()
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "치아 형태 생성", str(error))

    def vary(self):
        try:
            source = self.window.objects.get(self.source.currentData())
            if source is None:
                raise ValueError("치아형을 등록하거나 선택하세요.")
            mesh = vtk.vtkPolyData()
            mesh.DeepCopy(source["mesh"])
            points = vtk_to_numpy(mesh.GetPoints().GetData()).astype(float).copy()
            bounds = np.asarray(mesh.GetBounds()).reshape(3, 2)
            center = np.array([bounds[0].mean(), bounds[1].mean(), bounds[2, 0]])
            points -= center
            points *= [self.controls[name].value() for name in ["width", "depth", "height"]]
            rx, ry = max(np.ptp(points[:, 0]) / 2, .1), max(np.ptp(points[:, 1]) / 2, .1)
            height = max(np.ptp(points[:, 2]), .1)
            u, v = points[:, 0] / rx, points[:, 1] / ry
            weight = np.clip((points[:, 2] / height - .5) * 2, 0, 1)
            if self.style.currentIndex() == 1:
                points[:, 0] *= 1 + .08 * (1 - np.abs(v))
                points[:, 1] *= 1 + .08 * (1 - np.abs(u))
            elif self.style.currentIndex() == 2:
                points[:, 0] *= .9
                points[:, 2] *= 1.1
            if self.window.current_tooth % 10 >= 4:
                relief = sum(np.exp(-((u - a) ** 2 + (v - b) ** 2) / .09) for a, b in [(-.4, -.4), (.4, -.4), (-.4, .4), (.4, .4)])
                groove = np.exp(-np.square(u / .12)) + np.exp(-np.square(v / .12))
                points[:, 2] += weight * (self.controls["cusp"].value() * relief - self.controls["groove"].value() * groove)
            points += center
            mesh.GetPoints().SetData(numpy_to_vtk(points, deep=True))
            self.window.analyze_print_mesh(mesh, "치아형")
            self.window.push_undo()
            key = self.window.add_object(mesh, source["name"] + " · " + self.style.currentText(), "library", tooth=self.window.current_tooth, visible=False, library_origin="사용자 매개변수 변형")
            self.window.library_combo.setCurrentIndex(self.window.library_combo.findData(key))
            self.refresh()
            self.source.setCurrentIndex(self.source.findData(key))
            self.info.setText("치아형을 등록했습니다. 보철 설계와 폰틱·치아 배열에서 사용할 수 있습니다.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "치아 형태 편집", str(error))

    def import_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "치아형 폴더")
        if not folder:
            return
        try:
            from pathlib import Path
            paths = [path for path in Path(folder).iterdir() if path.suffix.lower() in {".stl", ".ply", ".obj", ".vtp"}]
            if not paths or len(paths) > 500:
                raise ValueError("1~500개의 모델 파일이 있는 폴더를 선택하세요.")
            staged = []
            for path in paths:
                reader = {".stl": vtk.vtkSTLReader, ".ply": vtk.vtkPLYReader, ".obj": vtk.vtkOBJReader, ".vtp": vtk.vtkXMLPolyDataReader}[path.suffix.lower()]()
                reader.SetFileName(str(path))
                reader.Update()
                triangle = vtk.vtkTriangleFilter()
                triangle.SetInputConnection(reader.GetOutputPort())
                triangle.Update()
                mesh = vtk.vtkPolyData()
                mesh.DeepCopy(triangle.GetOutput())
                if mesh.GetNumberOfPoints() == 0 or mesh.GetNumberOfPolys() == 0 or not np.isfinite(vtk_to_numpy(mesh.GetPoints().GetData())).all():
                    raise ValueError(path.name + "을 읽을 수 없습니다.")
                staged.append((path, mesh))
            self.window.push_undo()
            for path, mesh in staged:
                self.window.add_object(mesh, path.stem, "library", visible=False, library_origin=path.name)
            self.refresh()
            self.info.setText(f"외부 치아형 {len(staged)}개를 등록했습니다. 프로젝트 저장 시 메시가 함께 보관됩니다.")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "치아형 폴더", str(error))
