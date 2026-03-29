from __future__ import annotations

import os
from typing import Optional, Literal

from qgis.PyQt.QtWidgets import (
    QAction,
    QWidget,
    QHBoxLayout,
    QLabel,
    QDoubleSpinBox,
    QComboBox,
)
from qgis.PyQt.QtGui import QIcon
from qgis.gui import QgsProjectionSelectionWidget
from qgis.core import QgsProject
from .rotate_tool import RotateMapTool


RotationMode = Literal["group", "individual"]


class RotatePlugin:
    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()

        self.action: Optional[QAction] = None
        self.tool: Optional[RotateMapTool] = None
        self.toolbar = self.iface.addToolBar("BetterRotateToolbar")
        self.toolbar.setObjectName("BetterRotateToolbar")

        self.input_widget: Optional[QWidget] = None
        self.crs_widget: Optional[QgsProjectionSelectionWidget] = None
        self.angle_input: Optional[QDoubleSpinBox] = None
        self.mode_combo: Optional[QComboBox] = None

    def initGui(self):
        icon_path = os.path.join(os.path.dirname(__file__), "better_rotate.svg")
        self.action = QAction(
            QIcon(icon_path), "Better Rotate", self.iface.mainWindow()
        )
        self.action.setStatusTip("Rotate features with custom coordinate system")
        self.action.setToolTip("Click to select/rotate. Ctrl+Click to set center. ")
        self.action.setCheckable(True)
        self.action.triggered.connect(self.activateTool)

        self.toolbar.addAction(self.action)

        self.tool = RotateMapTool(self, self.canvas, self.iface)
        self.tool.deactivated.connect(self.onToolDeactivated)

        self.iface.currentLayerChanged.connect(self.onCurrentLayerChanged)
        self.onCurrentLayerChanged(self.iface.activeLayer())

    @staticmethod
    def _disconnect_helper(signal, slot) -> None:
        try:
            signal.disconnect(slot)
        except Exception:
            pass

    def onCurrentLayerChanged(self, layer) -> None:
        self.updateActionStatus()
        if not layer:
            return

        self._disconnect_helper(layer.editingStarted, self.updateActionStatus)
        self._disconnect_helper(layer.editingStopped, self.updateActionStatus)
        layer.editingStarted.connect(self.updateActionStatus)
        layer.editingStopped.connect(self.updateActionStatus)

    def updateActionStatus(self) -> None:
        layer = self.iface.activeLayer()
        enabled = layer is not None and layer.isEditable()
        if self.action is None:
            return
        if self.action:
            self.action.setEnabled(enabled)
        if not enabled and self.action.isChecked():
            self.action.setChecked(False)
            self.deactivateTool()

    def activateTool(self):
        if not self.action or not self.tool:
            return

        if self.action.isChecked():
            self._createInputWidget()
            self.canvas.setMapTool(self.tool)
        else:
            self.deactivateTool()

    def _createInputWidget(self):
        self._disposeInputWidget()

        self.input_widget = QWidget()
        layout = QHBoxLayout()
        layout.setContentsMargins(5, 0, 5, 0)

        # Target CRS
        crs_label = QLabel("Target CRS:")
        self.crs_widget = QgsProjectionSelectionWidget()
        self.crs_widget.setOptionVisible(QgsProjectionSelectionWidget.CrsOption.LayerCrs, True)
        self.crs_widget.setOptionVisible(QgsProjectionSelectionWidget.CrsOption.ProjectCrs, True)
        self.crs_widget.setMinimumWidth(250)

        # Rotation mode
        mode_label = QLabel("Mode:")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Group center", "group")
        self.mode_combo.addItem("Individual centers", "individual")
        self.mode_combo.setToolTip(
            "Rotate all features around one center, or each feature around its own centroid"
        )
        self.mode_combo.currentIndexChanged.connect(self.onModeChanged)

        # Rotation angle
        angle_label = QLabel("Angle (°):")
        self.angle_input = QDoubleSpinBox()
        self.angle_input.setRange(-360, 360)
        self.angle_input.setValue(0)
        self.angle_input.setDecimals(2)
        self.angle_input.setSingleStep(1)
        self.angle_input.setMinimumWidth(100)
        self.angle_input.valueChanged.connect(self.onAngleValueChanged)
        self.angle_input.lineEdit().returnPressed.connect(self.onAngleEnter)

        layout.addWidget(crs_label)
        layout.addWidget(self.crs_widget)
        layout.addWidget(mode_label)
        layout.addWidget(self.mode_combo)
        layout.addWidget(angle_label)
        layout.addWidget(self.angle_input)
        layout.addStretch()

        self.input_widget.setLayout(layout)
        self.iface.addUserInputWidget(self.input_widget)

        self.crs_widget.setCrs(QgsProject.instance().crs())

    def _disposeInputWidget(self):
        if self.input_widget:
            self.input_widget.releaseKeyboard()
            self.input_widget.deleteLater()

        self.input_widget = None
        self.crs_widget = None
        self.angle_input = None
        self.mode_combo = None

    def onToolDeactivated(self):
        if self.action and self.action.isChecked():
            self.action.setChecked(False)

        self._disposeInputWidget()

    def deactivateTool(self):
        if self.canvas.mapTool() == self.tool:
            self.canvas.unsetMapTool(self.tool)

    def getTargetCrs(self):
        if not self.crs_widget:
            return QgsProject.instance().crs()
        return self.crs_widget.crs()

    def getRotationMode(self) -> RotationMode:
        if not self.mode_combo:
            return "group"
        return self.mode_combo.currentData() or "group"

    def getRotationAngle(self):
        if not self.angle_input:
            return 0.0
        return float(self.angle_input.value())

    def updateAngleWidget(self, angle):
        if not self.angle_input:
            return
        self.angle_input.blockSignals(True)
        self.angle_input.setValue(angle)
        self.angle_input.blockSignals(False)

    def onAngleValueChanged(self, value: float) -> None:
        if self.tool and self.canvas.mapTool() == self.tool:
            self.tool.updatePreviewAngle(value)

    def onAngleEnter(self) -> None:
        if self.tool and self.canvas.mapTool() == self.tool:
            self.tool.applyRotationFromWidget()

    def onModeChanged(self, idx: int) -> None:
        if self.tool and self.canvas.mapTool() == self.tool:
            self.tool._onRotationModeChanged()

    def unload(self):
        try:
            self.iface.currentLayerChanged.disconnect(self.onCurrentLayerChanged)
        except Exception:
            pass

        if self.action:
            self.iface.removeToolBarIcon(self.action)
            self.action = None

        self._disposeInputWidget()

        # Clean up tool
        if self.tool:
            if self.canvas.mapTool() == self.tool:
                self.canvas.unsetMapTool(self.tool)
            self.tool = None

        del self.toolbar
