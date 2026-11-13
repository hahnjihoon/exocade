from PySide6 import QtWidgets

from .appearance import AppearanceTools
from .articulator import ArticulatorTools
from .attachments import AttachmentTools
from .bars import BarTools
from .blockout import BlockoutTools
from .case_manager import CaseManager
from .contacts import ContactTools
from .dentures import DentureTools
from .dicom_tools import DicomTools
from .materials import MaterialLibrary
from .model_tools import ModelTools
from .output_tools import OutputTools
from .restorations import RestorationTools
from .scan_tools import ScanTools
from .smile_design import SmileDesign
from .splints import SplintTools
from .tooth_library import ToothLibrary


class FeatureWorkbench(QtWidgets.QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.setWindowTitle("EXOCADE · 확장 기능")
        self.resize(1200, 860)
        self.setMinimumSize(960, 650)
        self.previous_index = -1
        layout = QtWidgets.QHBoxLayout(self)
        self.navigation = QtWidgets.QListWidget()
        self.navigation.setFixedWidth(190)
        self.pages = QtWidgets.QStackedWidget()
        self.widgets = []
        for title, widget_type in [("케이스·환자·의뢰 관리", CaseManager), ("스캔·복사·정합·마진", ScanTools),
                                   ("치아 형태 라이브러리", ToothLibrary), ("사용자 재료 라이브러리", MaterialLibrary),
                                   ("보철·컷백·임시치아", RestorationTools), ("어태치먼트·포스트", AttachmentTools),
                                   ("접촉·폰틱·브릿지", ContactTools), ("언더컷 블록아웃", BlockoutTools),
                                   ("바 단면·유지공", BarTools),
                                   ("모델·다이·표기", ModelTools), ("총의치·부분의치", DentureTools),
                                   ("스플린트·트레이 편집", SplintTools), ("가상 교합·턱 운동", ArticulatorTools),
                                   ("DICOM 다면·볼륨", DicomTools), ("사진 기반 스마일 설계", SmileDesign),
                                   ("재질·렌더링", AppearanceTools), ("출력 포함·검사 결과", OutputTools)]:
            widget = widget_type(window)
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(widget)
            self.navigation.addItem(title)
            self.pages.addWidget(scroll)
            self.widgets.append(widget)
        layout.addWidget(self.navigation)
        layout.addWidget(self.pages, 1)
        self.navigation.currentRowChanged.connect(self.activate)
        self.navigation.setCurrentRow(0)

    def activate(self, index):
        if index < 0:
            return
        if self.previous_index >= 0:
            widget = self.widgets[self.previous_index]
            if hasattr(widget, "deactivate"):
                widget.deactivate()
        self.pages.setCurrentIndex(index)
        self.widgets[index].refresh()
        self.previous_index = index

    def deactivate_all(self):
        for widget in self.widgets:
            if hasattr(widget, "deactivate"):
                widget.deactivate()

    def showEvent(self, event):
        super().showEvent(event)
        self.activate(self.navigation.currentRow())

    def closeEvent(self, event):
        self.deactivate_all()
        super().closeEvent(event)

    def hideEvent(self, event):
        self.deactivate_all()
        super().hideEvent(event)
