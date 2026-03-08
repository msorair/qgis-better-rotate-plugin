from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, Literal, Any

from qgis.core import (
    QgsCoordinateTransform,
    QgsProject,
    QgsGeometry,
    QgsPointXY,
    QgsWkbTypes,
    QgsRectangle,
    QgsVectorLayer,
    QgsFeature,
)
from qgis.gui import QgsMapTool, QgsVertexMarker, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor


RotationMode = Literal["group", "individual"]


@dataclass
class _Transforms:
    source_to_target: QgsCoordinateTransform
    canvas_to_target: QgsCoordinateTransform
    target_to_source: QgsCoordinateTransform


@dataclass
class _Cache:
    transforms_key: Optional[Tuple[int, str, str, str]] = None
    transforms: Optional[_Transforms] = None

    target_key: Optional[Tuple[int, str, Tuple[int, ...]]] = None
    geom_target_by_fid: Dict[int, QgsGeometry] = field(default_factory=dict)
    center_target_by_fid: Dict[int, QgsPointXY] = field(default_factory=dict)


class RotateMapTool(QgsMapTool):
    deactivated = pyqtSignal()

    @staticmethod
    def _normalize_angle_delta(angle_deg: float) -> float:
        # Map to (-180, 180]
        return (angle_deg + 180.0) % 360.0 - 180.0

    def __init__(self, plugin, canvas, iface):
        super().__init__(canvas)
        self.plugin = plugin
        self.iface = iface
        self.canvas = canvas

        self.layer: Optional[QgsVectorLayer] = None
        self._listening_layer_change = False
        self._selection_layer = None

        self.rotation_center: Optional[QgsPointXY] = None
        self.center_marker: Optional[QgsVertexMarker] = None

        self.is_rotating: bool = False
        self.start_point: Optional[QgsPointXY] = None
        self.features_to_rotate: list[QgsFeature] = []
        self.rubber_band: Optional[QgsRubberBand] = None
        self._rubber_geom_type = QgsWkbTypes.PolygonGeometry
        self.start_angle: float = 0.0
        self.current_angle_delta: float = 0.0

        self._cache = _Cache()

    def activate(self):
        super().activate()
        self._resetInteractionState(True)

        # update layer and selection
        lyr = self.iface.activeLayer()
        self.layer = lyr
        if lyr and lyr.selectedFeatureCount() > 0:
            self.features_to_rotate = list(lyr.getSelectedFeatures())
            self.calculateCenter()

        self._clearCaches()

        if not self._listening_layer_change:
            self.iface.currentLayerChanged.connect(self._onActiveLayerChanged)
            self._listening_layer_change = True

        self._connectSelectionChanged(self.layer)

    def deactivate(self):
        self.cleanup()
        self.deactivated.emit()
        super().deactivate()

    def cleanup(self):
        self._resetInteractionState(True)
        self.layer = None
        self._clearCaches()

        if self._listening_layer_change:
            try:
                self.iface.currentLayerChanged.disconnect(self._onActiveLayerChanged)
            except Exception:
                pass
            self._listening_layer_change = False

        self._disconnectSelectionChanged()

    def _onActiveLayerChanged(self, new_layer):
        # If the active layer changes during an interaction, cancel to avoid CRS/feature mismatches.
        if self.canvas.mapTool() is not self:
            return

        if not self.layer:
            self.layer = new_layer
            return

        if new_layer is self.layer:
            return

        if self.is_rotating or self.features_to_rotate:
            self.cancelOperation()
        self.layer = new_layer
        self._connectSelectionChanged(new_layer)

    def _disconnectSelectionChanged(self):
        if self._selection_layer is None:
            return

        try:
            self._selection_layer.selectionChanged.disconnect(self._onSelectionChanged)
        except Exception:
            pass
        self._selection_layer = None

    def _connectSelectionChanged(self, layer):
        if layer is self._selection_layer:
            return

        self._disconnectSelectionChanged()
        if layer is None:
            return

        try:
            layer.selectionChanged.connect(self._onSelectionChanged)
        except Exception:
            self._selection_layer = None
            return

        self._selection_layer = layer

    def _onSelectionChanged(self, *args):
        # Keep tool state consistent with layer selection.
        if self.canvas.mapTool() is not self:
            return

        layer = self._getLayer()
        if layer is None:
            return

        if self.is_rotating:
            self.cancelOperation()
            return

        self.features_to_rotate = list(layer.getSelectedFeatures())
        self.rotation_center = None
        self._clearCaches()

        if not self.features_to_rotate:
            if self.center_marker:
                self.canvas.scene().removeItem(self.center_marker)
                self.center_marker = None
            if self.rubber_band:
                self.rubber_band.reset(self._rubber_geom_type)
            self.plugin.updateAngleWidget(0.0)
            return

        self.calculateCenter()
        # Refresh preview if angle widget is non-zero
        angle = self.plugin.getRotationAngle()
        if abs(angle) > 1e-9:
            self.updatePreviewAngle(angle)

    def _clearCaches(self):
        self._cache = _Cache()

    def _resetInteractionState(self, clear_selection: bool):
        self.is_rotating = False
        self.start_point = None
        self.current_angle_delta = 0.0
        self.start_angle = 0.0

        if self.rubber_band:
            self.rubber_band.reset(self._rubber_geom_type)
            self.rubber_band = None

        if clear_selection:
            self.features_to_rotate = []
            self.rotation_center = None
            if self.center_marker:
                self.canvas.scene().removeItem(self.center_marker)
                self.center_marker = None

    def _getLayer(self):
        return self.layer or self.iface.activeLayer()

    def _getRotationMode(self) -> RotationMode:
        return self.plugin.getRotationMode()

    def _getTransforms(self, layer, target_crs):
        canvas_crs = self.canvas.mapSettings().destinationCrs()

        def crs_key(crs):
            return crs.authid() or str(crs.srsid()) or crs.toWkt()

        key = (
            id(layer),
            crs_key(layer.crs()),
            crs_key(canvas_crs),
            crs_key(target_crs),
        )
        if key == self._cache.transforms_key and self._cache.transforms is not None:
            return self._cache.transforms

        source_crs = layer.crs()
        source_to_target = QgsCoordinateTransform(
            source_crs, target_crs, QgsProject.instance()
        )
        canvas_to_target = QgsCoordinateTransform(
            canvas_crs, target_crs, QgsProject.instance()
        )
        target_to_source = QgsCoordinateTransform(
            target_crs, source_crs, QgsProject.instance()
        )

        self._cache.transforms_key = key
        self._cache.transforms = _Transforms(
            source_to_target=source_to_target,
            canvas_to_target=canvas_to_target,
            target_to_source=target_to_source,
        )
        return self._cache.transforms

    def _ensureTargetGeomCache(self, layer, target_crs):
        if not self.features_to_rotate:
            self._cache.target_key = None
            self._cache.geom_target_by_fid.clear()
            self._cache.center_target_by_fid.clear()
            return

        selection_sig = tuple(sorted(f.id() for f in self.features_to_rotate))
        key = (
            id(layer),
            target_crs.authid() or str(target_crs.srsid()) or target_crs.toWkt(),
            selection_sig,
        )
        if key == self._cache.target_key:
            return

        transforms = self._getTransforms(layer, target_crs)
        source_to_target = transforms.source_to_target

        geom_cache: Dict[int, QgsGeometry] = {}
        center_cache: Dict[int, QgsPointXY] = {}
        for feat in self.features_to_rotate:
            geom = feat.geometry()
            if not geom or geom.isNull():
                continue
            geom_t = QgsGeometry(geom)
            geom_t.transform(source_to_target)
            geom_cache[feat.id()] = geom_t

            c = geom_t.centroid()
            if c and not c.isNull():
                p = c.asPoint()
                center_cache[feat.id()] = QgsPointXY(p)

        self._cache.target_key = key
        self._cache.geom_target_by_fid = geom_cache
        self._cache.center_target_by_fid = center_cache

    def calculateCenter(self):
        if not self.features_to_rotate:
            return

        geometries = [
            f.geometry()
            for f in self.features_to_rotate
            if f.geometry() and not f.geometry().isNull()
        ]

        if not geometries:
            self.rotation_center = None
            return

        if len(geometries) == 1:
            rotation_center = geometries[0].centroid().asPoint()
        else:
            rotation_center = QgsGeometry.unaryUnion(geometries).centroid().asPoint()

        layer = self._getLayer()
        if not layer:
            return
        ct = QgsCoordinateTransform(
            layer.crs(),
            self.canvas.mapSettings().destinationCrs(),
            QgsProject.instance(),
        )
        self.rotation_center = ct.transform(rotation_center)
        self.updateCenterMarker(self.rotation_center)

    def updateCenterMarker(self, point):
        if not point:
            return
        if self.center_marker is None:
            self.center_marker = QgsVertexMarker(self.canvas)
            self.center_marker.setIconType(QgsVertexMarker.ICON_CROSS)
            self.center_marker.setColor(QColor(255, 0, 0))
            self.center_marker.setIconSize(15)
            self.center_marker.setPenWidth(3)
        self.center_marker.setCenter(point)

    def canvasReleaseEvent(self, e):
        # Right click to cancel
        if e.button() == Qt.RightButton:
            self.cancelOperation()
            return

        if e.button() != Qt.LeftButton:
            return

        point = e.mapPoint()

        # Ctrl+Click to set custom center
        if e.modifiers() & Qt.ControlModifier:
            if self.features_to_rotate:
                if self._getRotationMode() == "individual":
                    self.iface.messageBar().pushMessage(
                        "Individual mode: custom center ignored", level=0, duration=2
                    )
                    return
                self.rotation_center = point
                self.updateCenterMarker(point)
                self._clearCaches()
                self.iface.messageBar().pushMessage("Center Set", level=0, duration=1)
            return

        # If already rotating, confirm rotation
        if self.is_rotating:
            self.applyRotation()
            return

        # Start Rotation Logic
        # 1. Identify features if none selected
        if not self.features_to_rotate:
            lyr = self.iface.activeLayer()
            if not lyr:
                return

            self.layer = lyr
            # If user already has a selection, use it
            if lyr.selectedFeatureCount() > 0:
                self.features_to_rotate = list(lyr.getSelectedFeatures())
                self.calculateCenter()
            else:
                # Identify feature at point
                tolerance = self.canvas.mapUnitsPerPixel() * 5
                search_rect_canvas = QgsRectangle(
                    point.x() - tolerance,
                    point.y() - tolerance,
                    point.x() + tolerance,
                    point.y() + tolerance,
                )

                canvas_crs = self.canvas.mapSettings().destinationCrs()
                layer_crs = lyr.crs()
                ct = QgsCoordinateTransform(
                    canvas_crs, layer_crs, QgsProject.instance()
                )
                search_rect_layer = ct.transformBoundingBox(search_rect_canvas)

                req = lyr.getFeatures(search_rect_layer)
                found = next(req, None)
                if found:
                    self.features_to_rotate = [found]
                    self.calculateCenter()
                else:
                    self.iface.messageBar().pushMessage(
                        "No features found", level=0, duration=2
                    )
                    return

        # 2. Start rotating state
        if not self.rotation_center:
            self.calculateCenter()

        layer = self._getLayer()
        if layer:
            self._clearCaches()
            target_crs = self.plugin.getTargetCrs()
            self._ensureTargetGeomCache(layer, target_crs)

        self.is_rotating = True
        self.start_point = point

        dx = point.x() - self.rotation_center.x()
        dy = point.y() - self.rotation_center.y()
        self.start_angle = math.degrees(math.atan2(dy, dx))

        self.createRubberBand()
        self.plugin.updateAngleWidget(0.0)
        self.iface.messageBar().pushMessage(
            "Rotate mode active. Click to finish, Right Click to cancel.",
            level=0,
            duration=2,
        )

    def cancelOperation(self):
        self._resetInteractionState(True)
        self.plugin.updateAngleWidget(0.0)
        self._clearCaches()
        self.iface.messageBar().pushMessage("Canceled", level=0, duration=1)

        lyr = self.iface.activeLayer()
        if lyr and lyr.selectedFeatureCount() > 0:
            self.layer = lyr
            self.features_to_rotate = list(lyr.getSelectedFeatures())
            self.calculateCenter()

    def canvasMoveEvent(self, e):
        if not self.is_rotating or not self.rotation_center:
            return

        point = e.mapPoint()

        dx = point.x() - self.rotation_center.x()
        dy = point.y() - self.rotation_center.y()
        current_angle_abs = math.degrees(math.atan2(dy, dx))

        delta = self._normalize_angle_delta(current_angle_abs - self.start_angle)

        self.current_angle_delta = delta

        self.plugin.updateAngleWidget(delta)
        self.updateRubberBand(delta)

    def updatePreviewAngle(self, angle):
        self.current_angle_delta = angle
        if self.features_to_rotate and abs(angle) > 1e-9:
            if not self.rotation_center:
                self.calculateCenter()
            layer = self._getLayer()
            if layer and self.rotation_center:
                target_crs = self.plugin.getTargetCrs()
                self._ensureTargetGeomCache(layer, target_crs)
            self.createRubberBand()  # Ensure rubber band exists
            self.updateRubberBand(angle)
        elif self.rubber_band:
            # Clear preview for zero angle
            self.rubber_band.reset(self._rubber_geom_type)

    def applyRotationFromWidget(self):
        if self.features_to_rotate:
            self.applyRotation()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.plugin.deactivateTool()
            return

        if e.key() == Qt.Key_Return or e.key() == Qt.Key_Enter:
            if self.features_to_rotate:
                # Use value from widget in case it was typed manually
                angle = self.plugin.getRotationAngle()
                self.current_angle_delta = angle
                self.applyRotation()
            return

    def createRubberBand(self):
        if not self.rubber_band:
            layer = self._getLayer()
            self._rubber_geom_type = (
                QgsWkbTypes.geometryType(layer.wkbType())
                if layer is not None
                else QgsWkbTypes.PolygonGeometry
            )
            self.rubber_band = QgsRubberBand(self.canvas, self._rubber_geom_type)
            self.rubber_band.setColor(QColor(255, 0, 0, 100))
            self.rubber_band.setWidth(2)
            self.rubber_band.show()

    def _onRotationModeChanged(self):
        if self.is_rotating:
            self.cancelOperation()
            return

        self._clearCaches()
        if self.features_to_rotate:
            self.calculateCenter()
            self.updatePreviewAngle(self.plugin.getRotationAngle())

    def updateRubberBand(self, angle_deg):
        if not self.rubber_band:
            return

        target_crs = self.plugin.getTargetCrs()
        layer = self._getLayer()
        if not layer:
            return

        transforms = self._getTransforms(layer, target_crs)
        canvas_to_target = transforms.canvas_to_target
        target_to_source = transforms.target_to_source

        # Ensure geometry cache exists (fast preview)
        self._ensureTargetGeomCache(layer, target_crs)
        if not self._cache.geom_target_by_fid:
            return

        mode = self._getRotationMode()

        self.rubber_band.reset(self._rubber_geom_type)

        if mode == "group":
            center_target = canvas_to_target.transform(self.rotation_center)

            for feat in self.features_to_rotate:
                geom_target = self._cache.geom_target_by_fid.get(feat.id())
                if not geom_target:
                    continue

                geom = QgsGeometry(geom_target)
                geom.rotate(-angle_deg, center_target)
                geom.transform(target_to_source)
                self.rubber_band.addGeometry(geom, layer)
        else:
            # Rotate each feature around its own centroid in target CRS
            for feat in self.features_to_rotate:
                geom_target = self._cache.geom_target_by_fid.get(feat.id())
                center_target = self._cache.center_target_by_fid.get(feat.id())
                if not geom_target or center_target is None:
                    continue

                geom = QgsGeometry(geom_target)
                geom.rotate(-angle_deg, center_target)
                geom.transform(target_to_source)
                self.rubber_band.addGeometry(geom, layer)

    def applyRotation(self):
        lyr = self._getLayer()
        if not lyr or not lyr.isEditable():
            self.iface.messageBar().pushMessage("Layer not editable", level=2)
            return

        angle = self.current_angle_delta
        target_crs = self.plugin.getTargetCrs()

        transforms = self._getTransforms(lyr, target_crs)
        canvas_to_target = transforms.canvas_to_target
        target_to_source = transforms.target_to_source

        self._ensureTargetGeomCache(lyr, target_crs)
        if not self._cache.geom_target_by_fid:
            return

        mode = self._getRotationMode()

        center_group_target = (
            canvas_to_target.transform(self.rotation_center)
            if self.rotation_center is not None
            else None
        )

        feature_ids = [f.id() for f in self.features_to_rotate]

        lyr.beginEditCommand("Advanced Rotate")
        try:
            for feat in self.features_to_rotate:
                geom_target = self._cache.geom_target_by_fid.get(feat.id())
                if not geom_target:
                    continue

                if mode == "group":
                    if center_group_target is None:
                        continue
                    center_target = center_group_target
                else:
                    center_target = self._cache.center_target_by_fid.get(feat.id())
                    if center_target is None:
                        continue

                geom = QgsGeometry(geom_target)
                geom.rotate(-angle, center_target)
                geom.transform(target_to_source)
                lyr.changeGeometry(feat.id(), geom)

            lyr.endEditCommand()
            self.iface.messageBar().pushMessage(f"Rotated {angle:.2f}°", level=0)
        except Exception as e:
            lyr.destroyEditCommand()
            self.iface.messageBar().pushMessage(f"Error: {e}", level=2)
            return

        self.is_rotating = False
        self.current_angle_delta = 0.0
        if self.rubber_band:
            self.rubber_band.reset(self._rubber_geom_type)

        self._clearCaches()

        self.canvas.refresh()

        self.features_to_rotate = []
        for fid in feature_ids:
            f = lyr.getFeature(fid)
            if f.isValid():
                self.features_to_rotate.append(f)

        # Recalculate center based on new geometries
        if self.features_to_rotate:
            self.calculateCenter()
            self.plugin.updateAngleWidget(0.0)
