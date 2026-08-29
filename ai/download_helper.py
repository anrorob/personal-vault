"""Fetch Intel's Florence OpenVINO adapter and verify its pinned contents."""

from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen


URL = (
    "https://raw.githubusercontent.com/openvinotoolkit/openvino_notebooks/"
    "latest/notebooks/florence2/ov_florence2_helper.py"
)
EXPECTED = "d0e796134a93faaa375d687e1d6cf7328aeb1e9301a01734d2dd1cbb7a41dc2b"
destination = Path("/app/ov_florence2_helper.py")
contents = urlopen(URL, timeout=30).read()
if sha256(contents).hexdigest() != EXPECTED:
    raise RuntimeError("Intel Florence helper checksum changed")
destination.write_bytes(contents)
