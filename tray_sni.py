# -*- coding: utf-8 -*-
"""
原生 StatusNotifierItem 托盘(Linux / KDE 等支持 SNI 的桌面)
============================================================
作者:ZJ

为什么要自己实现而不用 pystray:
  · pystray 的 AppIndicator 后端在 Linux 下左键只会弹菜单(HAS_DEFAULT_ACTION=False),
    无法像 Windows 那样"左键单击切换卡片";
  · pystray 的 GtkStatusIcon 后端依赖 xembedsniproxy 桥接,实测在 Plasma 6(Wayland)上
    左键 activate 转发不可靠。
  本模块直接把应用注册成桌面的 StatusNotifierItem:桌面在左键时调用 Activate、右键时调用
  ContextMenu,由应用自己接管,因此点击行为稳定、与 Windows 一致,且可被精确验证。

只依赖 PyGObject(gi)+ Pillow;若环境不支持 SNI(无 StatusNotifierWatcher),构造会抛异常,
调用方据此回退到 pystray。
"""
from __future__ import annotations

import threading

import gi
from gi.repository import Gio, GLib

# StatusNotifierItem 的 DBus 接口定义:只声明应用真正用到的属性/方法/信号
_SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg name="status" type="s" direction="out"/>
    </signal>
  </interface>
</node>
"""

_WATCHER_NAME = "org.kde.StatusNotifierWatcher"
_WATCHER_PATH = "/StatusNotifierWatcher"
_ITEM_PATH = "/StatusNotifierItem"


class SNITray:
    """一个最小但完整的原生 SNI 托盘项。

    构造参数:
      sid       —— 托盘项 Id(英文标识,如 "claude_quota")
      image     —— 初始图标(PIL.Image,会转成 SNI 的 ARGB32 像素)
      title     —— 标题 / 悬浮提示文本
      on_activate(x, y)        —— 左键单击回调(桌面调用 Activate 时触发)
      on_context(x, y)         —— 右键回调(桌面调用 ContextMenu 时触发,可在此弹菜单)
      on_secondary(x, y)       —— 中键回调(可选)
      on_scroll(delta, orient) —— 滚轮回调(可选)
    回调在 GLib 线程里被调用,内部应只做线程安全的轻量操作(如往队列里放命令)。
    """

    def __init__(self, sid, image, title,
                 on_activate, on_context=None, on_secondary=None, on_scroll=None):
        self.sid = sid
        self._title = title or sid
        self._on_activate = on_activate
        self._on_context = on_context
        self._on_secondary = on_secondary
        self._on_scroll = on_scroll

        self._pixmap = self._to_pixmap(image)
        self._loop = None
        self._conn = None
        self._reg_id = 0
        self._ready = threading.Event()
        self._error = None

    # ---- 图标转换:PIL.RGBA -> SNI 要求的 ARGB32(网络字节序、每像素 A,R,G,B)----
    @staticmethod
    def _to_pixmap(image):
        from PIL import Image
        img = image.convert("RGBA")
        w, h = img.size
        r, g, b, a = img.split()
        # 用 PIL 直接重排通道顺序为 A,R,G,B,避免逐像素 Python 循环
        argb = Image.merge("RGBA", (a, r, g, b)).tobytes("raw", "RGBA")
        return GLib.Variant("a(iiay)", [(w, h, argb)])

    # ---- 运行:在调用线程里跑 GLib 主循环(与 pystray.run 用法一致)----
    def run(self):
        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            node = Gio.DBusNodeInfo.new_for_xml(_SNI_XML)
            self._reg_id = self._conn.register_object(
                _ITEM_PATH, node.interfaces[0],
                self._on_method_call, self._on_get_property, None)
            # 向桌面的 StatusNotifierWatcher 注册自己(以本连接的唯一名作为服务标识)
            self._conn.call_sync(
                _WATCHER_NAME, _WATCHER_PATH, _WATCHER_NAME,
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self._conn.get_unique_name(),)),
                None, Gio.DBusCallFlags.NONE, 3000, None)
        except Exception as e:
            # 没有 SNI 宿主(如非 KDE 且无 AppIndicator 扩展)时在此失败,交由上层回退
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        self._loop = GLib.MainLoop()
        self._loop.run()

    def wait_ready(self, timeout=4.0):
        """等待注册完成;注册失败则抛出原始异常,供上层 try/except 回退到 pystray。"""
        self._ready.wait(timeout)
        if self._error is not None:
            raise self._error

    # ---- DBus 方法分发:把左/右/中键、滚轮交给对应回调 ----
    def _on_method_call(self, conn, sender, path, iface, method, params, invocation):
        try:
            if method == "Activate":
                x, y = params.unpack()
                self._safe(self._on_activate, x, y)
            elif method == "ContextMenu":
                x, y = params.unpack()
                self._safe(self._on_context, x, y)
            elif method == "SecondaryActivate":
                x, y = params.unpack()
                self._safe(self._on_secondary, x, y)
            elif method == "Scroll":
                delta, orient = params.unpack()
                self._safe(self._on_scroll, delta, orient)
        finally:
            invocation.return_value(None)  # 这些方法均无返回值

    @staticmethod
    def _safe(cb, *args):
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            # 托盘回调不应让 GLib 线程崩溃;吞掉异常但不静默到完全无痕(此处无日志设施)
            pass

    # ---- DBus 属性读取:桌面取图标/标题/状态等都走这里 ----
    def _on_get_property(self, conn, sender, path, iface, prop, *_):
        if prop == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if prop == "Id":
            return GLib.Variant("s", self.sid)
        if prop == "Title":
            return GLib.Variant("s", self._title)
        if prop == "Status":
            return GLib.Variant("s", "Active")
        if prop == "WindowId":
            return GLib.Variant("i", 0)
        if prop in ("IconName", "OverlayIconName", "AttentionIconName"):
            return GLib.Variant("s", "")
        if prop == "IconPixmap":
            return self._pixmap
        if prop == "AttentionIconPixmap":
            return GLib.Variant("a(iiay)", [])
        if prop == "ToolTip":
            # (icon_name, icon_pixmaps, title, description):标题给图标,描述留空
            return GLib.Variant("(sa(iiay)ss)", ("", [], self._title, ""))
        if prop == "ItemIsMenu":
            # 关键:声明本项不是"纯菜单",桌面才会在左键时调用 Activate 而非直接弹菜单
            return GLib.Variant("b", False)
        if prop == "Menu":
            # 不提供 DBusMenu;给一个占位对象路径,桌面取菜单失败后会回退到调用 ContextMenu
            return GLib.Variant("o", "/NO_DBUSMENU")
        return None

    # ---- 对外更新接口:用 .icon / .title 赋值即可(与 pystray 的属性写法兼容)----
    @property
    def icon(self):
        return None

    @icon.setter
    def icon(self, image):
        self._pixmap = self._to_pixmap(image)
        GLib.idle_add(self._emit, "NewIcon")

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, text):
        self._title = text or self.sid
        # 标题变化同时影响标题与悬浮提示,两个信号都发,桌面据此刷新
        GLib.idle_add(self._emit, "NewTitle")
        GLib.idle_add(self._emit, "NewToolTip")

    def _emit(self, signal_name):
        if self._conn is not None:
            try:
                self._conn.emit_signal(
                    None, _ITEM_PATH, "org.kde.StatusNotifierItem", signal_name, None)
            except Exception:
                pass
        return False  # idle 源只跑一次

    def stop(self):
        if self._loop is not None:
            GLib.idle_add(self._loop.quit)
