# dmgbuild settings for DownloadPulse's DMG (packaging/desktop/macos/build-app.sh):
# the standard drag-to-install window. The volume holds the application and a
# link to /Applications; its Finder window opens at a fixed size, in icon view,
# without toolbar, sidebar, status or path bar, over the background
# packaging/desktop/assemble.py derives (dmg_background_svg), with each icon
# in its slot -- drag DownloadPulse onto Applications. The volume wears the
# application's own icon.
#   dmgbuild -s dmg-settings.py -D app=<DownloadPulse.app> -D background=<tiff> DownloadPulse <out.dmg>
# Positions are the icons' centres in points; assemble.py's DMG_WINDOW,
# DMG_APP and DMG_APPLICATIONS hold the same values (a test keeps them equal).
import os.path

application = defines["app"]  # noqa: F821 -- dmgbuild provides `defines`
name = os.path.basename(application)

volume_name = "DownloadPulse"
format = "UDZO"
filesystem = "HFS+"
files = [application]
symlinks = {"Applications": "/Applications"}
icon = os.path.join(application, "Contents", "Resources", "DownloadPulse.icns")
# No Finder flags on the application itself (it would no longer verify as
# signed, --strict); Finder already shows an application without ".app".

background = defines["background"]  # noqa: F821
window_rect = ((200, 120), (660, 400))
default_view = "icon-view"
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False
show_icon_preview = False
arrange_by = None
icon_size = 128
text_size = 13
icon_locations = {name: (170, 190), "Applications": (490, 190)}
