# Image dependencies

Pillow 12.3.0 uses MIT-CMU. pillow-heif 1.7.0 source uses BSD-3-Clause.
The installed wheel's unmodified `LICENSES_bundled.txt` additionally identifies
libheif 1.23.3 and libde265 1.1.2 as LGPLv3 and x265 4.2 as GPLv2; it labels
the combined binary wheel GPLv2. The research report's LGPL-only description
is incomplete for this resolved wheel. Source links are in that notice.
These notices are shipped in Sanad's wheel as well as upstream dist-info.
This inventory does not claim redistribution clearance or a deployed image.

Test-only dependencies: arabic-reshaper 3.0.1 (MIT), python-bidi 0.6.11
(LGPLv3, with its bundled Rust dependency notices), and the locally supplied
DejaVu Sans font (Bitstream Vera license, DejaVu changes public domain;
unmodified notice under tests/data/fonts). No font is downloaded by tests.
