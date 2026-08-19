# -*- coding: utf-8 -*-


def classFactory(iface):
    from .plugin import EnexisKabelPlugin

    return EnexisKabelPlugin(iface)
