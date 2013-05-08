# This file is part of Xpra.
# Copyright (C) 2008, 2009 Nathaniel Smith <njs@pobox.com>
# Copyright (C) 2011-2013 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

"""The magic GTK widget that represents a client window.

Most of the gunk required to be a valid window manager (reparenting, synthetic
events, mucking about with properties, etc. etc.) is wrapped up in here."""

# Maintain compatibility with old versions of Python, while avoiding a
# deprecation warning on new versions:
import sys
if sys.version_info < (2, 6):
    from sets import ImmutableSet
else:
    ImmutableSet = frozenset
import gobject
import gtk.gdk
import cairo
import os
from socket import gethostname

from xpra.x11.bindings.window_bindings import const, X11WindowBindings #@UnresolvedImport
X11Window = X11WindowBindings()

from xpra.x11.gtk_x11.gdk_bindings import (
               add_event_receiver,                          #@UnresolvedImport
               remove_event_receiver,                       #@UnresolvedImport
               get_display_for,                             #@UnresolvedImport
               calc_constrained_size,                       #@UnresolvedImport
               get_xwindow,                                 #@UnresolvedImport
               )
from xpra.x11.gtk_x11.send_wm import (
                send_wm_take_focus,                         #@UnresolvedImport
                send_wm_delete_window)                      #@UnresolvedImport
from xpra.gtk_common.pixbuf_to_rgb import get_rgb_rawdata
from xpra.gtk_common.gobject_util import (AutoPropGObjectMixin,
                           one_arg_signal, no_arg_signal,
                           non_none_list_accumulator)
from xpra.x11.gtk_x11.error import trap, XError
from xpra.x11.gtk_x11.prop import prop_get, prop_set
from xpra.x11.gtk_x11.composite import CompositeHelper

from xpra.log import Logger
log = Logger()

if gtk.pygtk_version<(2,17):
    log.error("your version of PyGTK is too old - expect some bugs")


# Todo:
#   client focus hints
#   _NET_WM_SYNC_REQUEST
#   root window requests (pagers, etc. requesting to change client states)
#   _NET_WM_PING/detect window not responding (also a root window message)

# Okay, we need a block comment to explain the window arrangement that this
# file is working with.
#
#                +--------+
#                | widget |
#                +--------+
#                  /    \
#  <- top         /     -\-        bottom ->
#                /        \
#          +-------+       |
#          | image |  +---------+
#          +-------+  | corral  |
#                     +---------+
#                          |
#                     +---------+
#                     | client  |
#                     +---------+
#
# Each box in this diagram represents one X/GDK window.  In the common case,
# every window here takes up exactly the same space on the screen (!).  In
# fact, the two windows on the right *always* have exactly the same size and
# location, and the window on the left and the top window also always have
# exactly the same size and position.  However, each window in the diagram
# plays a subtly different role.
#
# The client window is obvious -- this is the window owned by the client,
# which they created and which we have various ICCCM/EWMH-mandated
# responsibilities towards.  It is also composited.
#
# The purpose of the 'corral' is to keep the client window managed -- we
# select for SubstructureRedirect on it, so that the client cannot resize
# etc. without going through the WM.
#
# These two windows are always managed together, as a unit; an invariant of
# the code is that they always take up exactly the same space on the screen.
# They get reparented back and forth between widgets, and when there are no
# widgets, they get reparented to a "parking area".  For now, we're just using
# the root window as a parking area, so we also map/unmap the corral window
# depending on whether we are parked or not; the corral and window is left
# mapped at all times.
#
# When a particular WindowView controls the underlying client window, then two
# things happen:
#   -- Its size determines the size of the client window.  Ideally they are
#      the same size -- but this is not always the case, because the client
#      may have specified sizing constraints, in which case the client window
#      is the "best fit" to the controlling widget window.
#   -- The client window and its corral are reparented under the widget
#      window, as in the diagram above.  This is necessary to allow mouse
#      events to work -- a WindowView widget can always *look* like the client
#      window is there, through the magic of Composite, but in order for it to
#      *act* like the client window is there in terms of receiving mouse
#      events, it has to actually be there.
#
# We should also have a block comment describing how to create a
# view/"controller" for a WindowModel.
#
# Viewing a (Base)WindowModel is easy.  Connect to the client-contents-changed
# signal.  Every time the window contents is updated, you'll get a message.
# This message is passed a single object e, which has useful members:
#   e.x, e.y, e.width, e.height:
#      The part of the client window that was modified, and needs to be
#      redrawn.
# To get the actual contents of the window to draw, there is a "handle"
# available as the "client-contents-handle" property on the
# (Base)WindowModel.  So long as you hold a reference to this object, the
# window contents will be available in its ".pixmap" member.  The pixmap
# itself is also available as the "client-contents" property of the
# (Base)WindowModel.  The pixmap object will be destroyed as soon as the
# handle object leaves scope, so if you want to hold onto the pixmap for some
# reason (animating a fade when a window is unmapped or whatever) then make
# sure to hold a reference to the handle.
#
# But what if you'd like to do more than just look at your pretty composited
# windows?  Maybe you'd like to, say, *interact* with them?  Then life is a
# little more complicated.  To make a view "live", we have to move the actual
# client window to be a child of your view window and position it correctly.
# Obviously, only one view can be live at any given time, so we have to figure
# out which one that is.  Supposing we have a WindowModel called "model" and
# a view called "view", then the following pieces come into play:
#   The "ownership-election" signal on window:
#     If a view wants the chance to become live, it must connect to this
#     signal.  When the signal is emitted, its handler should return a tuple
#     of the form:
#       (votes, my_view)
#     Just like a real election, everyone votes for themselves.  The view that
#     gives the highest value to 'votes' becomes the new owner.  However, a
#     view with a negative (< 0) votes value will never become the owner.
#   model.ownership_election():
#     This method (distinct from the ownership-election signal!) triggers an
#     election.  All views MUST call this method whenever they decide their
#     number of votes has changed.  All views MUST call this method when they
#     are destructing themselves (ideally after disconnecting from the
#     ownership-election signal).
#   The "owner" property on window:
#     This records the view that currently owns the window (i.e., the winner
#     of the last election), or None if no view is live.
#   view.take_window(model, window):
#     This method is called on 'view' when it becomes owner of 'model'.  It
#     should reparent 'window' into the appropriate place, and put it at the
#     appropriate place in its window stack.  (The x,y position, however, does
#     not matter.)
#   view.window_size(model):
#     This method is called when the model needs to know how much space it is
#     allocated.  It should return the maximum (width, height) allowed.
#     (However, the model may choose to use less than this.)
#   view.window_position(mode, width, height):
#     This method is called when the model needs to know where it should be
#     located (relative to the parent window the view placed it in).  'width'
#     and 'height' are the size the model window will actually be.  It should
#     return the (x, y) position desired.
#   model.maybe_recalculate_geometry_for(view):
#     This method (potentially) triggers a resize/move of the client window
#     within the view.  If 'view' is not the current owner, is a no-op, which
#     means that views can call it without worrying about whether they are in
#     fact the current owner.
#
# The actual method for choosing 'votes' is not really determined yet.
# Probably it should take into account at least the following factors:
#   -- has focus (or has mouse-over?)
#   -- is visible in a tray/other window, and the tray/other window is visible
#      -- and is focusable
#      -- and is not focusable
#   -- is visible in a tray, and the tray/other window is not visible
#      -- and is focusable
#      -- and is not focusable
#      (NB: Widget.get_ancestor(my.Tray) will give us the nearest ancestor
#      that isinstance(my.Tray), if any.)
#   -- is not visible
#   -- the size of the widget (as a final tie-breaker)

class Unmanageable(Exception):
    pass

class BaseWindowModel(AutoPropGObjectMixin, gobject.GObject):
    __gproperties__ = {
        "client-window": (gobject.TYPE_PYOBJECT,
                          "gtk.gdk.Window representing the client toplevel", "",
                          gobject.PARAM_READABLE),
        "geometry": (gobject.TYPE_PYOBJECT,
                     "current (border-corrected, relative to parent) coordinates (x, y, w, h) for the window", "",
                     gobject.PARAM_READABLE),
        "transient-for": (gobject.TYPE_PYOBJECT,
                          "Transient for (or None)", "",
                          gobject.PARAM_READABLE),
        "modal": (gobject.TYPE_PYOBJECT,
                          "Modal (boolean)", "",
                          gobject.PARAM_READABLE),
        "window-type": (gobject.TYPE_PYOBJECT,
                        "Window type",
                        "NB, most preferred comes first, then fallbacks",
                        gobject.PARAM_READABLE),
        # NB "notify" signal never fires for the client-contents properties:
        "client-contents": (gobject.TYPE_PYOBJECT,
                            "gtk.gdk.Pixmap containing the window contents", "",
                            gobject.PARAM_READABLE),
        "client-contents-handle": (gobject.TYPE_PYOBJECT,
                                   "", "",
                                   gobject.PARAM_READABLE),
        }
    __gsignals__ = {
        "geometry": no_arg_signal,
        "client-contents-changed": one_arg_signal,
        "unmanaged": one_arg_signal,

        "xpra-configure-event": one_arg_signal,
        }

    def __init__(self, client_window):
        log("new window %s - %s", hex(client_window.xid), hex(get_xwindow(client_window)))
        super(BaseWindowModel, self).__init__()
        self.client_window = client_window
        self._managed = False
        self._managed_handlers = []
        self._setup_done = False
        self._geometry = None
        self._damage_forward_handle = None
        self._internal_set_property("client-window", client_window)
        self._composite = CompositeHelper(self.client_window, False)

    def managed_connect(self, detailed_signal, handler, *args):
        """ connects a signal handler and makes sure we will clean it up on unmanage() """
        handler_id = self.connect(detailed_signal, handler, *args)
        self._managed_handlers.append(handler_id)
        return handler_id

    def managed_disconnect(self):
        for handler_id in self._managed_handlers:
            self.disconnect(handler_id)

    def call_setup(self):
        log("call_setup()")
        try:
            self._geometry = trap.call_synced(X11Window.geometry_with_border, get_xwindow(self.client_window))
        except XError, e:
            raise Unmanageable(e)
        log("call_setup() adding event receiver")
        add_event_receiver(self.client_window, self)
        # Keith Packard says that composite state is undefined following a
        # reparent, so I'm not sure doing this here in the superclass,
        # before we reparent, actually works... let's wait and see.
        log("call_setup() composite setup")
        try:
            trap.call_synced(self._composite.setup)
        except XError, e:
            remove_event_receiver(self.client_window, self)
            log("window %s does not support compositing: %s", hex(get_xwindow(self.client_window)), e)
            trap.swallow_synced(self._composite.destroy)
            self._composite = None
            raise Unmanageable(e)
        #compositing is now enabled, from now on we need to call setup_failed to clean things up
        self._managed = True
        try:
            trap.call_synced(self.setup)
        except XError, e:
            try:
                trap.call_synced(self.setup_failed, e)
            except Exception, ex:
                log.error("error in cleanup handler: %s", ex)
            raise Unmanageable(e)
        self._setup_done = True
        log("call_setup() ended")

    def setup_failed(self, e):
        log("cannot manage %s: %s", hex(get_xwindow(self.client_window)), e)
        self.do_unmanaged(False)

    def setup(self):
        h = self._composite.connect("contents-changed", self._forward_contents_changed)
        self._composite.connect("xpra-configure-event", self.composite_configure_event)
        self._damage_forward_handle = h

    def prop_get(self, key, ptype, ignore_errors=False, raise_xerrors=False):
        # Utility wrapper for prop_get on the client_window
        # also allows us to ignore property errors during setup_client
        if not self._setup_done:
            ignore_errors = True
        return prop_get(self.client_window, key, ptype, ignore_errors=ignore_errors, raise_xerrors=raise_xerrors)

    def is_managed(self):
        return self._managed

    def _forward_contents_changed(self, obj, event):
        if self._managed:
            self.emit("client-contents-changed", event)

    def do_get_property_client_contents(self, name):
        return self._composite.get_property("contents")

    def do_get_property_client_contents_handle(self, name):
        return self._composite.get_property("contents-handle")

    def acknowledge_changes(self):
        self._composite.acknowledge_changes()

    def do_xpra_configure_event(self, event):
        self._geometry = (event.x, event.y, event.width, event.height,
                          event.border_width)
        log.info("WindowModel.do_xpra_configure_event(%s)", event)

    def composite_configure_event(self, composite_window, event):
        log("BaseWindowModel.composite_configure_event(%s,%s)", composite_window, event)
        if self._composite:
            self._composite.do_xpra_configure_event(event)

    def do_get_property_geometry(self, pspec):
        (x, y, w, h, b) = self._geometry
        return (x, y, w + 2*b, h + 2*b)

    def unmanage(self, exiting=False):
        if self._managed:
            self.emit("unmanaged", exiting)

    def do_unmanaged(self, wm_exiting):
        if not self._managed:
            return
        self._managed = False
        log("do_unmanaged(%s) damage_forward_handle=%s, composite=%s", wm_exiting, self._damage_forward_handle, self._composite)
        remove_event_receiver(self.client_window, self)
        gobject.idle_add(self.managed_disconnect)
        if self._composite:
            if self._damage_forward_handle:
                self._composite.disconnect(self._damage_forward_handle)
                self._damage_forward_handle = None
            self._composite.destroy()
            self._composite = None

    def _read_initial_properties(self):
        def pget(key, ptype):
            return self.prop_get(key, ptype, raise_xerrors=True)
        transient_for = pget("WM_TRANSIENT_FOR", "window")
        # May be None
        self._internal_set_property("transient-for", transient_for)

        window_types = pget("_NET_WM_WINDOW_TYPE", ["atom"])
        if not window_types:
            window_type = self._guess_window_type(transient_for)
            window_types = [gtk.gdk.atom_intern(window_type)]
        self._internal_set_property("window-type", window_types)

    def _guess_window_type(self, transient_for):
        if transient_for is not None:
            # EWMH says that even if it's transient-for, we MUST check to
            # see if it's override-redirect (and if so treat as NORMAL).
            # But we wouldn't be here if this was override-redirect.
            # (OverrideRedirectWindowModel overrides this method)
            return "_NET_WM_TYPE_DIALOG"
        return "_NET_WM_WINDOW_TYPE_NORMAL"

    def is_tray(self):
        return False

    def get_rgb_rawdata(self, x, y, width, height):
        pixmap = self.get_property("client-contents")
        if pixmap is None:
            log.debug("get_rgb_rawdata: pixmap is None for window %s", hex(get_xwindow(self.client_window)))
            return  None
        return get_rgb_rawdata(pixmap, x, y, width, height, logger=log)


gobject.type_register(BaseWindowModel)


# FIXME: EWMH says that O-R windows should set various properties just like
# ordinary managed windows; so some of that code should get pushed up into the
# superclass sooner or later.  When someone cares, presumably.
class OverrideRedirectWindowModel(BaseWindowModel):
    __gsignals__ = {
        "xpra-unmap-event": one_arg_signal,
        }

    def __init__(self, client_window):
        BaseWindowModel.__init__(self, client_window)

    def call_setup(self):
        self._read_initial_properties()
        BaseWindowModel.call_setup(self)

    def setup(self):
        BaseWindowModel.setup(self)
        self.client_window.set_events(self.client_window.get_events()
                                      | gtk.gdk.STRUCTURE_MASK)
        # So now if the window becomes unmapped in the future then we will
        # notice... but it might be unmapped already, and any event
        # already generated, and our request for that event is too late!
        # So double check now, *after* putting in our request:
        if not X11Window.is_mapped(get_xwindow(self.client_window)):
            raise Unmanageable("window already unmapped")
        ch = self._composite.get_property("contents-handle")
        if ch is None:
            raise Unmanageable("failed to get damage handle")

    def composite_configure_event(self, composite_window, event):
        BaseWindowModel.composite_configure_event(self, composite_window, event)
        log("OverrideRedirectWindowModel.composite_configure_event(%s, %s) client window geometry=%s", composite_window, event, self.client_window.get_geometry())
        try:
            self._geometry = trap.call_unsynced(X11Window.geometry_with_border, get_xwindow(self.client_window))
            self.emit("geometry")
        except XError:
            log.error("failed to update geometry!", exc_info=True)

    def _guess_window_type(self, transient_for):
        return "_NET_WM_WINDOW_TYPE_NORMAL"

    def do_xpra_unmap_event(self, event):
        self.unmanage()

    def get_dimensions(self):
        ww, wh = self._geometry[2:4]
        return ww, wh

    def is_OR(self):
        return  True

gobject.type_register(OverrideRedirectWindowModel)


class SystemTrayWindowModel(OverrideRedirectWindowModel):

    def __init__(self, client_window):
        OverrideRedirectWindowModel.__init__(self, client_window)

    def is_tray(self):
        return  True

    def _read_initial_properties(self):
        pass

    def composite_configure_event(self, composite_window, event):
        BaseWindowModel.composite_configure_event(self, composite_window, event)
        log("SystemTrayWindowModel.composite_configure_event(%s, %s) client window geometry=%s", composite_window, event, self.client_window.get_geometry())

    def move_resize(self, x, y, width, height):
        #Used by clients to tell us where the tray is located on screen
        log("SystemTrayWindowModel.move_resize(%s, %s, %s, %s)", x, y, width, height)
        self.client_window.move_resize(x, y, width, height)
        border = self._geometry[4]
        self._geometry = (x, y, width, height, border)


class WindowModel(BaseWindowModel):
    """This represents a managed client window.  It allows one to produce
    widgets that view that client window in various ways."""

    _NET_WM_ALLOWED_ACTIONS = [
        "_NET_WM_ACTION_CLOSE",
        ]

    __gproperties__ = {
        # Interesting properties of the client window, that will be
        # automatically kept up to date:
        "attention-requested": (gobject.TYPE_BOOLEAN,
                                "Urgency hint from client, or us", "",
                                False,
                                gobject.PARAM_READWRITE),
        "fullscreen": (gobject.TYPE_BOOLEAN,
                       "Fullscreen-ness of window", "",
                       False,
                       gobject.PARAM_READWRITE),

        "actual-size": (gobject.TYPE_PYOBJECT,
                        "Size of client window (actual (width,height))", "",
                        gobject.PARAM_READABLE),
        "user-friendly-size": (gobject.TYPE_PYOBJECT,
                               "Description of client window size for user", "",
                               gobject.PARAM_READABLE),
        "requested-position": (gobject.TYPE_PYOBJECT,
                               "Client-requested position on screen", "",
                               gobject.PARAM_READABLE),
        "requested-size": (gobject.TYPE_PYOBJECT,
                           "Client-requested size on screen", "",
                           gobject.PARAM_READABLE),
        "size-hints": (gobject.TYPE_PYOBJECT,
                       "Client hints on constraining its size", "",
                       gobject.PARAM_READABLE),
        "strut": (gobject.TYPE_PYOBJECT,
                  "Struts requested by window, or None", "",
                  gobject.PARAM_READABLE),
        "class-instance": (gobject.TYPE_PYOBJECT,
                           "Classic X 'class' and 'instance'", "",
                           gobject.PARAM_READABLE),
        "protocols": (gobject.TYPE_PYOBJECT,
                      "Supported WM protocols", "",
                      gobject.PARAM_READABLE),
        "pid": (gobject.TYPE_INT,
                "PID of owning process", "",
                -1, 65535, -1,
                gobject.PARAM_READABLE),
        "client-machine": (gobject.TYPE_PYOBJECT,
                           "Host where client process is running", "",
                           gobject.PARAM_READABLE),
        "group-leader": (gobject.TYPE_PYOBJECT,
                         "Window group leader (opaque identifier)", "",
                         gobject.PARAM_READABLE),
        # Toggling this property does not actually make the window iconified,
        # i.e. make it appear or disappear from the screen -- it merely
        # updates the various window manager properties that inform the world
        # whether or not the window is iconified.
        "iconic": (gobject.TYPE_BOOLEAN,
                   "ICCCM 'iconic' state -- any sort of 'not on desktop'.", "",
                   False,
                   gobject.PARAM_READWRITE),
        "can-focus": (gobject.TYPE_BOOLEAN,
                      "Does this window ever accept keyboard input?", "",
                      True,
                      gobject.PARAM_READWRITE),
        "state": (gobject.TYPE_PYOBJECT,
                  "State, as per _NET_WM_STATE", "",
                  gobject.PARAM_READABLE),
        "title": (gobject.TYPE_PYOBJECT,
                  "Window title (unicode or None)", "",
                  gobject.PARAM_READABLE),
        "icon-title": (gobject.TYPE_PYOBJECT,
                       "Icon title (unicode or None)", "",
                       gobject.PARAM_READABLE),
        "icon": (gobject.TYPE_PYOBJECT,
                 "Icon (local Cairo surface)", "",
                 gobject.PARAM_READABLE),
        "icon-pixmap": (gobject.TYPE_PYOBJECT,
                        "Icon (server Pixmap)", "",
                        gobject.PARAM_READABLE),

        "owner": (gobject.TYPE_PYOBJECT,
                  "Owner", "",
                  gobject.PARAM_READABLE),
        }
    __gsignals__ = {
        # X11 bell event:
        "bell": one_arg_signal,

        "ownership-election": (gobject.SIGNAL_RUN_LAST,
                               gobject.TYPE_PYOBJECT, (),
                               non_none_list_accumulator),

        "child-map-request-event": one_arg_signal,
        "child-configure-request-event": one_arg_signal,
        "xpra-property-notify-event": one_arg_signal,
        "xpra-unmap-event": one_arg_signal,
        "xpra-destroy-event": one_arg_signal,
        "xpra-xkb-event": one_arg_signal,
        }

    def __init__(self, parking_window, client_window):
        """Register a new client window with the WM.

        Raises an Unmanageable exception if this window should not be
        managed, for whatever reason.  ATM, this mostly means that the window
        died somehow before we could do anything with it."""

        BaseWindowModel.__init__(self, client_window)
        self.parking_window = parking_window
        self.corral_window = None
        self.client_window_saved_events = self.client_window.get_events()
        self.in_save_set = False
        self.client_reparented = False
        self.startup_unmap_serial = None

        # The WM_HINTS input field
        self._input_field = True
        self.connect("notify::iconic", self._handle_iconic_update)

        self.call_setup()

    def setup(self):
        BaseWindowModel.setup(self)

        x, y, w, h, _ = self.client_window.get_geometry()
        # We enable PROPERTY_CHANGE_MASK so that we can call
        # x11_get_server_time on this window.
        self.corral_window = gtk.gdk.Window(self.parking_window,
                                            x = x, y = y, width =w, height= h,
                                            window_type=gtk.gdk.WINDOW_CHILD,
                                            wclass=gtk.gdk.INPUT_OUTPUT,
                                            event_mask=gtk.gdk.PROPERTY_CHANGE_MASK,
                                            title = "CorralWindow-0x%s" % self.client_window.xid)
        log("setup() corral_window=%s", self.corral_window)
        X11Window.substructureRedirect(get_xwindow(self.corral_window))
        add_event_receiver(self.corral_window, self)

        # Start listening for important events.
        self.client_window.set_events(self.client_window_saved_events
                                      | gtk.gdk.STRUCTURE_MASK
                                      | gtk.gdk.PROPERTY_CHANGE_MASK)

        # The child might already be mapped, in case we inherited it from
        # a previous window manager.  If so, we unmap it now, and save the
        # serial number of the request -- this way, when we get an
        # UnmapNotify later, we'll know that it's just from us unmapping
        # the window, not from the client withdrawing the window.
        if X11Window.is_mapped(get_xwindow(self.client_window)):
            log("hiding inherited window")
            self.startup_unmap_serial = X11Window.Unmap(get_xwindow(self.client_window))

        # Process properties
        self._read_initial_properties()
        self._write_initial_properties_and_setup()

        # For now, we never use the Iconic state at all.
        self._internal_set_property("iconic", False)

        log("setup() adding to save set")
        X11Window.XAddToSaveSet(get_xwindow(self.client_window))
        self.in_save_set = True

        log("setup() reparenting")
        self.client_window.reparent(self.corral_window, 0, 0)
        self.client_reparented = True

        log("setup() geometry")
        w,h = self.client_window.get_geometry()[2:4]
        hints = self.get_property("size-hints")
        self._sanitize_size_hints(hints)
        nw, nh = calc_constrained_size(w, h, hints)[:2]
        if nw>=32768 or nh>=32768:
            #we can't handle windows that big!
            raise Unmanageable("window constrained size is too large: %sx%s (from client geometry: %s,%s with size hints=%s)" % (nw, nh, w, h, hints))
        log("setup() resizing windows to %sx%s", nw, nh)
        self.client_window.resize(nw, nh)
        self.corral_window.resize(nw, nh)
        self.client_window.show_unraised()
        self.client_window.get_geometry()


    def is_OR(self):
        return  False

    def get_dimensions(self):
        return  self.get_property("actual-size")

    def do_xpra_xkb_event(self, event):
        log("WindowModel.do_xpra_xkb_event(%r)" % event)
        if event.type!="bell":
            log.error("WindowModel.do_xpra_xkb_event(%r) unknown event type: %s" % (event, event.type))
            return
        event.window_model = self
        self.emit("bell", event)

    def do_child_map_request_event(self, event):
        # If we get a MapRequest then it might mean that someone tried to map
        # this window multiple times in quick succession, before we actually
        # mapped it (so that several MapRequests ended up queued up; FSF Emacs
        # 22.1.50.1 does this, at least).  It alternatively might mean that
        # the client is naughty and tried to map their window which is
        # currently not displayed.  In either case, we should just ignore the
        # request.
        pass

    def do_xpra_unmap_event(self, event):
        if event.delivered_to is self.corral_window or self.corral_window is None:
            return
        assert event.window is self.client_window
        # The client window got unmapped.  The question is, though, was that
        # because it was withdrawn/destroyed, or was it because we unmapped it
        # going into IconicState?
        #
        # At the moment, we never actually put windows into IconicState
        # (i.e. unmap them), except in the special case when we start up and
        # find windows that are already mapped.  So we only need to check
        # against that one serial number.
        #
        # Also, if we receive a *synthetic* UnmapNotify event, that always
        # means that the client has withdrawn the window (even if it was not
        # mapped in the first place) -- ICCCM section 4.1.4.
        log("Client window unmapped")
        if event.send_event or event.serial != self.startup_unmap_serial:
            self.unmanage()

    def do_xpra_destroy_event(self, event):
        if event.delivered_to is self.corral_window or self.corral_window is None:
            return
        assert event.window is self.client_window
        # This is somewhat redundant with the unmap signal, because if you
        # destroy a mapped window, then a UnmapNotify is always generated.
        # However, this allows us to catch the destruction of unmapped
        # ("iconified") windows, and also catch any mistakes we might have
        # made with unmap heuristics.  I love the smell of XDestroyWindow in
        # the morning.  It makes for simple code:
        self.unmanage()

    SCRUB_PROPERTIES = ["WM_STATE",
                        "_NET_WM_STATE",
                        "_NET_FRAME_EXTENTS",
                        "_NET_WM_ALLOWED_ACTIONS",
                        ]

    def do_unmanaged(self, wm_exiting):
        log("unmanaging window: %s (%s - %s)", self, self.corral_window, self.client_window)
        self._internal_set_property("owner", None)
        if self.corral_window:
            remove_event_receiver(self.corral_window, self)
            for prop in WindowModel.SCRUB_PROPERTIES:
                trap.swallow_synced(X11Window.XDeleteProperty, get_xwindow(self.client_window), prop)
            if self.client_reparented:
                self.client_window.reparent(gtk.gdk.get_default_root_window(), 0, 0)
                self.client_reparented = False
            self.client_window.set_events(self.client_window_saved_events)
            #it is now safe to destroy the corral window:
            self.corral_window.destroy()
            self.corral_window = None
            # It is important to remove from our save set, even after
            # reparenting, because according to the X spec, windows that are
            # in our save set are always Mapped when we exit, *even if those
            # windows are no longer inferior to any of our windows!* (see
            # section 10. Connection Close).  This causes "ghost windows", see
            # bug #27:
            if self.in_save_set:
                trap.swallow_synced(X11Window.XRemoveFromSaveSet, get_xwindow(self.client_window))
                self.in_save_set = False
            trap.swallow_synced(X11Window.sendConfigureNotify, get_xwindow(self.client_window))
            if wm_exiting:
                self.client_window.show_unraised()
        BaseWindowModel.do_unmanaged(self, wm_exiting)

    def ownership_election(self):
        candidates = self.emit("ownership-election")
        if candidates:
            rating, winner = sorted(candidates)[-1]
            if rating < 0:
                winner = None
        else:
            winner = None
        old_owner = self.get_property("owner")
        if old_owner is winner:
            return
        if old_owner is not None:
            self.corral_window.hide()
            self.corral_window.reparent(self.parking_window, 0, 0)
        self._internal_set_property("owner", winner)
        if winner is not None:
            winner.take_window(self, self.corral_window)
            self._update_client_geometry()
            self.corral_window.show_unraised()
        trap.swallow_synced(X11Window.sendConfigureNotify, get_xwindow(self.client_window))

    def do_xpra_configure_event(self, event):
        WindowModel.do_xpra_configure_event(self, event)
        self.notify("geometry")

    def maybe_recalculate_geometry_for(self, maybe_owner):
        if maybe_owner and self.get_property("owner") is maybe_owner:
            self._update_client_geometry()

    def _sanitize_size_hints(self, size_hints):
        if size_hints is None:
            return
        for attr in ["min_aspect", "max_aspect"]:
            v = getattr(size_hints, attr)
            if v is not None:
                try:
                    f = float(v)
                except:
                    f = None
                if f is None or f>=(2**32-1):
                    log.warn("clearing invalid aspect hint value for %s: %s", attr, v)
                    setattr(size_hints, attr, -1.0)
        for attr in ["max_size", "min_size", "base_size", "resize_inc",
                     "min_aspect_ratio", "max_aspect_ratio"]:
            v = getattr(size_hints, attr)
            if v is not None:
                try:
                    w,h = v
                except:
                    w,h = None,None
                if (w is None or h is None) or w>=(2**32-1) or h>(2**32-1):
                    log.warn("clearing invalid size hint value for %s: %s", attr, v)
                    setattr(size_hints, attr, (-1,-1))
        #if max-size is smaller than min-size (bogus), clamp it..
        mins = size_hints.min_size
        maxs = size_hints.max_size
        if mins is not None and maxs is not None:
            minw,minh = mins
            maxw,maxh = maxs
            if maxw<minw or maxh<minh:
                size_hints.max_size = max(minw, maxw), max(minh, maxh)
                log.warn("invalid max_size=%s for min_size=%s has now been clamped to: %s",
                         maxs, mins, size_hints.max_size)

    def _update_client_geometry(self):
        owner = self.get_property("owner")
        if owner is not None:
            log("_update_client_geometry: owner()=%s", owner)
            def window_size():
                return  owner.window_size(self)
            def window_position(w, h):
                return  owner.window_position(self, w, h)
            self._do_update_client_geometry(window_size, window_position)
        elif not self._setup_done:
            log("_update_client_geometry: using initial size=%s and position=%s",
                self.get_property("requested-size"), self.get_property("requested-position"))
            #try to honour initial size and position requests during setup:
            def window_size():
                return self.get_property("requested-size")
            def window_position(w, h):
                return self.get_property("requested-position")
            self._do_update_client_geometry(window_size, window_position)

    def _do_update_client_geometry(self, window_size_cb, window_position_cb):
        allocated_w, allocated_h = window_size_cb()
        log("_do_update_client_geometry: %sx%s", allocated_w, allocated_h)
        hints = self.get_property("size-hints")
        log("_do_update_client_geometry: hints=%s", hints)
        self._sanitize_size_hints(hints)
        log("_do_update_client_geometry: sanitized hints=%s", hints)
        size = calc_constrained_size(allocated_w, allocated_h, hints)
        log("_do_update_client_geometry: size=%s", size)
        w, h, wvis, hvis = size
        x, y = window_position_cb(w, h)
        log("_do_update_client_geometry: position=%s", (x,y))
        self.corral_window.move_resize(x, y, w, h)
        trap.swallow_synced(X11Window.configureAndNotify, get_xwindow(self.client_window), 0, 0, w, h)
        self._internal_set_property("actual-size", (w, h))
        self._internal_set_property("user-friendly-size", (wvis, hvis))

    def composite_configure_event(self, composite_window, event):
        log("WindowModel.composite_configure_event(%s, %s)", composite_window, event)
        BaseWindowModel.composite_configure_event(self, composite_window, event)
        gobject.idle_add(self.may_resize_corral_window)

    def may_resize_corral_window(self):
        if not self._managed:
            return
        if self.corral_window is None or not self.corral_window.is_visible():
            return
        if self.client_window is None or not self.client_window.is_visible():
            return
        try:
            #workaround applications whose windows disappear from underneath us:
            if trap.call_synced(self.resize_corral_window):
                self.emit("geometry")
        except XError, e:
            log.warn("failed to resize corral window: %s", e)

    def resize_corral_window(self):
        #the client window may have been resized (generally programmatically)
        #so we may need to update the corral_window to match
        cow, coh = self.corral_window.get_geometry()[2:4]
        clx, cly, clw, clh = self.client_window.get_geometry()[:4]
        if (clx, cly) != (0, 0):
            log("resize_corral_window() client window has moved, resetting it")
            self.client_window.move(0, 0)
        if cow!=clw or coh!=clh:
            log("resize_corral_window() corral window (%sx%s) does not match client window (%sx%s), resizing it",
                     cow, coh, clw, clh)
            self.corral_window.resize(clw, clh)
            hints = self.get_property("size-hints")
            self._sanitize_size_hints(hints)
            size = calc_constrained_size(clw, clh, hints)
            log("resize_corral_window() new constrained size=%s", size)
            w, h, wvis, hvis = size
            self._internal_set_property("actual-size", (w, h))
            self._internal_set_property("user-friendly-size", (wvis, hvis))
            return True
        return False

    def do_child_configure_request_event(self, event):
        # Ignore the request, but as per ICCCM 4.1.5, send back a synthetic
        # ConfigureNotify telling the client that nothing has happened.
        log("do_child_configure_request_event(%s)", event)
        trap.swallow_synced(X11Window.sendConfigureNotify, get_xwindow(event.window))

        # Also potentially update our record of what the app has requested:
        (x, y) = self.get_property("requested-position")
        if event.value_mask & const["CWX"]:
            x = event.x
        if event.value_mask & const["CWY"]:
            y = event.y
        self._internal_set_property("requested-position", (x, y))

        (w, h) = self.get_property("requested-size")
        if event.value_mask & const["CWWidth"]:
            w = event.width
        if event.value_mask & const["CWHeight"]:
            h = event.height
        self._internal_set_property("requested-size", (w, h))
        self._update_client_geometry()

        # FIXME: consider handling attempts to change stacking order here.
        # (In particular, I believe that a request to jump to the top is
        # meaningful and should perhaps even be respected.)

    ################################
    # Property reading
    ################################

    def do_xpra_property_notify_event(self, event):
        if event.delivered_to is self.corral_window:
            return
        assert event.window is self.client_window
        self._handle_property_change(str(event.atom))

    _property_handlers = {}

    def _handle_property_change(self, name):
        log("Property changed on %s: %s", self.client_window.xid, name)
        if name in self._property_handlers:
            self._property_handlers[name](self)

    def _handle_wm_hints(self):
        wm_hints = self.prop_get("WM_HINTS", "wm-hints", True)
        if wm_hints is not None:
            # GdkWindow or None
            self._internal_set_property("group-leader", wm_hints.group_leader)
            # FIXME: extract state and input hint

            if wm_hints.urgency:
                self.set_property("attention-requested", True)

            log("wm_hints.input = %s", wm_hints.input)
            #we only set this value once:
            #(input_field always starts as True, and we then set it to an int)
            if self._input_field is True and wm_hints.input is not None:
                #keep the value as an int to differentiate from the start value:
                self._input_field = int(wm_hints.input)
                if bool(self._input_field):
                    self.notify("can-focus")

    _property_handlers["WM_HINTS"] = _handle_wm_hints

    def _handle_wm_normal_hints(self):
        size_hints = self.prop_get("WM_NORMAL_HINTS", "wm-size-hints")
        # Don't send out notify and ConfigureNotify events when this property
        # gets no-op updated -- some apps like FSF Emacs 21 like to update
        # their properties every time they see a ConfigureNotify, and this
        # reduces the chance for us to get caught in loops:
        old_hints = self.get_property("size-hints")
        if size_hints and (old_hints is None or size_hints.__dict__ != old_hints.__dict__):
            self._internal_set_property("size-hints", size_hints)
            self._update_client_geometry()

    _property_handlers["WM_NORMAL_HINTS"] = _handle_wm_normal_hints

    def _handle_title_change(self):
        net_wm_name = self.prop_get("_NET_WM_NAME", "utf8", True)
        if net_wm_name is not None:
            self._internal_set_property("title", net_wm_name)
        else:
            # may be None
            wm_name = self.prop_get("WM_NAME", "latin1", True)
            self._internal_set_property("title", wm_name)

    _property_handlers["WM_NAME"] = _handle_title_change
    _property_handlers["_NET_WM_NAME"] = _handle_title_change

    def _handle_icon_title_change(self):
        net_wm_icon_name = self.prop_get("_NET_WM_ICON_NAME", "utf8", True)
        if net_wm_icon_name is not None:
            self._internal_set_property("icon-title", net_wm_icon_name)
        else:
            # may be None
            wm_icon_name = self.prop_get("WM_ICON_NAME", "latin1", True)
            self._internal_set_property("icon-title", wm_icon_name)

    _property_handlers["WM_ICON_NAME"] = _handle_icon_title_change
    _property_handlers["_NET_WM_ICON_NAME"] = _handle_icon_title_change

    def _handle_wm_strut(self):
        partial = self.prop_get("_NET_WM_STRUT_PARTIAL", "strut-partial")
        if partial is not None:
            self._internal_set_property("strut", partial)
            return
        full = self.prop_get("_NET_WM_STRUT", "strut")
        # Might be None:
        self._internal_set_property("strut", full)

    _property_handlers["_NET_WM_STRUT"] = _handle_wm_strut
    _property_handlers["_NET_WM_STRUT_PARTIAL"] = _handle_wm_strut

    def _handle_net_wm_icon(self):
        log("_NET_WM_ICON changed on %s, re-reading", self.client_window.xid)
        surf = self.prop_get("_NET_WM_ICON", "icon")
        if surf is not None:
            # FIXME: There is no Pixmap.new_for_display(), so this isn't
            # actually display-clean.  Oh well.
            pixmap = gtk.gdk.Pixmap(None,
                                    surf.get_width(), surf.get_height(), 32)
            screen = get_display_for(pixmap).get_default_screen()
            pixmap.set_colormap(screen.get_rgba_colormap())
            cr = pixmap.cairo_create()
            cr.set_source_surface(surf)
            # Important to use SOURCE, because a newly created Pixmap can have
            # random trash as its contents, and otherwise that will show
            # through any alpha in the icon:
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.paint()
        else:
            pixmap = None
        self._internal_set_property("icon", surf)
        self._internal_set_property("icon-pixmap", pixmap)
        log("icon is now %r", self.get_property("icon"))
    _property_handlers["_NET_WM_ICON"] = _handle_net_wm_icon

    def _read_initial_properties(self):
        # Things that don't change:
        BaseWindowModel._read_initial_properties(self)
        def pget(key, ptype):
            return self.prop_get(key, ptype, raise_xerrors=True)

        geometry = self.client_window.get_geometry()
        self._internal_set_property("requested-position", (geometry[0], geometry[1]))
        self._internal_set_property("requested-size", (geometry[2], geometry[3]))

        def set_class_instance(s):
            try:
                parts = class_instance.split("\0")
                if len(parts)!=3:
                    return  False
                (c, i, _) = parts
                self._internal_set_property("class-instance", (c, i))
                return  True
            except ValueError:
                log.warn("Malformed WM_CLASS: %s, ignoring", class_instance)
                return  False
        class_instance = pget("WM_CLASS", "latin1")
        if class_instance:
            if not set_class_instance(class_instance):
                set_class_instance(pget("WM_CLASS", "utf8"))

        protocols = pget("WM_PROTOCOLS", ["atom"])
        if protocols is None:
            protocols = []
        self._internal_set_property("protocols", protocols)
        self.notify("can-focus")

        pid = pget("_NET_WM_PID", "u32")
        if pid is not None:
            self._internal_set_property("pid", pid)
        else:
            self._internal_set_property("pid", -1)

        client_machine = pget("WM_CLIENT_MACHINE", "latin1")
        # May be None
        self._internal_set_property("client-machine", client_machine)

        # WARNING: have to handle _NET_WM_STATE before we look at WM_HINTS;
        # WM_HINTS assumes that our "state" property is already set.  This is
        # because there are four ways a window can get its urgency
        # ("attention-requested") bit set:
        #   1) _NET_WM_STATE_DEMANDS_ATTENTION in the _initial_ state hints
        #   2) setting the bit WM_HINTS, at _any_ time
        #   3) sending a request to the root window to add
        #      _NET_WM_STATE_DEMANDS_ATTENTION to their state hints
        #   4) if we (the wm) decide they should be and set it
        # To implement this, we generally track the urgency bit via
        # _NET_WM_STATE (since that is under our sole control during normal
        # operation).  Then (1) is accomplished through the normal rule that
        # initial states are read off from the client, and (2) is accomplished
        # by having WM_HINTS affect _NET_WM_STATE.  But this means that
        # WM_HINTS and _NET_WM_STATE handling become intertangled.
        net_wm_state = pget("_NET_WM_STATE", ["atom"])
        if net_wm_state:
            self._internal_set_property("state", ImmutableSet(net_wm_state))
        else:
            self._internal_set_property("state", ImmutableSet())
        modal = (net_wm_state is not None) and ("_NET_WM_STATE_MODAL" in net_wm_state)
        self._internal_set_property("modal", modal)

        for mutable in ["WM_HINTS", "WM_NORMAL_HINTS",
                        "WM_NAME", "_NET_WM_NAME",
                        "WM_ICON_NAME", "_NET_WM_ICON_NAME",
                        "_NET_WM_STRUT", "_NET_WM_STRUT_PARTIAL"]:
            log("reading initial value for %s", mutable)
            self._handle_property_change(mutable)
        for mutable in ["_NET_WM_ICON"]:
            try:
                self._handle_property_change(mutable)
            except:
                log.error("error reading initial property %s", mutable, exc_info=True)

    ################################
    # Property setting
    ################################

    # A few words about _NET_WM_STATE are in order.  Basically, it is a set of
    # flags.  Clients are allowed to set the initial value of this X property
    # to anything they like, when their window is first mapped; after that,
    # though, only the window manager is allowed to touch this property.  So
    # we store its value (or at least, our idea as to its value, the X server
    # in principle could disagree) as the "state" property.  There are
    # basically two things we need to accomplish:
    #   1) Whenever our property is modified, we mirror that modification into
    #      the X server.  This is done by connecting to our own notify::state
    #      signal.
    #   2) As a more user-friendly interface to these state flags, we provide
    #      several boolean properties like "attention-requested".
    #      These are virtual boolean variables; they are actually backed
    #      directly by the "state" property, and reading/writing them in fact
    #      accesses the "state" set directly.  This is done by overriding
    #      do_set_property and do_get_property.
    _state_properties = {
        "attention-requested": "_NET_WM_STATE_DEMANDS_ATTENTION",
        "fullscreen": "_NET_WM_STATE_FULLSCREEN",
        }

    _state_properties_reversed = {}
    for k, v in _state_properties.iteritems():
        _state_properties_reversed[v] = k

    def _state_add(self, state_name):
        curr = set(self.get_property("state"))
        if state_name not in curr:
            curr.add(state_name)
            self._internal_set_property("state", ImmutableSet(curr))
            if state_name in self._state_properties_reversed:
                self.notify(self._state_properties_reversed[state_name])

    def _state_remove(self, state_name):
        curr = set(self.get_property("state"))
        if state_name in curr:
            curr.discard(state_name)
            self._internal_set_property("state", ImmutableSet(curr))
            if state_name in self._state_properties_reversed:
                self.notify(self._state_properties_reversed[state_name])

    def _state_isset(self, state_name):
        return state_name in self.get_property("state")

    def _handle_state_changed(self, *args):
        # Sync changes to "state" property out to X property.
        trap.swallow_synced(prop_set, self.client_window, "_NET_WM_STATE",
                 ["atom"], self.get_property("state"))

    def do_set_property(self, pspec, value):
        if pspec.name in self._state_properties:
            state = self._state_properties[pspec.name]
            if value:
                self._state_add(state)
            else:
                self._state_remove(state)
        else:
            AutoPropGObjectMixin.do_set_property(self, pspec, value)

    def do_get_property_can_focus(self, name):
        assert name == "can-focus"
        return bool(self._input_field) or "WM_TAKE_FOCUS" in self.get_property("protocols")

    def do_get_property(self, pspec):
        if pspec.name in self._state_properties:
            return self._state_isset(self._state_properties[pspec.name])
        else:
            return AutoPropGObjectMixin.do_get_property(self, pspec)


    def _handle_iconic_update(self, *args):
        def set_state(state):
            trap.swallow_synced(prop_set, self.client_window, "WM_STATE",
                             ["u32"],
                             [state, const["XNone"]])

        if self.get_property("iconic"):
            set_state(const["IconicState"])
            self._state_add("_NET_WM_STATE_HIDDEN")
        else:
            set_state(const["NormalState"])
            self._state_remove("_NET_WM_STATE_HIDDEN")

    def _write_initial_properties_and_setup(self):
        # Things that don't change:
        prop_set(self.client_window, "_NET_WM_ALLOWED_ACTIONS",
                 ["atom"], self._NET_WM_ALLOWED_ACTIONS)
        prop_set(self.client_window, "_NET_FRAME_EXTENTS",
                 ["u32"], [0, 0, 0, 0])

        self.connect("notify::state", self._handle_state_changed)
        # Flush things:
        self._handle_state_changed()


    ################################
    # Focus handling:
    ################################

    def give_client_focus(self):
        """The focus manager has decided that our client should recieve X
        focus.  See world_window.py for details."""
        if self.corral_window:
            trap.swallow_synced(self.do_give_client_focus)

    def do_give_client_focus(self):
        log("Giving focus to client")
        # Have to fetch the time, not just use CurrentTime, both because ICCCM
        # says that WM_TAKE_FOCUS must use a real time and because there are
        # genuine race conditions here (e.g. suppose the client does not
        # actually get around to requesting the focus until after we have
        # already changed our mind and decided to give it to someone else).
        now = gtk.gdk.x11_get_server_time(self.corral_window)
        # ICCCM 4.1.7 *claims* to describe how we are supposed to give focus
        # to a window, but it is completely opaque.  From reading the
        # metacity, kwin, gtk+, and qt code, it appears that the actual rules
        # for giving focus are:
        #   -- the WM_HINTS input field determines whether the WM should call
        #      XSetInputFocus
        #   -- independently, the WM_TAKE_FOCUS protocol determines whether
        #      the WM should send a WM_TAKE_FOCUS ClientMessage.
        # If both are set, both methods MUST be used together. For example,
        # GTK+ apps respect WM_TAKE_FOCUS alone but I'm not sure they handle
        # XSetInputFocus well, while Qt apps ignore (!!!) WM_TAKE_FOCUS
        # (unless they have a modal window), and just expect to get focus from
        # the WM's XSetInputFocus.
        if bool(self._input_field):
            log("... using XSetInputFocus")
            X11Window.XSetInputFocus(get_xwindow(self.client_window), now)
        if "WM_TAKE_FOCUS" in self.get_property("protocols"):
            log("... using WM_TAKE_FOCUS")
            send_wm_take_focus(self.client_window, now)

    ################################
    # Killing clients:
    ################################

    def request_close(self):
        if "WM_DELETE_WINDOW" in self.get_property("protocols"):
            trap.swallow_synced(send_wm_delete_window, self.client_window)
        else:
            log.warn("window does not support WM_DELETE_WINDOW... using force_quit()")
            # You don't wanna play ball?  Then no more Mr. Nice Guy!
            self.force_quit()

    def force_quit(self):
        pid = self.get_property("pid")
        machine = self.get_property("client-machine")
        localhost = gethostname()
        if pid > 0 and machine is not None and machine == localhost:
            if pid==os.getpid():
                log.warn("force_quit() refusing to kill ourselves!")
            else:
                try:
                    os.kill(pid, 9)
                except OSError:
                    log.warn("failed to kill() client with pid %s", pid)
        trap.swallow_synced(X11Window.XKillClient, get_xwindow(self.client_window))

gobject.type_register(WindowModel)
