import numpy as np
import vtk
from PySide6 import QtCore, QtGui, QtWidgets
from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy


class DicomTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.volume_actor = None
        layout = QtWidgets.QVBoxLayout(self)
        button = QtWidgets.QPushButton("DICOM 시리즈 불러오기")
        button.clicked.connect(self.load)
        layout.addWidget(button)
        form = QtWidgets.QFormLayout()
        self.level = QtWidgets.QDoubleSpinBox()
        self.level.setRange(-32768, 65535)
        self.level.setValue(500)
        self.width = QtWidgets.QDoubleSpinBox()
        self.width.setRange(1, 100000)
        self.width.setValue(2000)
        self.low = QtWidgets.QDoubleSpinBox()
        self.low.setRange(-32768, 65535)
        self.low.setValue(300)
        self.high = QtWidgets.QDoubleSpinBox()
        self.high.setRange(-32768, 65535)
        self.high.setValue(3000)
        for title, widget in [("윈도 레벨", self.level), ("윈도 폭", self.width), ("분할 최저 강도", self.low), ("분할 최고 강도", self.high)]:
            form.addRow(title, widget)
        layout.addLayout(form)
        self.level.valueChanged.connect(self.render_slices)
        self.width.valueChanged.connect(self.render_slices)
        self.views = []
        self.sliders = []
        row = QtWidgets.QHBoxLayout()
        for title in ["시상면 X", "관상면 Y", "축상면 Z"]:
            column = QtWidgets.QVBoxLayout()
            column.addWidget(QtWidgets.QLabel(title))
            label = QtWidgets.QLabel()
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            label.setMinimumSize(160, 180)
            label.setStyleSheet("background: #10151d;")
            column.addWidget(label)
            slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            slider.setRange(0, 0)
            slider.valueChanged.connect(self.render_slices)
            column.addWidget(slider)
            self.views.append(label)
            self.sliders.append(slider)
            row.addLayout(column)
        layout.addLayout(row)
        self.largest = QtWidgets.QCheckBox("분할 후 가장 큰 연결 영역만 유지")
        self.largest.setChecked(True)
        layout.addWidget(self.largest)
        for title, callback in [("강도 범위로 3D 표면 분할", self.segment), ("CT 볼륨 미리보기", self.volume), ("현재 다면 보기 PNG 저장", self.save_image)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.info = QtWidgets.QLabel("DICOM의 복셀 간격·원점·방향을 유지합니다. 원본 영상은 프로젝트 파일에 포함되지 않습니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def load(self):
        self.window.import_dicom()
        self.refresh()

    def refresh(self):
        if self.window.ct_volume is None:
            return
        shape = self.window.ct_volume.shape
        for slider, size in zip(self.sliders, shape[::-1]):
            slider.blockSignals(True)
            slider.setRange(0, size - 1)
            slider.setValue(size // 2)
            slider.blockSignals(False)
        self.render_slices()

    def render_slices(self):
        if self.window.ct_volume is None:
            return
        values = self.window.ct_volume
        x, y, z = [slider.value() for slider in self.sliders]
        if x >= values.shape[2] or y >= values.shape[1] or z >= values.shape[0]:
            return
        slices = [values[:, :, x][::-1], values[:, y, :][::-1], values[z, :, :]]
        spacing = self.window.ct_metadata["spacing"]
        aspects = [(spacing[1], spacing[2]), (spacing[0], spacing[2]), (spacing[0], spacing[1])]
        low = self.level.value() - self.width.value() / 2
        for label, array, aspect in zip(self.views, slices, aspects):
            pixels = np.ascontiguousarray(np.clip((array - low) / self.width.value() * 255, 0, 255).astype(np.uint8))
            image = QtGui.QImage(pixels.data, pixels.shape[1], pixels.shape[0], pixels.strides[0], QtGui.QImage.Format.Format_Grayscale8).copy()
            ratio = pixels.shape[1] * aspect[0] / max(pixels.shape[0] * aspect[1], 1e-6)
            size = QtCore.QSize(max(1, int(220 * ratio)), 220).scaled(260, 240, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            label.setPixmap(QtGui.QPixmap.fromImage(image).scaled(size, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation))
        physical = np.asarray(self.window.ct_metadata["origin"]) + np.asarray(self.window.ct_metadata["direction"]).reshape(3, 3) @ (np.asarray([x, y, z]) * spacing)
        self.info.setText(f"복셀 X {x} / Y {y} / Z {z} · LPS {physical[0]:.2f}, {physical[1]:.2f}, {physical[2]:.2f} mm")

    def segment(self):
        try:
            if self.window.ct_volume is None:
                raise ValueError("DICOM 시리즈를 먼저 불러오세요.")
            if self.low.value() >= self.high.value():
                raise ValueError("최저 강도는 최고 강도보다 작아야 합니다.")
            values = self.window.ct_volume
            mask = np.pad(((values >= self.low.value()) & (values <= self.high.value())).astype(np.float32), 1)
            image = vtk.vtkImageData()
            image.SetDimensions(*mask.shape[::-1])
            image.SetSpacing(*self.window.ct_metadata["spacing"])
            image.SetOrigin(*(-np.asarray(self.window.ct_metadata["spacing"])))
            image.GetPointData().SetScalars(numpy_to_vtk(mask.ravel(), deep=True))
            contour = vtk.vtkFlyingEdges3D()
            contour.SetInputData(image)
            contour.SetValue(0, .5)
            contour.Update()
            mesh = contour.GetOutput()
            if self.largest.isChecked():
                connectivity = vtk.vtkPolyDataConnectivityFilter()
                connectivity.SetInputData(mesh)
                connectivity.SetExtractionModeToLargestRegion()
                connectivity.Update()
                mesh = connectivity.GetOutput()
            if mesh.GetNumberOfPoints() == 0:
                raise ValueError("선택 범위에서 분할할 표면을 찾지 못했습니다.")
            output = vtk.vtkPolyData()
            output.DeepCopy(mesh)
            metadata = self.window.ct_metadata
            direction = np.asarray(metadata["direction"]).reshape(3, 3)
            coordinates = vtk_to_numpy(output.GetPoints().GetData()).astype(float) @ direction.T + np.asarray(metadata["origin"])
            output.GetPoints().SetData(numpy_to_vtk(coordinates, deep=True))
            if np.linalg.det(direction) < 0:
                reverse = vtk.vtkReverseSense()
                reverse.SetInputData(output)
                reverse.ReverseCellsOn()
                reverse.Update()
                output.DeepCopy(reverse.GetOutput())
            self.window.push_undo()
            self.window.add_object(output, f"CT 범위 분할 · {self.low.value():g}~{self.high.value():g}", "segmentation", dicom_geometry=metadata.copy(), threshold_range=[self.low.value(), self.high.value()])
            self.window.camera_view("fit")
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "DICOM 분할", str(error))

    def volume(self):
        try:
            if self.window.ct_volume is None:
                raise ValueError("DICOM 시리즈를 먼저 불러오세요.")
            self.deactivate()
            values = self.window.ct_volume
            image = vtk.vtkImageData()
            image.SetDimensions(*values.shape[::-1])
            image.SetSpacing(*self.window.ct_metadata["spacing"])
            image.SetOrigin(*self.window.ct_metadata["origin"])
            image.SetDirectionMatrix(self.window.ct_metadata["direction"])
            image.GetPointData().SetScalars(numpy_to_vtk(np.ascontiguousarray(values).ravel(), deep=True))
            mapper = vtk.vtkSmartVolumeMapper()
            mapper.SetInputData(image)
            color = vtk.vtkColorTransferFunction()
            color.AddRGBPoint(self.low.value(), .35, .20, .15)
            color.AddRGBPoint(self.high.value(), .95, .90, .77)
            opacity = vtk.vtkPiecewiseFunction()
            opacity.AddPoint(self.low.value() - 1, 0)
            opacity.AddPoint(self.low.value(), .03)
            opacity.AddPoint(self.high.value(), .7)
            properties = vtk.vtkVolumeProperty()
            properties.SetColor(color)
            properties.SetScalarOpacity(opacity)
            properties.SetInterpolationTypeToLinear()
            properties.ShadeOn()
            actor = vtk.vtkVolume()
            actor.SetMapper(mapper)
            actor.SetProperty(properties)
            self.volume_actor = actor
            self.window.renderer.AddVolume(actor)
            self.window.renderer.ResetCamera()
            self.window.vtk_widget.GetRenderWindow().Render()
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, "DICOM 볼륨", str(error))

    def save_image(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "다면 보기 저장", "dicom-views.png", "PNG (*.png)")
        if path and not self.grab().save(path, "PNG"):
            QtWidgets.QMessageBox.warning(self, "DICOM 보기 저장", "이미지를 저장할 수 없습니다.")

    def deactivate(self):
        if self.volume_actor:
            self.window.renderer.RemoveVolume(self.volume_actor)
            self.volume_actor = None
            self.window.vtk_widget.GetRenderWindow().Render()
