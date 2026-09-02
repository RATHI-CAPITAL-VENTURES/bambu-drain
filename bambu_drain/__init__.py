"""bambu-drain — a Raspberry Pi that pretends to be a USB stick.

The printer writes timelapses and sliced files to what it believes is an
ordinary flash drive. It is a backing image on the Pi. When the printer goes
quiet, the Pi ejects the media, empties it, and puts it back.

Nothing here touches the printer's network stack, which is the entire point:
LAN-Only + Developer Mode (the other way to reach the files) costs you Bambu
cloud, Handy and MakerWorld. A USB stick costs you nothing.
"""

__version__ = "0.1.0"
