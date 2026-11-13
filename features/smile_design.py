import numpy as np
import vtk
from PySide6 import QtCore, QtGui, QtWidgets
from vtkmodules.util.numpy_support import vtk_to_numpy


class SmileView(QtWidgets.QGraphicsView):
    point_picked = QtCore.Signal(QtCore.QPointF)

    def __init__(self, scene):
        super().__init__(scene)
        self.picking = False
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.RubberBandDrag)

    def mousePressEvent(self, event):
        if self.picking and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.point_picked.emit(self.mapToScene(event.position().toPoint()))
            event.accept()
            return
        super().mousePressEvent(event)


class SmileDesign(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.photo = None
        self.overlay = None
        self.guides = []
        self.photo_points = []
        self.tooth_points = []
        self.pick_kind = None
        layout = QtWidgets.QVBoxLayout(self)
        self.scene = QtWidgets.QGraphicsScene(self)
        self.view = SmileView(self.scene)
        self.view.setMinimumSize(450, 320)
        self.view.point_picked.connect(self.pick_point)
        layout.addWidget(self.view, 1)
        row = QtWidgets.QHBoxLayout()
        for title, callback in [("얼굴 사진", self.load_photo), ("현재 보철 화면 가져오기", self.capture),
                                ("사진 기준점 2개", lambda: self.start_pick("photo")), ("치아 기준점 2개", lambda: self.start_pick("tooth")),
                                ("기준점으로 정렬", self.align), ("PNG 저장", self.save_image)]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        form = QtWidgets.QFormLayout()
        self.opacity = QtWidgets.QDoubleSpinBox()
        self.opacity.setRange(.05, 1)
        self.opacity.setValue(.9)
        self.opacity.valueChanged.connect(self.change_overlay)
        self.scale = QtWidgets.QDoubleSpinBox()
        self.scale.setRange(.001, 1000)
        self.scale.setDecimals(4)
        self.scale.setValue(1)
        self.scale.valueChanged.connect(self.change_overlay)
        self.rotation = QtWidgets.QDoubleSpinBox()
        self.rotation.setRange(-180, 180)
        self.rotation.valueChanged.connect(self.change_overlay)
        for title, widget in [("치아 불투명도", self.opacity), ("치아 배율", self.scale), ("치아 회전", self.rotation)]:
            form.addRow(title, widget)
        layout.addLayout(form)
        self.export_guides = QtWidgets.QCheckBox("PNG에 중앙선·교합선 포함")
        layout.addWidget(self.export_guides)
        self.info = QtWidgets.QLabel("3D 정면 카메라에서 보철을 가져온 뒤 사진과 치아의 대응점 두 개를 같은 순서로 선택하세요. 치아와 중앙선·교합선은 드래그할 수 있습니다. 2D 심미 합성이며 3D 얼굴 정합은 수행하지 않습니다.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

    def refresh(self):
        if self.photo is None:
            self.view.fitInView(self.scene.itemsBoundingRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def load_photo(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "얼굴 사진", "", "이미지 (*.png *.jpg *.jpeg *.bmp)")
        if not path:
            return
        pixmap = QtGui.QPixmap(path)
        if pixmap.isNull():
            QtWidgets.QMessageBox.warning(self, "얼굴 사진", "이미지를 읽을 수 없습니다.")
            return
        if self.photo:
            self.scene.removeItem(self.photo)
        for guide in self.guides:
            self.scene.removeItem(guide)
        self.guides.clear()
        self.photo = self.scene.addPixmap(pixmap)
        self.photo.setZValue(-10)
        self.scene.setSceneRect(self.photo.boundingRect())
        for line in [QtCore.QLineF(pixmap.width() / 2, 0, pixmap.width() / 2, pixmap.height()),
                     QtCore.QLineF(0, pixmap.height() * .65, pixmap.width(), pixmap.height() * .65)]:
            guide = self.scene.addLine(line, QtGui.QPen(QtGui.QColor("#36d7be"), max(1., pixmap.width() / 1200)))
            guide.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
            guide.setZValue(10)
            self.guides.append(guide)
        self.view.fitInView(self.photo, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.photo_points.clear()

    def capture(self):
        render_window = self.window.vtk_widget.GetRenderWindow()
        visibility = {key: obj["actor"].GetVisibility() for key, obj in self.window.objects.items()}
        alpha = self.window.renderer.GetBackgroundAlpha()
        extra = []
        try:
            for key, obj in self.window.objects.items():
                obj["actor"].SetVisibility(visibility[key] and obj["role"] in {"restoration", "pontic", "denture", "gingiva", "part"} and obj.get("export_enabled", True))
            for actor in [self.window.margin_actor, self.window.path_actor, self.window.bed_actor, self.window.scalar_bar, *self.window.measure_actors]:
                if actor is not None:
                    extra.append((actor, actor.GetVisibility()))
                    actor.SetVisibility(False)
            self.window.renderer.SetBackgroundAlpha(0)
            render_window.SetAlphaBitPlanes(1)
            render_window.Render()
            capture = vtk.vtkWindowToImageFilter()
            capture.SetInput(render_window)
            capture.SetInputBufferTypeToRGBA()
            capture.ReadFrontBufferOff()
            capture.Update()
            image = capture.GetOutput()
            width, height, depth = image.GetDimensions()
            pixels = np.ascontiguousarray(vtk_to_numpy(image.GetPointData().GetScalars()).reshape(height, width, 4)[::-1])
            result = QtGui.QImage(pixels.data, width, height, pixels.strides[0], QtGui.QImage.Format.Format_RGBA8888).copy()
            if self.overlay:
                self.scene.removeItem(self.overlay)
            self.overlay = self.scene.addPixmap(QtGui.QPixmap.fromImage(result))
            self.overlay.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
            self.overlay.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
            self.overlay.setZValue(1)
            self.change_overlay()
            self.tooth_points.clear()
            if not self.photo:
                self.view.fitInView(self.overlay, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        finally:
            for key, visible in visibility.items():
                if key in self.window.objects:
                    self.window.objects[key]["actor"].SetVisibility(visible)
            for actor, visible in extra:
                actor.SetVisibility(visible)
            self.window.renderer.SetBackgroundAlpha(alpha)
            render_window.Render()

    def start_pick(self, kind):
        if self.photo is None or self.overlay is None:
            self.info.setText("얼굴 사진과 보철 화면을 먼저 가져오세요.")
            return
        self.pick_kind = kind
        self.photo_points.clear() if kind == "photo" else self.tooth_points.clear()
        self.view.picking = True
        self.info.setText(("사진" if kind == "photo" else "치아") + "의 대응점 두 개를 순서대로 클릭하세요.")

    def pick_point(self, point):
        points = self.photo_points if self.pick_kind == "photo" else self.tooth_points
        points.append(point if self.pick_kind == "photo" else self.overlay.mapFromScene(point))
        if len(points) == 2:
            self.view.picking = False
            self.info.setText("기준점 두 개를 선택했습니다.")

    def align(self):
        if len(self.photo_points) != 2 or len(self.tooth_points) != 2 or self.overlay is None:
            self.info.setText("사진과 치아의 기준점을 각각 두 개 선택하세요.")
            return
        source = np.array([[point.x(), point.y()] for point in self.tooth_points])
        target = np.array([[point.x(), point.y()] for point in self.photo_points])
        a, b = source[1] - source[0], target[1] - target[0]
        if np.linalg.norm(a) < 1e-6 or np.linalg.norm(b) < 1e-6:
            self.info.setText("서로 떨어진 기준점을 선택하세요.")
            return
        self.scale.setValue(float(np.linalg.norm(b) / np.linalg.norm(a)))
        angle = np.arctan2(b[1], b[0]) - np.arctan2(a[1], a[0])
        self.rotation.setValue(float(np.degrees(np.arctan2(np.sin(angle), np.cos(angle)))))
        transformed = self.overlay.mapToScene(self.tooth_points[0])
        self.overlay.setPos(self.overlay.pos() + self.photo_points[0] - transformed)
        self.info.setText("두 대응점으로 배율·회전·위치를 정렬했습니다.")

    def change_overlay(self):
        if self.overlay:
            self.overlay.setScale(self.scale.value())
            self.overlay.setRotation(self.rotation.value())
            self.overlay.setOpacity(self.opacity.value())

    def save_image(self):
        if self.photo is None:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "스마일 합성 저장", "smile.png", "PNG (*.png)")
        if not path:
            return
        rect = self.photo.boundingRect()
        image = QtGui.QImage(rect.size().toSize(), QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        selected = self.scene.selectedItems()
        self.scene.clearSelection()
        for guide in self.guides:
            guide.setVisible(self.export_guides.isChecked())
        painter = QtGui.QPainter(image)
        self.scene.render(painter, QtCore.QRectF(image.rect()), rect)
        painter.end()
        for guide in self.guides:
            guide.setVisible(True)
        for item in selected:
            item.setSelected(True)
        if not image.save(path, "PNG"):
            QtWidgets.QMessageBox.warning(self, "스마일 합성", "이미지를 저장할 수 없습니다.")
