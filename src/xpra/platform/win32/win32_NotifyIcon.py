#!/usr/bin/env python
# This file is part of Xpra.
# Copyright (C) 2011-2013 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

# Low level support for the "system tray" on MS Windows
# Based on code from winswitch, itself based on "win32gui_taskbar demo"

import win32api                    #@UnresolvedImport
import win32gui                    #@UnresolvedImport
import win32con                    #@UnresolvedImport

import sys, os

from xpra.log import Logger, debug_if_env
log = Logger()
debug = debug_if_env(log, "XPRA_TRAY_DEBUG")


BUTTON_MAP = {
            win32con.WM_LBUTTONDOWN     : (1, 1),
            win32con.WM_LBUTTONUP       : (1, 0),
            win32con.WM_MBUTTONDOWN     : (2, 1),
            win32con.WM_MBUTTONUP       : (2, 0),
            win32con.WM_RBUTTONDOWN     : (3, 1),
            win32con.WM_RBUTTONUP       : (3, 0)
            }


class win32NotifyIcon(object):

    click_callbacks = {}
    exit_callbacks = {}
    command_callbacks = {}
    live_hwnds = set()

    def __init__(self, title, click_callback, exit_callback, command_callback=None, iconPathName=None):
        self.title = title[:127]
        self.current_icon = None
        # Register the Window class.
        self.hinst = NIwc.hInstance
        # Create the Window.
        style = win32con.WS_OVERLAPPED | win32con.WS_SYSMENU
        self.hwnd = win32gui.CreateWindow(NIclassAtom, self.title+" StatusIcon Window", style, \
            0, 0, win32con.CW_USEDEFAULT, win32con.CW_USEDEFAULT, \
            0, 0, self.hinst, None)
        win32gui.UpdateWindow(self.hwnd)
        self.current_icon = self.win32LoadIcon(iconPathName)
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, self.make_nid(win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP))
        #register callbacks:
        win32NotifyIcon.live_hwnds.add(self.hwnd)
        win32NotifyIcon.click_callbacks[self.hwnd] = click_callback
        win32NotifyIcon.exit_callbacks[self.hwnd] = exit_callback
        win32NotifyIcon.command_callbacks[self.hwnd] = command_callback
        

    def make_nid(self, flags):
        return (self.hwnd, 0, flags, WM_TRAY_EVENT, self.current_icon, self.title)

    def set_blinking(self, on):
        #FIXME: implement blinking on win32 using a timer
        pass

    def set_tooltip(self, name):
        self.title = name[:127]
        win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, self.make_nid(win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP))

    def set_icon(self, iconPathName):
        self.current_icon = self.win32LoadIcon(iconPathName)
        win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, self.make_nid(win32gui.NIF_ICON))

    def win32LoadIcon(self, iconPathName):
        icon_flags = win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE
        try:
            return    win32gui.LoadImage(self.hinst, iconPathName, win32con.IMAGE_ICON, 0, 0, icon_flags)
        except Exception, e:
            log.error("Failed to load icon at %s: %s", iconPathName, e)
            return    win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    @classmethod
    def remove_callbacks(cls, hwnd):
        for x in (cls.command_callbacks, cls.exit_callbacks, cls.click_callbacks):
            if hwnd in x:
                del x[hwnd]

    @classmethod
    def OnCommand(cls, hwnd, msg, wparam, lparam):
        cc = cls.command_callbacks.get(hwnd)
        log("OnCommand(%s,%s,%s,%s) command callback=%s", hwnd, msg, wparam, lparam, cc)
        if cc:
            cid = win32api.LOWORD(wparam)
            cc(hwnd, cid)

    @classmethod
    def OnDestroy(cls, hwnd, msg, wparam, lparam):
        ec = cls.exit_callbacks.get(hwnd)
        log("OnDestroy(%s,%s,%s,%s) exit_callback=%s", hwnd, msg, wparam, lparam, ec)
        if hwnd not in cls.live_hwnds:
            return
        cls.live_hwnds.remove(hwnd)
        cls.remove_callbacks(hwnd)
        try:
            nid = (hwnd, 0)
            log("OnDestroy(..) calling Shell_NotifyIcon(NIM_DELETE, %s)", nid)
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, nid)
            log("OnDestroy(..) calling exit_callback=%s", ec)
            if ec:
                ec()
        except:
            log.error("OnDestroy(..)", exc_info=True)

    @classmethod
    def OnTaskbarNotify(cls, hwnd, msg, wparam, lparam):
        bm = BUTTON_MAP.get(lparam)
        cc = cls.click_callbacks.get(hwnd)
        log("OnTaskbarNotify(%s,%s,%s,%s) button lookup: %s, callback=%s", hwnd, msg, wparam, lparam, bm, cc)
        if bm is not None and cc:
            cc(*bm)
        return 1

    def close(self):
        log("win32NotifyIcon.close()")
        win32NotifyIcon.remove_callbacks(self.hwnd)
        self.OnDestroy(0, None, None, None)

    def get_geometry(self):
        return    win32gui.GetWindowRect(self.hwnd)


WM_TRAY_EVENT = win32con.WM_USER+20        #a message id we choose
message_map = {
    win32con.WM_DESTROY            : win32NotifyIcon.OnDestroy,
    win32con.WM_COMMAND            : win32NotifyIcon.OnCommand,
    WM_TRAY_EVENT                      : win32NotifyIcon.OnTaskbarNotify,
}
NIwc = win32gui.WNDCLASS()
NIwc.hInstance = win32api.GetModuleHandle(None)
NIwc.lpszClassName = "win32NotifyIcon"
NIwc.lpfnWndProc = message_map # could also specify a wndproc.
NIclassAtom = win32gui.RegisterClass(NIwc)




def main():
    def notify_callback(hwnd):
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu( menu, win32con.MF_STRING, 1024, "Generate balloon")
        win32gui.AppendMenu( menu, win32con.MF_STRING, 1025, "Exit")
        pos = win32api.GetCursorPos()
        win32gui.SetForegroundWindow(hwnd)
        win32gui.TrackPopupMenu(menu, win32con.TPM_LEFTALIGN, pos[0], pos[1], 0, hwnd, None)
        win32api.PostMessage(hwnd, win32con.WM_NULL, 0, 0)

    def command_callback(hwnd, cid):
        if cid == 1024:
            from xpra.platform.win32.win32_balloon import notify
            notify(hwnd, "hello", "world")
        elif cid == 1025:
            print("Goodbye")
            win32gui.DestroyWindow(hwnd)
        else:
            print("OnCommand for ID=%s" % cid)

    def win32_quit():
        win32gui.PostQuitMessage(0) # Terminate the app.

    iconPathName = os.path.abspath(os.path.join( sys.prefix, "pyc.ico"))
    win32NotifyIcon(notify_callback, win32_quit, command_callback, iconPathName)
    win32gui.PumpMessages()


if __name__=='__main__':
    main()
