def classFactory(iface):
    from .rotate_plugin import RotatePlugin
    return RotatePlugin(iface)
