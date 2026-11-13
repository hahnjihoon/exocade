from PySide6 import QtWidgets
import vtk


class AppearanceTools(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.target = QtWidgets.QComboBox()
        self.preset = QtWidgets.QComboBox()
        self.preset.addItems(["치아", "치은", "레진", "금속"])
        self.roughness = QtWidgets.QDoubleSpinBox()
        self.roughness.setRange(.05, 1)
        self.roughness.setValue(.3)
        self.opacity = QtWidgets.QDoubleSpinBox()
        self.opacity.setRange(.05, 1)
        self.opacity.setValue(1)
        self.scale = QtWidgets.QSpinBox()
        self.scale.setRange(1, 4)
        self.scale.setValue(2)
        for title, widget in [("대상", self.target), ("표면 재질", self.preset), ("거칠기", self.roughness),
                              ("불투명도", self.opacity), ("이미지 해상도 배율", self.scale)]:
            form.addRow(title, widget)
        layout.addLayout(form)
        for title, callback in [("재질 적용", self.apply), ("현재 3D 화면 PNG 저장", self.save_image)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            layout.addWidget(button)
        layout.addWidget(QtWidgets.QLabel("PBR 재질과 현재 조명·카메라로 렌더링합니다."))
        layout.addStretch()

    def refresh(self):
        previous = self.target.currentData()
        self.target.clear()
        for key, obj in self.window.objects.items():
            self.target.addItem(obj["name"], key)
        self.target.setCurrentIndex(max(0, self.target.findData(previous or self.window.active_id)))

    def apply(self):
        obj = self.window.objects.get(self.target.currentData())
        if obj is None:
            return
        presets = [((.96, .87, .70), 0.), ((.82, .37, .43), 0.), ((.70, .82, .89), 0.), ((.74, .77, .81), .85)]
        color, metallic = presets[self.preset.currentIndex()]
        self.window.push_undo()
        properties = obj["actor"].GetProperty()
        properties.SetInterpolationToPBR()
        properties.SetColor(*color)
        properties.SetMetallic(metallic)
        properties.SetRoughness(self.roughness.value())
        properties.SetOpacity(self.opacity.value())
        obj["actor"].GetMapper().ScalarVisibilityOff()
        obj["color"] = color
        obj["appearance"] = {"metallic": metallic, "roughness": self.roughness.value()}
        self.window.dirty = True
        self.window.vtk_widget.GetRenderWindow().Render()

    def save_image(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "3D 렌더 저장", "render.png", "PNG (*.png)")
        if not path:
            return
        capture = vtk.vtkWindowToImageFilter()
        capture.SetInput(self.window.vtk_widget.GetRenderWindow())
        capture.SetScale(self.scale.value())
        capture.ReadFrontBufferOff()
        capture.Update()
        writer = vtk.vtkPNGWriter()
        writer.SetFileName(path)
        writer.SetInputConnection(capture.GetOutputPort())
        writer.Write()
        if writer.GetErrorCode():
            QtWidgets.QMessageBox.warning(self, "3D 렌더 저장", "이미지를 저장할 수 없습니다.")
